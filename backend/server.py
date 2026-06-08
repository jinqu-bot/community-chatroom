"""
社区聊天室 - 服务器端
===========================
基于 WebSocket 的实时聊天与游戏平台
"""
import asyncio, json, sqlite3, hashlib, uuid, re
from datetime import datetime
from pathlib import Path
from aiohttp import web
import aiohttp

# 导入斗地主和国际象棋游戏模块
from doudizhu_game import (
    ddz_manager, DDZRoom, DDZPlayer, Card, create_deck, shuffle_deck,
    sort_cards, cards_to_dict_list, dict_list_to_cards, analyze_cards,
    CardType, PlayedHand, is_bomb, calculate_settlement
)
from chess_game import chess_intl_manager, ChessGame

# ==================== 配置 ====================
BASE_DIR = Path(__file__).parent.parent
UPLOAD_DIR = BASE_DIR / "data" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = BASE_DIR / "data" / "chat.db"
MAX_FILE_SIZE = 10 * 1024 * 1024
COOKIE_NAME = "chat_token"
COOKIE_MAX_AGE = 7 * 24 * 60 * 60
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
HOST, PORT = "0.0.0.0", 8080
PUBLIC_BASE_URL, PUBLIC_WS_URL = "", ""

# ==================== 数据库 ====================
def init_db():
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    # 用户表
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL, nickname TEXT NOT NULL, avatar TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    # 聊天记录表
    c.execute("""CREATE TABLE IF NOT EXISTS chat_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL,
        msg_type TEXT NOT NULL, content TEXT NOT NULL, time TEXT NOT NULL)""")
    # 私聊消息表
    c.execute("""CREATE TABLE IF NOT EXISTS private_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, sender TEXT NOT NULL,
        receiver TEXT NOT NULL, content TEXT NOT NULL, time TEXT NOT NULL,
        is_read INTEGER DEFAULT 0, msg_type TEXT DEFAULT 'text',
        filename TEXT DEFAULT '', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    # 用户备注表
    c.execute("""CREATE TABLE IF NOT EXISTS user_remarks (
        id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL,
        target_user TEXT NOT NULL, remark TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE(owner, target_user))""")
    # 帖子表
    c.execute("""CREATE TABLE IF NOT EXISTS posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, author TEXT NOT NULL,
        title TEXT NOT NULL, content TEXT NOT NULL, time TEXT NOT NULL,
        likes INTEGER DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    # 评论表
    c.execute("""CREATE TABLE IF NOT EXISTS comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT, post_id INTEGER NOT NULL,
        author TEXT NOT NULL, content TEXT NOT NULL, time TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (post_id) REFERENCES posts(id) ON DELETE CASCADE)""")
    # 点赞表
    c.execute("""CREATE TABLE IF NOT EXISTS post_likes (
        id INTEGER PRIMARY KEY AUTOINCREMENT, post_id INTEGER NOT NULL,
        user TEXT NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(post_id, user))""")
    # 好友申请表
    c.execute("""CREATE TABLE IF NOT EXISTS friend_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT, from_user TEXT NOT NULL,
        to_user TEXT NOT NULL, message TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'pending',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    # 好友关系表
    c.execute("""CREATE TABLE IF NOT EXISTS friends (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user1 TEXT NOT NULL,
        user2 TEXT NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(user1, user2))""")
    # 会话表
    c.execute("""CREATE TABLE IF NOT EXISTS sessions (
        token TEXT PRIMARY KEY, username TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, expires_at TIMESTAMP)""")
    # 游戏房间表
    c.execute("""CREATE TABLE IF NOT EXISTS game_rooms (
        room_id TEXT PRIMARY KEY, room_name TEXT NOT NULL, game_type TEXT NOT NULL,
        owner_id TEXT NOT NULL, max_players INTEGER NOT NULL DEFAULT 4,
        current_players INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'waiting',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    # 游戏房间玩家表
    c.execute("""CREATE TABLE IF NOT EXISTS game_room_players (
        id INTEGER PRIMARY KEY AUTOINCREMENT, room_id TEXT NOT NULL,
        username TEXT NOT NULL, joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(room_id, username),
        FOREIGN KEY (room_id) REFERENCES game_rooms(room_id) ON DELETE CASCADE)""")
    # 五子棋游戏表
    c.execute("""CREATE TABLE IF NOT EXISTS gomoku_games (
        room_id TEXT PRIMARY KEY, board TEXT NOT NULL DEFAULT '{}',
        current_turn INTEGER NOT NULL DEFAULT 1, player_black TEXT, player_white TEXT,
        black_ready INTEGER DEFAULT 0, white_ready INTEGER DEFAULT 0,
        status TEXT DEFAULT 'waiting', winner TEXT, move_count INTEGER DEFAULT 0,
        last_move_x INTEGER, last_move_y INTEGER, last_move_time TIMESTAMP,
        FOREIGN KEY (room_id) REFERENCES game_rooms(room_id))""")
    # 象棋比赛记录表
    c.execute("""CREATE TABLE IF NOT EXISTS chess_matches (
        id INTEGER PRIMARY KEY AUTOINCREMENT, room_id TEXT NOT NULL,
        red_player TEXT, black_player TEXT, winner TEXT, move_count INTEGER DEFAULT 0,
        start_time TIMESTAMP, end_time TIMESTAMP,
        FOREIGN KEY (room_id) REFERENCES game_rooms(room_id))""")
    # 迁移：添加字段
    for col_tbl in [("last_online","users","TIMESTAMP DEFAULT NULL"),
                     ("filename","chat_history",'TEXT DEFAULT \'\''),
                     ("msg_type","private_messages","TEXT DEFAULT 'text'"),
                     ("filename","private_messages",'TEXT DEFAULT \'\''),
                     ("channel_id","chat_history","TEXT DEFAULT 'general'"),
                     ("reply_to_id","chat_history","INTEGER DEFAULT NULL"),
                     ("reply_to_content","chat_history",'TEXT DEFAULT \'\''),
                     ("reply_to_username","chat_history",'TEXT DEFAULT \'\''),
                     ("reply_to_id","private_messages","INTEGER DEFAULT NULL"),
                     ("reply_to_content","private_messages",'TEXT DEFAULT \'\''),
                     ("reply_to_username","private_messages",'TEXT DEFAULT \'\'')]:
        try: c.execute(f"ALTER TABLE {col_tbl[1]} ADD COLUMN {col_tbl[0]} {col_tbl[2]}")
        except sqlite3.OperationalError: pass
    # 频道表
    c.execute("""CREATE TABLE IF NOT EXISTS channels (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL,
        description TEXT DEFAULT '', creator TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    # 默认"大厅"频道
    c.execute("INSERT OR IGNORE INTO channels (name, description, creator) VALUES ('大厅','默认公共聊天频道','system')")
    conn.commit(); conn.close()

def hash_password(pw): return hashlib.sha256(pw.encode()).hexdigest()
def verify_password(pw, h): return hash_password(pw) == h

def save_message(username, msg_type, content, time_str, filename="", channel_id="general", reply_to_id=None, reply_to_content="", reply_to_username=""):
    conn = sqlite3.connect(str(DB_PATH)); c = conn.cursor()
    c.execute("INSERT INTO chat_history (username, msg_type, content, time, filename, channel_id, reply_to_id, reply_to_content, reply_to_username) VALUES (?,?,?,?,?,?,?,?,?)",
              (username, msg_type, content, time_str, filename, channel_id, reply_to_id, reply_to_content, reply_to_username))
    c.execute("DELETE FROM chat_history WHERE id NOT IN (SELECT id FROM chat_history ORDER BY id DESC LIMIT 500)")
    conn.commit(); conn.close()

def save_private_message(sender, receiver, content, time_str, msg_type='text', filename='', reply_to_id=None, reply_to_content="", reply_to_username=""):
    conn = sqlite3.connect(str(DB_PATH)); c = conn.cursor()
    c.execute("INSERT INTO private_messages (sender, receiver, content, time, msg_type, filename, reply_to_id, reply_to_content, reply_to_username) VALUES (?,?,?,?,?,?,?,?,?)",
              (sender, receiver, content, time_str, msg_type, filename, reply_to_id, reply_to_content, reply_to_username))
    conn.commit(); conn.close()

def get_private_messages(user1, user2):
    conn = sqlite3.connect(str(DB_PATH)); c = conn.cursor()
    c.execute("SELECT id, sender, receiver, content, time, is_read, msg_type, filename, reply_to_id, reply_to_content, reply_to_username FROM private_messages WHERE (sender=? AND receiver=?) OR (sender=? AND receiver=?) ORDER BY id ASC",
              (user1, user2, user2, user1))
    rows = c.fetchall(); conn.close(); return rows

def get_chat_list(username):
    conn = sqlite3.connect(str(DB_PATH)); c = conn.cursor()
    c.execute("""SELECT other, last_msg, last_time, unread FROM (
        SELECT CASE WHEN sender=? THEN receiver ELSE sender END as other,
               content as last_msg, time as last_time,
               (SELECT COUNT(*) FROM private_messages pm2 WHERE pm2.sender=other AND pm2.receiver=? AND pm2.is_read=0) as unread,
               ROW_NUMBER() OVER (PARTITION BY CASE WHEN sender=? THEN receiver ELSE sender END ORDER BY id DESC) as rn
        FROM private_messages WHERE sender=? OR receiver=?) WHERE rn=1 ORDER BY last_time DESC""",
              (username, username, username, username, username))
    rows = c.fetchall(); conn.close(); return rows

def mark_messages_read(username, other_user):
    conn = sqlite3.connect(str(DB_PATH)); c = conn.cursor()
    c.execute("UPDATE private_messages SET is_read=1 WHERE sender=? AND receiver=? AND is_read=0",
              (other_user, username))
    conn.commit(); conn.close()

def get_user_info(username):
    conn = sqlite3.connect(str(DB_PATH)); c = conn.cursor()
    c.execute("SELECT username, nickname, avatar FROM users WHERE username=?", (username,))
    row = c.fetchone(); conn.close(); return row

def update_last_online(username, ts):
    conn = sqlite3.connect(str(DB_PATH)); c = conn.cursor()
    c.execute("UPDATE users SET last_online=? WHERE username=?", (ts, username))
    conn.commit(); conn.close()

def set_remark(owner, target_user, remark):
    conn = sqlite3.connect(str(DB_PATH)); c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO user_remarks (owner, target_user, remark) VALUES (?,?,?)",
              (owner, target_user, remark))
    conn.commit(); conn.close()

def get_remarks(owner):
    conn = sqlite3.connect(str(DB_PATH)); c = conn.cursor()
    c.execute("SELECT target_user, remark FROM user_remarks WHERE owner=?", (owner,))
    rows = c.fetchall(); conn.close(); return {r[0]: r[1] for r in rows}

def get_display_name(target, viewer):
    if target == viewer: return target
    remarks = get_remarks(viewer)
    if target in remarks: return remarks[target]
    user = get_user_info(target)
    return user[1] if user else target

def save_session(token, username):
    conn = sqlite3.connect(str(DB_PATH)); c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO sessions (token, username, expires_at) VALUES (?,?,datetime('now','+7 days'))",
              (token, username))
    conn.commit(); conn.close()

def get_session_user(token):
    conn = sqlite3.connect(str(DB_PATH)); c = conn.cursor()
    c.execute("SELECT username FROM sessions WHERE token=? AND expires_at > datetime('now')", (token,))
    row = c.fetchone(); conn.close(); return row[0] if row else None

def delete_session(token):
    conn = sqlite3.connect(str(DB_PATH)); c = conn.cursor()
    c.execute("DELETE FROM sessions WHERE token=?", (token,)); conn.commit(); conn.close()

def delete_user_sessions(username):
    conn = sqlite3.connect(str(DB_PATH)); c = conn.cursor()
    c.execute("DELETE FROM sessions WHERE username=?", (username,)); conn.commit(); conn.close()

def load_sessions():
    conn = sqlite3.connect(str(DB_PATH)); c = conn.cursor()
    c.execute("SELECT token, username FROM sessions WHERE expires_at > datetime('now')")
    for token, username in c.fetchall():
        chat_room.tokens[token] = username
    conn.close()
    print(f"已恢复 {len(chat_room.tokens)} 个登录会话")

# 帖子相关
def create_post(author, title, content, time_str):
    conn = sqlite3.connect(str(DB_PATH)); c = conn.cursor()
    c.execute("INSERT INTO posts (author, title, content, time) VALUES (?,?,?,?)",
              (author, title, content, time_str))
    pid = c.lastrowid; conn.commit(); conn.close(); return pid

def get_posts(page=1):
    conn = sqlite3.connect(str(DB_PATH)); c = conn.cursor()
    offset = (page - 1) * 20
    c.execute("""SELECT p.id, p.author, p.title, p.content, p.time, p.likes,
        (SELECT COUNT(*) FROM comments WHERE post_id=p.id) as comment_count
        FROM posts p ORDER BY p.id DESC LIMIT 20 OFFSET ?""", (offset,))
    rows = c.fetchall(); conn.close(); return rows

def get_post(post_id):
    conn = sqlite3.connect(str(DB_PATH)); c = conn.cursor()
    c.execute("SELECT id, author, title, content, time, likes, (SELECT COUNT(*) FROM comments WHERE post_id=?) as comment_count FROM posts WHERE id=?", (post_id, post_id))
    row = c.fetchone(); conn.close(); return row

def like_post(post_id, username):
    conn = sqlite3.connect(str(DB_PATH)); c = conn.cursor()
    c.execute("SELECT id FROM post_likes WHERE post_id=? AND user=?", (post_id, username))
    if c.fetchone():
        c.execute("DELETE FROM post_likes WHERE post_id=? AND user=?", (post_id, username))
        c.execute("UPDATE posts SET likes=likes-1 WHERE id=?", (post_id,))
        conn.commit(); conn.close(); return False
    else:
        c.execute("INSERT INTO post_likes (post_id, user) VALUES (?,?)", (post_id, username))
        c.execute("UPDATE posts SET likes=likes+1 WHERE id=?", (post_id,))
        conn.commit(); conn.close(); return True

def is_post_liked(post_id, username):
    conn = sqlite3.connect(str(DB_PATH)); c = conn.cursor()
    c.execute("SELECT 1 FROM post_likes WHERE post_id=? AND user=?", (post_id, username))
    r = c.fetchone(); conn.close(); return r is not None

def add_comment(post_id, author, content, time_str):
    conn = sqlite3.connect(str(DB_PATH)); c = conn.cursor()
    c.execute("INSERT INTO comments (post_id, author, content, time) VALUES (?,?,?,?)",
              (post_id, author, content, time_str))
    cid = c.lastrowid; conn.commit(); conn.close(); return cid

def get_comments(post_id):
    conn = sqlite3.connect(str(DB_PATH)); c = conn.cursor()
    c.execute("SELECT id, author, content, time FROM comments WHERE post_id=? ORDER BY id ASC", (post_id,))
    rows = c.fetchall(); conn.close(); return rows

# 好友相关
def send_friend_request(from_user, to_user, message):
    conn = sqlite3.connect(str(DB_PATH)); c = conn.cursor()
    c.execute("SELECT 1 FROM friends WHERE (user1=? AND user2=?) OR (user1=? AND user2=?)",
              (from_user, to_user, to_user, from_user))
    if c.fetchone(): conn.close(); return False, "已经是好友"
    c.execute("SELECT 1 FROM friend_requests WHERE from_user=? AND to_user=? AND status='pending'",
              (from_user, to_user))
    if c.fetchone(): conn.close(); return False, "已发送过申请"
    c.execute("INSERT INTO friend_requests (from_user, to_user, message) VALUES (?,?,?)",
              (from_user, to_user, message))
    conn.commit(); conn.close(); return True, "申请已发送"

def get_received_requests(username):
    conn = sqlite3.connect(str(DB_PATH)); c = conn.cursor()
    c.execute("""SELECT fr.id, fr.from_user, fr.to_user, fr.message, fr.status, fr.created_at, u.nickname, u.avatar
        FROM friend_requests fr LEFT JOIN users u ON u.username=fr.from_user
        WHERE fr.to_user=? ORDER BY fr.id DESC""", (username,))
    rows = c.fetchall(); conn.close(); return rows

def get_sent_requests(username):
    conn = sqlite3.connect(str(DB_PATH)); c = conn.cursor()
    c.execute("SELECT id, from_user, to_user, message, status, created_at FROM friend_requests WHERE from_user=? ORDER BY id DESC", (username,))
    rows = c.fetchall(); conn.close(); return rows

def accept_friend_request(request_id, username):
    conn = sqlite3.connect(str(DB_PATH)); c = conn.cursor()
    c.execute("SELECT from_user, to_user FROM friend_requests WHERE id=? AND to_user=? AND status='pending'",
              (request_id, username))
    row = c.fetchone()
    if not row: conn.close(); return False, "申请不存在"
    from_user = row[0]
    u1, u2 = sorted([from_user, username])
    c.execute("INSERT OR IGNORE INTO friends (user1, user2) VALUES (?,?)", (u1, u2))
    c.execute("UPDATE friend_requests SET status='accepted' WHERE id=?", (request_id,))
    conn.commit(); conn.close(); return True, from_user

def reject_friend_request(request_id, username):
    conn = sqlite3.connect(str(DB_PATH)); c = conn.cursor()
    c.execute("SELECT from_user FROM friend_requests WHERE id=? AND to_user=? AND status='pending'",
              (request_id, username))
    row = c.fetchone()
    if not row: conn.close(); return False, "申请不存在", ""
    from_user = row[0]
    c.execute("UPDATE friend_requests SET status='rejected' WHERE id=?", (request_id,))
    conn.commit(); conn.close(); return True, "已拒绝", from_user

def are_friends(u1, u2):
    a, b = sorted([u1, u2])
    conn = sqlite3.connect(str(DB_PATH)); c = conn.cursor()
    c.execute("SELECT 1 FROM friends WHERE user1=? AND user2=?", (a, b))
    r = c.fetchone(); conn.close(); return r is not None

def get_friend_list(username):
    conn = sqlite3.connect(str(DB_PATH)); c = conn.cursor()
    c.execute("""SELECT CASE WHEN f.user1=? THEN f.user2 ELSE f.user1 END as friend,
        u.nickname, u.avatar, u.last_online FROM friends f
        JOIN users u ON u.username = CASE WHEN f.user1=? THEN f.user2 ELSE f.user1 END
        WHERE f.user1=? OR f.user2=?""",
              (username, username, username, username))
    rows = c.fetchall(); conn.close(); return rows

def delete_friend(username, friend):
    a, b = sorted([username, friend])
    conn = sqlite3.connect(str(DB_PATH)); c = conn.cursor()
    c.execute("DELETE FROM friends WHERE user1=? AND user2=?", (a, b))
    deleted = c.rowcount > 0; conn.commit(); conn.close(); return deleted

def search_users(keyword, exclude_user):
    conn = sqlite3.connect(str(DB_PATH)); c = conn.cursor()
    c.execute("SELECT username, nickname, avatar, last_online FROM users WHERE (username LIKE ? OR nickname LIKE ?) AND username != ? LIMIT 20",
              (f"%{keyword}%", f"%{keyword}%", exclude_user))
    rows = c.fetchall(); conn.close(); return rows

# 游戏房间DB操作
def db_create_room(room_id, room_name, game_type, owner_id, max_players):
    conn = sqlite3.connect(str(DB_PATH)); c = conn.cursor()
    c.execute("INSERT INTO game_rooms (room_id, room_name, game_type, owner_id, max_players, current_players, status) VALUES (?,?,?,?,?,1,'waiting')",
              (room_id, room_name, game_type, owner_id, max_players))
    c.execute("INSERT INTO game_room_players (room_id, username) VALUES (?,?)", (room_id, owner_id))
    conn.commit(); conn.close()

def db_add_player(room_id, username):
    conn = sqlite3.connect(str(DB_PATH)); c = conn.cursor()
    try: c.execute("INSERT INTO game_room_players (room_id, username) VALUES (?,?)", (room_id, username))
    except sqlite3.IntegrityError: pass
    c.execute("UPDATE game_rooms SET current_players=(SELECT COUNT(*) FROM game_room_players WHERE room_id=?) WHERE room_id=?",
              (room_id, room_id))
    conn.commit(); conn.close()

def db_remove_player(room_id, username):
    conn = sqlite3.connect(str(DB_PATH)); c = conn.cursor()
    c.execute("DELETE FROM game_room_players WHERE room_id=? AND username=?", (room_id, username))
    c.execute("UPDATE game_rooms SET current_players=(SELECT COUNT(*) FROM game_room_players WHERE room_id=?) WHERE room_id=?", (room_id, room_id))
    conn.commit(); conn.close()

def db_delete_room(room_id):
    conn = sqlite3.connect(str(DB_PATH)); c = conn.cursor()
    c.execute("DELETE FROM game_room_players WHERE room_id=?", (room_id,))
    c.execute("DELETE FROM game_rooms WHERE room_id=?", (room_id,))
    conn.commit(); conn.close()

def db_update_room_status(room_id, status):
    conn = sqlite3.connect(str(DB_PATH)); c = conn.cursor()
    c.execute("UPDATE game_rooms SET status=? WHERE room_id=?", (status, room_id))
    conn.commit(); conn.close()

def db_update_room_owner(room_id, new_owner):
    conn = sqlite3.connect(str(DB_PATH)); c = conn.cursor()
    c.execute("UPDATE game_rooms SET owner_id=? WHERE room_id=?", (new_owner, room_id))
    conn.commit(); conn.close()

def db_chess_start(room_id, red_player, black_player):
    conn = sqlite3.connect(str(DB_PATH)); c = conn.cursor()
    c.execute("INSERT INTO chess_matches (room_id, red_player, black_player, start_time) VALUES (?,?,?,?)",
              (room_id, red_player, black_player, datetime.now()))
    conn.commit(); conn.close()

def db_chess_end(room_id, winner, move_count):
    conn = sqlite3.connect(str(DB_PATH)); c = conn.cursor()
    c.execute("UPDATE chess_matches SET winner=?, move_count=?, end_time=? WHERE room_id=? AND end_time IS NULL",
              (winner, move_count, datetime.now(), room_id))
    conn.commit(); conn.close()

# ==================== 五子棋 ====================
BOARD_SIZE = 15; EMPTY = 0; BLACK = 1; WHITE = 2

class GomokuGame:
    def __init__(self, room_id): self.room_id=room_id; self.board=[[EMPTY]*BOARD_SIZE for _ in range(BOARD_SIZE)]; self.current_turn=BLACK; self.player_black=None; self.player_white=None; self.black_ready=False; self.white_ready=False; self.status="waiting"; self.winner=None; self.move_count=0; self.last_move_x=None; self.last_move_y=None; self.last_move_time=None
    def assign_color(self, username):
        if self.player_black is None: self.player_black=username; return BLACK
        elif self.player_white is None: self.player_white=username; return WHITE
        return None
    def get_color(self, username):
        if username==self.player_black: return BLACK
        if username==self.player_white: return WHITE
        return None
    def remove_player(self, username):
        if username==self.player_black: self.player_black=None; self.black_ready=False
        elif username==self.player_white: self.player_white=None; self.white_ready=False
        self._check_players()
    def _check_players(self):
        actual=[p for p in (self.player_black,self.player_white) if p is not None]
        if len(actual)<2 and self.status=="playing": self.status="waiting"; self.black_ready=False; self.white_ready=False
        if len(actual)==0: self.reset_game()
    def set_ready(self, username):
        if self.status!="waiting": return False, False
        if username==self.player_black: self.black_ready=True
        elif username==self.player_white: self.white_ready=True
        else: return False, False
        both=self.black_ready and self.white_ready
        if both: self._start_game()
        return True, both
    def unready(self, username):
        if self.status!="waiting": return
        if username==self.player_black: self.black_ready=False
        elif username==self.player_white: self.white_ready=False
    def _start_game(self): self.status="playing"; self.current_turn=BLACK; self.winner=None; self.board=[[EMPTY]*BOARD_SIZE for _ in range(BOARD_SIZE)]; self.move_count=0; self.last_move_x=None; self.last_move_y=None; self.last_move_time=datetime.now()
    def reset_game(self): self.status="waiting"; self.current_turn=BLACK; self.board=[[EMPTY]*BOARD_SIZE for _ in range(BOARD_SIZE)]; self.winner=None; self.move_count=0; self.black_ready=False; self.white_ready=False; self.last_move_x=None; self.last_move_y=None; self.last_move_time=None
    def make_move(self, username, x, y):
        if self.status!="playing": return False, "游戏未开始", None
        color=self.get_color(username)
        if color is None: return False, "你不是对局玩家", None
        if color!=self.current_turn: return False, "还没轮到你", None
        if x<0 or x>=BOARD_SIZE or y<0 or y>=BOARD_SIZE: return False, "坐标超出棋盘范围", None
        if self.board[y][x]!=EMPTY: return False, "该位置已有棋子", None
        self.board[y][x]=color; self.move_count+=1; self.last_move_x=x; self.last_move_y=y; self.last_move_time=datetime.now()
        winner=self._check_win(x,y,color)
        if winner: self.status="finished"; self.winner=username; return True, "游戏结束", username
        if self.move_count>=BOARD_SIZE*BOARD_SIZE: self.status="finished"; return True, "平局", None
        self.current_turn=WHITE if color==BLACK else BLACK; return True, "落子成功", None
    def _check_win(self, x, y, color):
        for dirs in [[(1,0),(-1,0)],[(0,1),(0,-1)],[(1,1),(-1,-1)],[(1,-1),(-1,1)]]:
            count=1
            for dx,dy in dirs:
                nx,ny=x+dx,y+dy
                while 0<=nx<BOARD_SIZE and 0<=ny<BOARD_SIZE and self.board[ny][nx]==color: count+=1; nx+=dx; ny+=dy
            if count>=5: return True
        return False
    def to_dict(self): return {"room_id":self.room_id,"board":self.board,"current_turn":self.current_turn,"player_black":self.player_black,"player_white":self.player_white,"black_ready":self.black_ready,"white_ready":self.white_ready,"status":self.status,"winner":self.winner,"move_count":self.move_count,"last_move_x":self.last_move_x,"last_move_y":self.last_move_y}

class GomokuGameManager:
    def __init__(self): self.games={}
    def get_or_create(self, room_id):
        if room_id not in self.games: self.games[room_id]=GomokuGame(room_id)
        return self.games[room_id]
    def get(self, room_id): return self.games.get(room_id)
    def remove(self, room_id):
        if room_id in self.games: del self.games[room_id]

gomoku_manager = GomokuGameManager()

# ==================== 中国象棋 ====================
CC_ROOK, CC_HORSE, CC_ELEPHANT, CC_ADVISOR, CC_KING, CC_CANNON, CC_PAWN = "rook","horse","elephant","advisor","king","cannon","pawn"
CC_RED, CC_BLACK = "red","black"; CC_COLS, CC_ROWS = 9,10

class ChineseChessGame:
    def __init__(self, room_id): self.room_id=room_id; self.board=None; self.current_turn=CC_RED; self.player_red=None; self.player_black=None; self.red_ready=False; self.black_ready=False; self.status="waiting"; self.winner=None; self.move_count=0; self.move_history=[]; self.last_move_from=None; self.last_move_to=None; self._init_board()
    def _init_board(self):
        board=[[None]*CC_COLS for _ in range(CC_ROWS)]
        back_black=[CC_ROOK,CC_HORSE,CC_ELEPHANT,CC_ADVISOR,CC_KING,CC_ADVISOR,CC_ELEPHANT,CC_HORSE,CC_ROOK]
        for col,pt in enumerate(back_black): board[0][col]=(CC_BLACK,pt)
        board[2][1]=(CC_BLACK,CC_CANNON); board[2][7]=(CC_BLACK,CC_CANNON)
        for col in(0,2,4,6,8): board[3][col]=(CC_BLACK,CC_PAWN)
        back_red=[CC_ROOK,CC_HORSE,CC_ELEPHANT,CC_ADVISOR,CC_KING,CC_ADVISOR,CC_ELEPHANT,CC_HORSE,CC_ROOK]
        for col,pt in enumerate(back_red): board[9][col]=(CC_RED,pt)
        board[7][1]=(CC_RED,CC_CANNON); board[7][7]=(CC_RED,CC_CANNON)
        for col in(0,2,4,6,8): board[6][col]=(CC_RED,CC_PAWN)
        self.board=board
    def assign_color(self, username):
        if self.player_red is None: self.player_red=username; return CC_RED
        elif self.player_black is None: self.player_black=username; return CC_BLACK
        return None
    def get_color(self, username):
        if username==self.player_red: return CC_RED
        if username==self.player_black: return CC_BLACK
        return None
    def remove_player(self, username):
        if username==self.player_red: self.player_red=None; self.red_ready=False
        elif username==self.player_black: self.player_black=None; self.black_ready=False
        self._check_players()
    def _check_players(self):
        actual=[p for p in(self.player_red,self.player_black) if p is not None]
        if len(actual)<2 and self.status=="playing": self.status="waiting"; self.red_ready=self.black_ready=False
        if len(actual)==0: self.reset_game()
    def set_ready(self, username):
        if self.status!="waiting": return False,False
        if username==self.player_red: self.red_ready=True
        elif username==self.player_black: self.black_ready=True
        else: return False,False
        both=self.red_ready and self.black_ready
        if both: self._start_game()
        return True,both
    def unready(self, username):
        if self.status!="waiting": return
        if username==self.player_red: self.red_ready=False
        elif username==self.player_black: self.black_ready=False
    def _start_game(self): self.status="playing"; self.current_turn=CC_RED; self.winner=None; self.move_count=0; self.move_history.clear(); self._init_board()
    def reset_game(self): self.status="waiting"; self.current_turn=CC_RED; self.winner=None; self.move_count=0; self.move_history.clear(); self.red_ready=self.black_ready=False; self._init_board()
    def is_valid_move(self, fr, fc, tr, tc, color):
        if not(0<=fr<CC_ROWS and 0<=fc<CC_COLS and 0<=tr<CC_ROWS and 0<=tc<CC_COLS): return False,"坐标超出棋盘范围"
        if fr==tr and fc==tc: return False,"起点和终点相同"
        piece=self.board[fr][fc]
        if piece is None: return False,"该位置没有棋子"
        if piece[0]!=color: return False,"不能移动对手的棋子"
        target=self.board[tr][tc]
        if target and target[0]==color: return False,"不能吃己方棋子"
        pt=piece[1]
        if pt==CC_ROOK: return self._rook_rule(fr,fc,tr,tc)
        elif pt==CC_HORSE: return self._horse_rule(fr,fc,tr,tc)
        elif pt==CC_ELEPHANT: return self._elephant_rule(fr,fc,tr,tc,color)
        elif pt==CC_ADVISOR: return self._advisor_rule(fr,fc,tr,tc,color)
        elif pt==CC_KING: return self._king_rule(fr,fc,tr,tc,color)
        elif pt==CC_CANNON: return self._cannon_rule(fr,fc,tr,tc)
        elif pt==CC_PAWN: return self._pawn_rule(fr,fc,tr,tc,color)
        return False,"未知棋子类型"
    def _rook_rule(self,fr,fc,tr,tc):
        if fr!=tr and fc!=tc: return False,"车只能走直线"
        if not self._path_clear(fr,fc,tr,tc): return False,"车不能跨越其他棋子"
        return True,""
    def _horse_rule(self,fr,fc,tr,tc):
        dr,dc=abs(tr-fr),abs(tc-fc)
        if not((dr==2 and dc==1) or (dr==1 and dc==2)): return False,"马只能走日字"
        if dr==2:
            if self.board[fr+(1 if tr>fr else -1)][fc] is not None: return False,"马蹩脚了"
        else:
            if self.board[fr][fc+(1 if tc>fc else -1)] is not None: return False,"马蹩脚了"
        return True,""
    def _elephant_rule(self,fr,fc,tr,tc,color):
        dr,dc=abs(tr-fr),abs(tc-fc)
        if dr!=2 or dc!=2: return False,"象只能走田字"
        if color==CC_RED and tr<5: return False,"相不能过河"
        if color==CC_BLACK and tr>4: return False,"象不能过河"
        er=fr+(1 if tr>fr else -1); ec=fc+(1 if tc>fc else -1)
        if self.board[er][ec] is not None: return False,"象眼被塞住了"
        return True,""
    def _advisor_rule(self,fr,fc,tr,tc,color):
        if abs(tr-fr)!=1 or abs(tc-fc)!=1: return False,"士只能走斜线一步"
        if not self._in_palace(tr,tc,color): return False,"士不能出九宫"
        return True,""
    def _king_rule(self,fr,fc,tr,tc,color):
        dr,dc=abs(tr-fr),abs(tc-fc)
        if not((dr==1 and dc==0) or (dr==0 and dc==1)): return False,"帅只能直线走一步"
        if not self._in_palace(tr,tc,color): return False,"帅不能出九宫"
        return True,""
    def _cannon_rule(self,fr,fc,tr,tc):
        if fr!=tr and fc!=tc: return False,"炮只能走直线"
        target=self.board[tr][tc]; cnt=self._count_between(fr,fc,tr,tc)
        if target is None:
            if cnt>0: return False,"炮移动时不能跨越棋子"
        else:
            if cnt!=1: return False,"炮吃子必须翻过一座山"
        return True,""
    def _pawn_rule(self,fr,fc,tr,tc,color):
        dr=tr-fr; dc=abs(tc-fc)
        if color==CC_RED:
            if dc==0 and dr==-1: return True,""
            if fr<=4 and dc==1 and dr==0: return True,""
            return False,"兵只能前进，过河后可以横走"
        else:
            if dc==0 and dr==1: return True,""
            if fr>=5 and dc==1 and dr==0: return True,""
            return False,"卒只能前进，过河后可以横走"
    def _in_palace(self,row,col,color):
        if color==CC_RED: return 7<=row<=9 and 3<=col<=5
        else: return 0<=row<=2 and 3<=col<=5
    def _path_clear(self,fr,fc,tr,tc):
        if fr==tr:
            step=1 if tc>fc else -1
            for c in range(fc+step,tc,step):
                if self.board[fr][c] is not None: return False
        else:
            step=1 if tr>fr else -1
            for r in range(fr+step,tr,step):
                if self.board[r][fc] is not None: return False
        return True
    def _count_between(self,fr,fc,tr,tc):
        cnt=0
        if fr==tr:
            step=1 if tc>fc else -1
            for c in range(fc+step,tc,step):
                if self.board[fr][c] is not None: cnt+=1
        else:
            step=1 if tr>fr else -1
            for r in range(fr+step,tr,step):
                if self.board[r][fc] is not None: cnt+=1
        return cnt
    def _find_king(self,color):
        for r in range(CC_ROWS):
            for c in range(CC_COLS):
                p=self.board[r][c]
                if p and p[0]==color and p[1]==CC_KING: return r,c
        return None,None
    def _kings_facing(self):
        rr,rc=self._find_king(CC_RED); br,bc=self._find_king(CC_BLACK)
        if rr is None or br is None or rc!=bc: return False
        step=1 if br>rr else -1
        for r in range(rr+step,br,step):
            if self.board[r][rc] is not None: return False
        return True
    def is_check(self,color):
        kr,kc=self._find_king(color)
        if kr is None: return False
        opp=CC_BLACK if color==CC_RED else CC_RED
        for r in range(CC_ROWS):
            for c in range(CC_COLS):
                p=self.board[r][c]
                if p and p[0]==opp:
                    valid,_=self.is_valid_move(r,c,kr,kc,opp)
                    if valid: return True
        return False
    def is_checkmate(self,color):
        for r in range(CC_ROWS):
            for c in range(CC_COLS):
                p=self.board[r][c]
                if p and p[0]==color:
                    for tr in range(CC_ROWS):
                        for tc in range(CC_COLS):
                            valid,_=self.is_valid_move(r,c,tr,tc,color)
                            if not valid: continue
                            saved=self.board[tr][tc]
                            self.board[tr][tc]=p; self.board[r][c]=None
                            in_check=self._kings_facing() or self.is_check(color)
                            self.board[r][c]=p; self.board[tr][tc]=saved
                            if not in_check: return False
        return True
    def make_move(self,username,fr,fc,tr,tc):
        if self.status!="playing": return False,"游戏未开始",{}
        color=self.get_color(username)
        if color is None: return False,"你不是对局玩家",{}
        if color!=self.current_turn: return False,"还没轮到你",{}
        valid,msg=self.is_valid_move(fr,fc,tr,tc,color)
        if not valid: return False,msg,{}
        piece=self.board[fr][fc]
        saved=self.board[tr][tc]
        self.board[tr][tc]=piece; self.board[fr][fc]=None
        if self._kings_facing() or self.is_check(color):
            self.board[fr][fc]=piece; self.board[tr][tc]=saved; return False,"走后会导致自己被将",{}
        self.move_count+=1
        self.move_history.append({"from":[fr,fc],"to":[tr,tc],"captured":saved,"color":color})
        self.last_move_from=[fr,fc]; self.last_move_to=[tr,tc]
        opp=CC_BLACK if color==CC_RED else CC_RED
        extra={}
        if self.is_check(opp): extra["is_check"]=True
        if self.is_checkmate(opp): extra["is_checkmate"]=True; extra["winner"]=username; self.status="finished"; self.winner=username
        self.current_turn=opp; return True,"走棋成功",extra
    def to_dict(self): return {"room_id":self.room_id,"board":self.board,"current_turn":self.current_turn,"player_red":self.player_red,"player_black":self.player_black,"red_ready":self.red_ready,"black_ready":self.black_ready,"status":self.status,"winner":self.winner,"move_count":self.move_count,"last_move_from":self.last_move_from,"last_move_to":self.last_move_to}

class ChineseChessManager:
    def __init__(self): self.games={}
    def get_or_create(self,room_id):
        if room_id not in self.games: self.games[room_id]=ChineseChessGame(room_id)
        return self.games[room_id]
    def get(self,room_id): return self.games.get(room_id)
    def remove(self,room_id):
        if room_id in self.games: del self.games[room_id]

chess_manager = ChineseChessManager()

# ==================== 聊天室与游戏大厅 ====================
GAME_TYPES = {
    "gomoku":{"name":"五子棋","icon":"♟️","max_players":2},
    "chinese_chess":{"name":"中国象棋","icon":"♚","max_players":2},
    "draw_guess":{"name":"你画我猜","icon":"🎨","max_players":8},
    "aeroplane_chess":{"name":"飞行棋","icon":"✈️","max_players":4},
    "doudizhu":{"name":"斗地主","icon":"🃏","max_players":3},
    "chess":{"name":"国际象棋","icon":"♞","max_players":2},
}

class ChatRoom:
    def __init__(self): self.clients=set(); self.user_map={}; self.tokens={}; self.online_users={}
    async def broadcast(self, message):
        disconnected=[]
        for client in list(self.clients):
            try:
                if not client.closed: await client.send_str(message)
            except Exception: disconnected.append(client)
        for client in disconnected: await self.remove_client(client)
    async def add_client(self, websocket, username):
        self.clients.add(websocket); self.user_map[websocket]=username
        if username not in self.online_users: self.online_users[username]=set()
        self.online_users[username].add(websocket)
        update_last_online(username, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    async def remove_client(self, websocket):
        username=self.user_map.pop(websocket,None); self.clients.discard(websocket)
        if username and username in self.online_users:
            self.online_users[username].discard(websocket)
            if not self.online_users[username]:
                del self.online_users[username]
                update_last_online(username, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        return username
    def is_online(self, username): return username in self.online_users and len(self.online_users[username])>0
    @property
    def online_count(self): return len(self.clients)

chat_room = ChatRoom()

class GameLobby:
    def __init__(self): self.rooms={}
    def create_room(self,room_id,room_name,game_type,owner_id,max_players=4):
        room={"room_id":room_id,"room_name":room_name,"game_type":game_type,"owner_id":owner_id,"max_players":max_players,"current_players":1,"status":"waiting","players":{owner_id},"spectators":set()}
        self.rooms[room_id]=room; return room
    def join_room(self,room_id,username):
        room=self.rooms.get(room_id)
        if not room: return False,"房间不存在",None
        if username in room["players"]: return True,"已在房间中","player"
        if username in room.get("spectators",set()): return True,"已在观战中","spectator"
        pc=len(room["players"])
        if room["status"]=="waiting" and pc<room["max_players"]:
            room["players"].add(username); room["current_players"]=pc+1; return True,"加入成功","player"
        if "spectators" not in room: room["spectators"]=set()
        room["spectators"].add(username); return True,"进入观战","spectator"
    def leave_room(self,room_id,username):
        room=self.rooms.get(room_id)
        if not room: return False,"房间不存在",None
        is_player=username in room["players"]; is_spectator=username in room.get("spectators",set())
        if not is_player and not is_spectator: return False,"不在房间中",None
        new_owner=None
        if is_player:
            room["players"].discard(username); room["current_players"]=len(room["players"])
            if not room["players"]: del self.rooms[room_id]; return True,"离开成功（房间已解散）",None
            if username==room["owner_id"]:
                new_owner=next(iter(room["players"])); room["owner_id"]=new_owner
        if is_spectator: room["spectators"].discard(username)
        return True,"离开成功",new_owner
    def dismiss_room(self,room_id,username):
        room=self.rooms.get(room_id)
        if not room: return False,"房间不存在",set()
        if room["owner_id"]!=username: return False,"只有房主可以解散房间",set()
        all_users=room["players"]|room.get("spectators",set()); all_copy=all_users.copy(); del self.rooms[room_id]
        return True,"房间已解散",all_copy
    def start_game(self,room_id,username):
        room=self.rooms.get(room_id)
        if not room: return False,"房间不存在"
        if room["owner_id"]!=username: return False,"只有房主可以开始游戏"
        if room["status"]!="waiting": return False,"游戏已经开始了"
        if len(room["players"])<2: return False,"至少需要2名玩家"
        room["status"]="playing"; return True,"游戏开始"
    def finish_game(self,room_id):
        room=self.rooms.get(room_id)
        if room: room["status"]="waiting"
    def get_room_info(self,room_id): return self.rooms.get(room_id)
    def get_room_list(self): return list(self.rooms.values())
    def get_user_room(self,username):
        for room in self.rooms.values():
            if username in room["players"]: return room
        return None
    def get_user_in_room(self,username):
        for room in self.rooms.values():
            if username in room["players"]: return room,"player"
            if username in room.get("spectators",set()): return room,"spectator"
        return None,None
    def get_all_users_in_room(self,room_id):
        room=self.rooms.get(room_id)
        if not room: return set()
        return room["players"]|room.get("spectators",set())

game_lobby = GameLobby()

# ==================== Auth ====================
def make_cookie_response(response_data, cookie_value=None):
    response=web.json_response(response_data)
    if cookie_value: response.set_cookie(COOKIE_NAME, cookie_value, max_age=COOKIE_MAX_AGE, path="/", httponly=True, samesite="Lax")
    return response

def get_auth_user(request):
    token=request.cookies.get(COOKIE_NAME,"")
    if not token:
        auth_header=request.headers.get("Authorization","")
        if auth_header.startswith("Bearer "): token=auth_header[7:]
    if not token: return None
    if token in chat_room.tokens: return chat_room.tokens[token]
    username=get_session_user(token)
    if username: chat_room.tokens[token]=username; return username
    return None

def get_client_ip(request):
    forwarded=request.headers.get('X-Forwarded-For','')
    if forwarded: return forwarded.split(',')[0].strip()
    real_ip=request.headers.get('X-Real-IP','')
    if real_ip: return real_ip.strip()
    return request.remote or 'unknown'

# ==================== API Handlers ====================
async def api_register(request):
    try:
        data=await request.json(); username=data.get("username","").strip(); password=data.get("password",""); nickname=data.get("nickname",username)
        if not username or not password: return web.json_response({"code":400,"msg":"用户名和密码不能为空"})
        if len(username)<3 or len(username)>20: return web.json_response({"code":400,"msg":"用户名长度需在3-20位"})
        if len(password)<6: return web.json_response({"code":400,"msg":"密码至少6位"})
        conn=sqlite3.connect(str(DB_PATH)); c=conn.cursor()
        c.execute("SELECT id FROM users WHERE username=?",(username,))
        if c.fetchone(): conn.close(); return web.json_response({"code":400,"msg":"用户名已被注册"})
        hashed=hash_password(password); c.execute("INSERT INTO users (username,password,nickname) VALUES (?,?,?)",(username,hashed,nickname))
        conn.commit(); conn.close(); return web.json_response({"code":200,"msg":"注册成功"})
    except Exception as e: print(f"注册错误:{e}"); return web.json_response({"code":500,"msg":"服务器错误"})

async def api_login(request):
    try:
        data=await request.json(); username=data.get("username","").strip(); password=data.get("password","")
        conn=sqlite3.connect(str(DB_PATH)); c=conn.cursor()
        c.execute("SELECT id,password,nickname,avatar FROM users WHERE username=?",(username,)); user=c.fetchone(); conn.close()
        if not user or not verify_password(password,user[1]): return web.json_response({"code":401,"msg":"用户名或密码错误"})
        token=str(uuid.uuid4()); chat_room.tokens[token]=username; save_session(token,username)
        return make_cookie_response({"code":200,"msg":"登录成功","data":{"token":token,"username":username,"nickname":user[2],"avatar":user[3]}},cookie_value=token)
    except Exception as e: print(f"登录错误:{e}"); return web.json_response({"code":500,"msg":"服务器错误"})

async def api_logout(request):
    username=get_auth_user(request)
    if username:
        for t,u in list(chat_room.tokens.items()):
            if u==username: del chat_room.tokens[t]; delete_session(t)
        delete_user_sessions(username)
    response=web.json_response({"code":200,"msg":"已退出"}); response.del_cookie(COOKIE_NAME,path="/"); return response

async def api_check_login(request):
    token=request.cookies.get(COOKIE_NAME,"")
    auth_header=request.headers.get("Authorization","")
    if auth_header.startswith("Bearer "): token=auth_header[7:] or token
    username=None
    if token:
        if token in chat_room.tokens: username=chat_room.tokens[token]
        else:
            username=get_session_user(token)
            if username: chat_room.tokens[token]=username
    if username:
        user=get_user_info(username)
        if user: return web.json_response({"code":200,"data":{"logged_in":True,"username":user[0],"nickname":user[1],"avatar":user[2],"token":token,"public_base_url":PUBLIC_BASE_URL,"public_ws_url":PUBLIC_WS_URL}})
    return web.json_response({"code":200,"data":{"logged_in":False}})

async def api_server_info(request):
    return web.json_response({"code":200,"data":{"base_url":PUBLIC_BASE_URL or f"http://localhost:{PORT}","ws_url":PUBLIC_WS_URL or f"ws://localhost:{PORT}/ws"}})

async def api_upload(request):
    try:
        username=get_auth_user(request)
        if not username: return web.json_response({"code":401,"msg":"请先登录"},status=401)
        reader=await request.multipart(); field=await reader.next()
        if not field or field.name!="file": return web.json_response({"code":400,"msg":"缺少文件字段"})
        filename=field.filename
        if not filename: return web.json_response({"code":400,"msg":"文件名无效"})
        file_data=b""
        while True:
            chunk=await field.read_chunk(65536)
            if not chunk: break
            file_data+=chunk
            if len(file_data)>MAX_FILE_SIZE: return web.json_response({"code":413,"msg":"文件过大（最大10MB）"})
        ext=Path(filename).suffix.lower(); unique_name=f"{uuid.uuid4().hex}{ext}"; file_path=UPLOAD_DIR/unique_name
        with open(file_path,"wb") as f: f.write(file_data)
        msg_type="image" if ext in IMAGE_EXTS else "file"; file_url=f"/uploads/{unique_name}"
        return web.json_response({"code":200,"data":{"type":msg_type,"url":file_url,"filename":filename,"size":len(file_data)}})
    except Exception as e: print(f"上传错误:{e}"); return web.json_response({"code":500,"msg":f"上传失败:{str(e)}"})

async def api_history(request):
    try:
        limit=int(request.query.get("limit",50)); offset=int(request.query.get("offset",0))
        channel_id=request.query.get("channel","general")
        conn=sqlite3.connect(str(DB_PATH)); c=conn.cursor()
        c.execute("SELECT id,username,msg_type,content,time,filename,reply_to_id,reply_to_content,reply_to_username FROM chat_history WHERE channel_id=? ORDER BY id DESC LIMIT ? OFFSET ?",(channel_id,limit,offset))
        rows=c.fetchall(); conn.close()
        history=[{"id":r[0],"username":r[1],"type":r[2],"content":r[3],"time":r[4],"filename":r[5] or "","reply_to_id":r[6],"reply_to_content":r[7] or "","reply_to_username":r[8] or ""} for r in reversed(rows)]
        return web.json_response({"code":200,"data":history})
    except Exception as e: print(f"历史错误:{e}"); return web.json_response({"code":500,"msg":"服务器错误"})

# 频道API
async def api_get_channels(request):
    conn=sqlite3.connect(str(DB_PATH)); c=conn.cursor()
    c.execute("SELECT id,name,description,creator FROM channels ORDER BY id ASC")
    rows=c.fetchall(); conn.close()
    return web.json_response({"code":200,"data":[{"id":r[0],"name":r[1],"description":r[2],"creator":r[3]} for r in rows]})

async def api_create_channel(request):
    username=get_auth_user(request)
    if not username: return web.json_response({"code":401,"msg":"未登录"})
    data=await request.json(); name=data.get("name","").strip(); description=data.get("description","")
    if not name or len(name)<1 or len(name)>20: return web.json_response({"code":400,"msg":"频道名需1-20位"})
    try:
        conn=sqlite3.connect(str(DB_PATH)); c=conn.cursor()
        c.execute("INSERT INTO channels (name,description,creator) VALUES (?,?,?)",(name,description,username))
        cid=c.lastrowid; conn.commit(); conn.close()
        await chat_room.broadcast(json.dumps({"type":"channel_updated"}))
        return web.json_response({"code":200,"msg":"频道创建成功","data":{"id":cid,"name":name}})
    except sqlite3.IntegrityError: return web.json_response({"code":400,"msg":"频道名已存在"})

async def api_delete_channel(request):
    username=get_auth_user(request)
    if not username: return web.json_response({"code":401,"msg":"未登录"})
    data=await request.json(); channel_name=data.get("name","").strip()
    if channel_name=="大厅": return web.json_response({"code":400,"msg":"不能删除默认频道"})
    conn=sqlite3.connect(str(DB_PATH)); c=conn.cursor()
    c.execute("SELECT creator FROM channels WHERE name=?",(channel_name,)); row=c.fetchone()
    if not row: conn.close(); return web.json_response({"code":400,"msg":"频道不存在"})
    if row[0]!=username and username not in ("system","admin"): conn.close(); return web.json_response({"code":400,"msg":"只有创建者可以删除"})
    c.execute("DELETE FROM channels WHERE name=?",(channel_name,))
    c.execute("DELETE FROM chat_history WHERE channel_id=?",(channel_name,))
    conn.commit(); conn.close()
    await chat_room.broadcast(json.dumps({"type":"channel_updated"}))
    return web.json_response({"code":200,"msg":"频道已删除"})

# 私聊
async def api_send_private(request):
    username=get_auth_user(request)
    if not username: return web.json_response({"code":401,"msg":"未登录"})
    data=await request.json(); receiver=data.get("receiver","").strip(); content=data.get("content","").strip(); msg_type=data.get("msg_type","text"); filename=data.get("filename","")
    reply_to=data.get("reply_to")
    rid=reply_to.get("id") if reply_to else None; rcontent=reply_to.get("content","") if reply_to else ""; rusername=reply_to.get("username","") if reply_to else ""
    if not receiver or not content: return web.json_response({"code":400,"msg":"参数不完整"})
    if receiver==username: return web.json_response({"code":400,"msg":"不能给自己发消息"})
    user=get_user_info(receiver)
    if not user: return web.json_response({"code":404,"msg":"用户不存在"})
    time_str=datetime.now().strftime("%H:%M:%S"); save_private_message(username,receiver,content,time_str,msg_type,filename,rid,rcontent,rusername)
    msg_json=json.dumps({"type":"private","sender":username,"receiver":receiver,"content":content,"msg_type":msg_type,"filename":filename,"time":time_str,"reply_to":reply_to})
    for ws,user_name in chat_room.user_map.items():
        if user_name in(username,receiver):
            try: await ws.send_str(msg_json)
            except Exception: pass
    return web.json_response({"code":200,"msg":"发送成功"})

async def api_get_private_history(request):
    username=get_auth_user(request)
    if not username: return web.json_response({"code":401,"msg":"未登录"})
    other=request.query.get("user","")
    if not other: return web.json_response({"code":400,"msg":"缺少参数"})
    messages=get_private_messages(username,other); mark_messages_read(username,other)
    result=[{"id":m[0],"sender":m[1],"receiver":m[2],"content":m[3],"time":m[4],"is_read":m[5],"msg_type":m[6] if len(m)>6 else 'text',"filename":m[7] if len(m)>7 else '',"reply_to_id":m[8] if len(m)>8 else None,"reply_to_content":m[9] if len(m)>9 else '',"reply_to_username":m[10] if len(m)>10 else ''} for m in messages]; result.reverse()
    return web.json_response({"code":200,"data":result})

async def api_get_chat_list(request):
    username=get_auth_user(request)
    if not username: return web.json_response({"code":401,"msg":"未登录"})
    chats=get_chat_list(username); remarks=get_remarks(username); chat_users={c[0] for c in chats}; result=[]
    for chat in chats:
        ou=chat[0]; ui=get_user_info(ou)
        result.append({"username":ou,"nickname":remarks.get(ou) or (ui[1] if ui else ou),"avatar":ui[2] if ui else "","last_msg":chat[1] or "","last_time":chat[2] or "","unread":chat[3] or 0})
    friends=get_friend_list(username)
    for f in friends:
        if f[0] not in chat_users: result.append({"username":f[0],"nickname":remarks.get(f[0]) or f[1],"avatar":f[2] or "","last_msg":"","last_time":"","unread":0})
    return web.json_response({"code":200,"data":result})

async def api_get_all_users(request):
    username=get_auth_user(request)
    if not username: return web.json_response({"code":401,"msg":"未登录"})
    conn=sqlite3.connect(str(DB_PATH)); c=conn.cursor()
    c.execute("SELECT username,nickname,avatar FROM users WHERE username!=?",(username,)); users=c.fetchall(); conn.close()
    remarks=get_remarks(username)
    result=[{"username":u[0],"nickname":remarks.get(u[0]) or u[1],"avatar":u[2],"online":chat_room.is_online(u[0])} for u in users]
    return web.json_response({"code":200,"data":result})

async def api_set_remark(request):
    username=get_auth_user(request)
    if not username: return web.json_response({"code":401,"msg":"未登录"})
    data=await request.json(); target=data.get("target","").strip(); remark=data.get("remark","").strip()
    if not target: return web.json_response({"code":400,"msg":"缺少目标用户"})
    if not remark:
        conn=sqlite3.connect(str(DB_PATH)); c=conn.cursor()
        c.execute("DELETE FROM user_remarks WHERE owner=? AND target_user=?",(username,target)); conn.commit(); conn.close()
        return web.json_response({"code":200,"msg":"备注已删除"})
    set_remark(username,target,remark); return web.json_response({"code":200,"msg":"备注设置成功"})

async def api_get_remarks(request):
    username=get_auth_user(request)
    if not username: return web.json_response({"code":401,"msg":"未登录"})
    return web.json_response({"code":200,"data":get_remarks(username)})

# 帖子
async def api_create_post(request):
    username=get_auth_user(request)
    if not username: return web.json_response({"code":401,"msg":"未登录"})
    data=await request.json(); title=data.get("title","").strip(); content=data.get("content","").strip()
    if not title or not content: return web.json_response({"code":400,"msg":"标题和内容不能为空"})
    if len(title)>100: return web.json_response({"code":400,"msg":"标题太长"})
    time_str=datetime.now().strftime("%Y-%m-%d %H:%M"); pid=create_post(username,title,content,time_str)
    return web.json_response({"code":200,"msg":"发布成功","post_id":pid})

async def api_get_posts(request):
    username=get_auth_user(request); page=int(request.query.get("page",1))
    posts=get_posts(page); result=[]
    for p in posts:
        liked=is_post_liked(p[0],username) if username else False
        result.append({"id":p[0],"author":p[1],"title":p[2],"content":p[3],"time":p[4],"likes":p[5],"comment_count":p[6],"liked":liked})
    return web.json_response({"code":200,"data":result})

async def api_get_post_detail(request):
    username=get_auth_user(request); post_id=int(request.query.get("id",0))
    if not post_id: return web.json_response({"code":400,"msg":"缺少帖子ID"})
    post=get_post(post_id)
    if not post: return web.json_response({"code":404,"msg":"帖子不存在"})
    comments=get_comments(post_id); liked=is_post_liked(post_id,username) if username else False
    return web.json_response({"code":200,"data":{"post":{"id":post[0],"author":post[1],"title":post[2],"content":post[3],"time":post[4],"likes":post[5],"comment_count":post[6],"liked":liked},"comments":[{"id":c[0],"author":c[1],"content":c[2],"time":c[3]} for c in comments]}})

async def api_like_post(request):
    username=get_auth_user(request)
    if not username: return web.json_response({"code":401,"msg":"未登录"})
    data=await request.json(); post_id=int(data.get("post_id",0))
    if not post_id: return web.json_response({"code":400,"msg":"缺少帖子ID"})
    liked=like_post(post_id,username); return web.json_response({"code":200,"liked":liked})

async def api_add_comment(request):
    username=get_auth_user(request)
    if not username: return web.json_response({"code":401,"msg":"未登录"})
    data=await request.json(); post_id=int(data.get("post_id",0)); content=data.get("content","").strip()
    if not post_id or not content: return web.json_response({"code":400,"msg":"参数不完整"})
    if len(content)>500: return web.json_response({"code":400,"msg":"评论太长"})
    post=get_post(post_id)
    if not post: return web.json_response({"code":404,"msg":"帖子不存在"})
    time_str=datetime.now().strftime("%H:%M"); cid=add_comment(post_id,username,content,time_str)
    await chat_room.broadcast(json.dumps({"type":"comment_notification","post_id":post_id,"post_title":post[2],"author":username,"content":content[:50],"time":time_str}))
    return web.json_response({"code":200,"msg":"评论成功","comment_id":cid})

# 好友
async def api_search_users(request):
    username=get_auth_user(request)
    if not username: return web.json_response({"code":401,"msg":"未登录"})
    keyword=request.query.get("keyword","").strip()
    if not keyword or len(keyword)<1: return web.json_response({"code":400,"msg":"请输入搜索关键词"})
    users=search_users(keyword,username)
    result=[{"username":u[0],"nickname":u[1],"avatar":u[2],"last_online":u[3],"online":chat_room.is_online(u[0]),"is_friend":are_friends(username,u[0])} for u in users]
    return web.json_response({"code":200,"data":result})

async def api_send_friend_request(request):
    username=get_auth_user(request)
    if not username: return web.json_response({"code":401,"msg":"未登录"})
    data=await request.json(); to_user=data.get("to_user","").strip(); message=data.get("message","").strip()
    if not to_user: return web.json_response({"code":400,"msg":"请输入目标用户"})
    if to_user==username: return web.json_response({"code":400,"msg":"不能添加自己为好友"})
    target=get_user_info(to_user)
    if not target: return web.json_response({"code":404,"msg":"用户不存在"})
    success,msg=send_friend_request(username,to_user,message)
    if not success: return web.json_response({"code":400,"msg":msg})
    if chat_room.is_online(to_user):
        for ws,user_name in chat_room.user_map.items():
            if user_name==to_user:
                try: await ws.send_str(json.dumps({"type":"friend_request","from_user":username,"message":message}))
                except Exception: pass
    return web.json_response({"code":200,"msg":msg})

async def api_get_friend_requests(request):
    username=get_auth_user(request)
    if not username: return web.json_response({"code":401,"msg":"未登录"})
    tab=request.query.get("tab","received")
    rows=get_sent_requests(username) if tab=="sent" else get_received_requests(username)
    result=[{"id":r[0],"from_user":r[1],"to_user":r[2],"message":r[3],"status":r[4],"created_at":r[5],"nickname":r[6] if len(r)>6 else r[1],"avatar":r[7] if len(r)>7 else ""} for r in rows]
    return web.json_response({"code":200,"data":result})

async def api_friend_request_count(request):
    username=get_auth_user(request)
    if not username: return web.json_response({"code":401,"msg":"未登录"})
    conn=sqlite3.connect(str(DB_PATH)); c=conn.cursor()
    c.execute("SELECT COUNT(*) FROM friend_requests WHERE to_user=? AND status='pending'",(username,))
    count=c.fetchone()[0]; conn.close()
    return web.json_response({"code":200,"data":{"count":count}})

async def api_accept_friend_request(request):
    username=get_auth_user(request)
    if not username: return web.json_response({"code":401,"msg":"未登录"})
    data=await request.json(); request_id=int(data.get("request_id",0))
    if not request_id: return web.json_response({"code":400,"msg":"缺少申请ID"})
    success,info=accept_friend_request(request_id,username)
    if not success: return web.json_response({"code":400,"msg":info})
    if chat_room.is_online(info):
        for ws,user_name in chat_room.user_map.items():
            if user_name==info:
                try: await ws.send_str(json.dumps({"type":"friend_accepted","from_user":username}))
                except Exception: pass
    return web.json_response({"code":200,"msg":f"已添加{info}为好友","friend":info})

async def api_reject_friend_request(request):
    username=get_auth_user(request)
    if not username: return web.json_response({"code":401,"msg":"未登录"})
    data=await request.json(); request_id=int(data.get("request_id",0))
    if not request_id: return web.json_response({"code":400,"msg":"缺少申请ID"})
    success,msg,_=reject_friend_request(request_id,username)
    if not success: return web.json_response({"code":400,"msg":msg})
    return web.json_response({"code":200,"msg":msg})

async def api_delete_friend(request):
    username=get_auth_user(request)
    if not username: return web.json_response({"code":401,"msg":"未登录"})
    data=await request.json(); friend=data.get("friend","").strip()
    if not friend: return web.json_response({"code":400,"msg":"缺少好友用户名"})
    deleted=delete_friend(username,friend)
    if not deleted: return web.json_response({"code":400,"msg":"不是好友关系"})
    if chat_room.is_online(friend):
        for ws,user_name in chat_room.user_map.items():
            if user_name==friend:
                try: await ws.send_str(json.dumps({"type":"friend_deleted","from_user":username}))
                except Exception: pass
    return web.json_response({"code":200,"msg":f"已删除好友{friend}"})

async def api_get_friend_list(request):
    username=get_auth_user(request)
    if not username: return web.json_response({"code":401,"msg":"未登录"})
    friends=get_friend_list(username); remarks=get_remarks(username)
    result=[{"username":f[0],"nickname":remarks.get(f[0]) or f[1],"avatar":f[2],"last_online":f[3],"online":chat_room.is_online(f[0])} for f in friends]
    return web.json_response({"code":200,"data":result})

# 游戏大厅API
async def api_game_lobby_info(request):
    username=get_auth_user(request)
    if not username: return web.json_response({"code":401,"msg":"未登录"})
    rooms=game_lobby.get_room_list()
    room_list=[{"room_id":r["room_id"],"room_name":r["room_name"],"game_type":r["game_type"],"game_name":GAME_TYPES.get(r["game_type"],{}).get("name",r["game_type"]),"game_icon":GAME_TYPES.get(r["game_type"],{}).get("icon","🎮"),"owner_id":r["owner_id"],"max_players":r["max_players"],"current_players":r["current_players"],"status":r["status"],"owner_nickname":get_display_name(r["owner_id"],username)} for r in rooms]
    return web.json_response({"code":200,"data":{"online_count":chat_room.online_count,"room_count":len(rooms),"rooms":room_list,"game_types":[{"type":k,"name":v["name"],"icon":v["icon"],"max_players":v["max_players"]} for k,v in GAME_TYPES.items()]}})

async def api_create_room(request):
    username=get_auth_user(request)
    if not username: return web.json_response({"code":401,"msg":"未登录"})
    cr=game_lobby.get_user_room(username)
    if cr: return web.json_response({"code":400,"msg":f"你已在房间「{cr['room_name']}」中，请先离开"})
    data=await request.json(); room_name=data.get("room_name","").strip(); game_type=data.get("game_type","").strip(); max_players=int(data.get("max_players",4))
    if not room_name: return web.json_response({"code":400,"msg":"请输入房间名称"})
    if not game_type or game_type not in GAME_TYPES: return web.json_response({"code":400,"msg":"无效的游戏类型"})
    if max_players<2 or max_players>20: return web.json_response({"code":400,"msg":"玩家数需在2-20之间"})
    room_id=uuid.uuid4().hex[:8]; game_lobby.create_room(room_id,room_name,game_type,username,max_players); db_create_room(room_id,room_name,game_type,username,max_players)
    if game_type=="gomoku": gomoku_manager.get_or_create(room_id).assign_color(username)
    if game_type=="chinese_chess": chess_manager.get_or_create(room_id).assign_color(username)
    if game_type=="doudizhu": ddz=ddz_manager.create_room(room_name); ddz.room_id=room_id; ddz.add_player(username)
    if game_type=="chess": chess_intl_manager.get_or_create(room_id).assign_color(username)
    await broadcast_lobby_update()
    return web.json_response({"code":200,"msg":"房间创建成功","data":{"room_id":room_id}})

async def api_join_room(request):
    username=get_auth_user(request)
    if not username: return web.json_response({"code":401,"msg":"未登录"})
    cr=game_lobby.get_user_room(username)
    if cr: return web.json_response({"code":400,"msg":f"你已在房间「{cr['room_name']}」中，请先离开"})
    data=await request.json(); room_id=data.get("room_id","").strip()
    if not room_id: return web.json_response({"code":400,"msg":"缺少房间ID"})
    success,msg,role=game_lobby.join_room(room_id,username)
    if not success: return web.json_response({"code":400,"msg":msg})
    if role=="player": db_add_player(room_id,username)
    gomoku_state,chess_state,intl_chess_state,ddz_state=None,None,None,None
    if role=="player":
        room=game_lobby.get_room_info(room_id)
        if room:
            gt=room["game_type"]
            if gt=="gomoku": g=gomoku_manager.get_or_create(room_id); g.assign_color(username); gomoku_state=g.to_dict()
            elif gt=="chinese_chess": g=chess_manager.get_or_create(room_id); g.assign_color(username); chess_state=g.to_dict()
            elif gt=="doudizhu": d=ddz_manager.get_room(room_id) or ddz_manager.create_room(room["room_name"])
            if gt=="doudizhu":
                if not ddz_manager.get_room(room_id): d.room_id=room_id
                d.add_player(username); ddz_state=d.to_dict()
            elif gt=="chess": g=chess_intl_manager.get_or_create(room_id); g.assign_color(username); intl_chess_state=g.to_dict()
    extra={"role":role}
    if gomoku_state: extra["gomoku"]=gomoku_state
    if chess_state: extra["chess"]=chess_state
    if intl_chess_state: extra["intl_chess"]=intl_chess_state
    if ddz_state: extra["ddz"]=ddz_state
    await broadcast_room_update(room_id,"player_join",username,**extra); await broadcast_lobby_update()
    return web.json_response({"code":200,"msg":msg,"data":{"room_id":room_id,"role":role}})

async def api_leave_room(request):
    username=get_auth_user(request)
    if not username: return web.json_response({"code":401,"msg":"未登录"})
    data=await request.json(); room_id=data.get("room_id","").strip()
    if not room_id: return web.json_response({"code":400,"msg":"缺少房间ID"})
    room=game_lobby.get_room_info(room_id); was_player=room and username in room["players"] if room else False
    success,msg,new_owner=game_lobby.leave_room(room_id,username)
    if not success: return web.json_response({"code":400,"msg":msg})
    if was_player: db_remove_player(room_id,username)
    if new_owner: db_update_room_owner(room_id,new_owner)
    if was_player and room:
        gt=room["game_type"]
        if gt=="gomoku": g=gomoku_manager.get(room_id); g and g.remove_player(username)
        elif gt=="chinese_chess": g=chess_manager.get(room_id); g and g.remove_player(username)
        elif gt=="doudizhu": d=ddz_manager.get_room(room_id); d and d.remove_player(username)
        elif gt=="chess": g=chess_intl_manager.get(room_id); g and g.remove_player(username)
    if game_lobby.get_room_info(room_id): await broadcast_room_update(room_id,"player_leave",username,new_owner=new_owner)
    await broadcast_lobby_update()
    return web.json_response({"code":200,"msg":msg})

async def api_dismiss_room(request):
    username=get_auth_user(request)
    if not username: return web.json_response({"code":401,"msg":"未登录"})
    data=await request.json(); room_id=data.get("room_id","").strip()
    if not room_id: return web.json_response({"code":400,"msg":"缺少房间ID"})
    success,msg,players=game_lobby.dismiss_room(room_id,username)
    if not success: return web.json_response({"code":400,"msg":msg})
    db_delete_room(room_id); gomoku_manager.remove(room_id); chess_manager.remove(room_id); ddz_manager.remove_room(room_id); chess_intl_manager.remove(room_id)
    await broadcast_room_dismissed(room_id,players); await broadcast_lobby_update()
    return web.json_response({"code":200,"msg":msg})

async def api_game_start(request):
    username=get_auth_user(request)
    if not username: return web.json_response({"code":401,"msg":"未登录"})
    data=await request.json(); room_id=data.get("room_id","").strip()
    if not room_id: return web.json_response({"code":400,"msg":"缺少房间ID"})
    success,msg=game_lobby.start_game(room_id,username)
    if not success: return web.json_response({"code":400,"msg":msg})
    db_update_room_status(room_id,"playing"); await broadcast_room_update(room_id,"game_start",username); await broadcast_lobby_update()
    return web.json_response({"code":200,"msg":msg})

async def api_game_finish(request):
    username=get_auth_user(request)
    if not username: return web.json_response({"code":401,"msg":"未登录"})
    data=await request.json(); room_id=data.get("room_id","").strip()
    if not room_id: return web.json_response({"code":400,"msg":"缺少房间ID"})
    room=game_lobby.get_room_info(room_id)
    if not room: return web.json_response({"code":400,"msg":"房间不存在"})
    if room["owner_id"]!=username: return web.json_response({"code":400,"msg":"只有房主可以结束游戏"})
    game_lobby.finish_game(room_id); db_update_room_status(room_id,"waiting"); await broadcast_room_update(room_id,"game_finish",username); await broadcast_lobby_update()
    return web.json_response({"code":200,"msg":"游戏已结束"})

async def api_get_room_info(request):
    username=get_auth_user(request)
    if not username: return web.json_response({"code":401,"msg":"未登录"})
    room_id=request.query.get("room_id","").strip()
    if not room_id: return web.json_response({"code":400,"msg":"缺少房间ID"})
    room=game_lobby.get_room_info(room_id)
    if not room: return web.json_response({"code":404,"msg":"房间不存在"})
    players=[]
    for p in room["players"]:
        info=get_user_info(p)
        pd={"username":p,"nickname":get_display_name(p,username),"avatar":info[2] if info else "","online":chat_room.is_online(p),"is_owner":p==room["owner_id"]}
        gt=room["game_type"]
        if gt=="gomoku":
            g=gomoku_manager.get_or_create(room_id); color=g.get_color(p)
            if color: pd["gomoku_color"]=color; pd["ready"]=(color==BLACK and g.black_ready) or (color==WHITE and g.white_ready)
        elif gt=="chinese_chess":
            g=chess_manager.get_or_create(room_id); color=g.get_color(p)
            if color: pd["chess_color"]=color; pd["ready"]=(color==CC_RED and g.red_ready) or (color==CC_BLACK and g.black_ready)
        elif gt=="chess":
            g=chess_intl_manager.get_or_create(room_id); color=g.get_color(p)
            if color: pd["intl_chess_color"]=color; pd["ready"]=(color=='white' and g.white_ready) or (color=='black' and g.black_ready)
        players.append(pd)
    result={"code":200,"data":{"room_id":room["room_id"],"room_name":room["room_name"],"game_type":room["game_type"],"game_name":GAME_TYPES.get(room["game_type"],{}).get("name",room["game_type"]),"game_icon":GAME_TYPES.get(room["game_type"],{}).get("icon","🎮"),"owner_id":room["owner_id"],"max_players":room["max_players"],"current_players":room["current_players"],"status":room["status"],"players":players,"spectator_count":len(room.get("spectators",set()))}}
    gt=room["game_type"]
    if gt=="gomoku": result["data"]["gomoku"]=gomoku_manager.get_or_create(room_id).to_dict()
    elif gt=="chinese_chess": result["data"]["chess"]=chess_manager.get_or_create(room_id).to_dict()
    elif gt=="doudizhu":
        d=ddz_manager.get_room(room_id)
        if d: result["data"]["ddz"]=d.to_dict(); result["data"]["ddz_private"]=d.get_player_state(username)
    elif gt=="chess": result["data"]["intl_chess"]=chess_intl_manager.get_or_create(room_id).to_dict()
    return web.json_response(result)

# 五子棋API
async def api_gomoku_ready(request):
    username=get_auth_user(request)
    if not username: return web.json_response({"code":401,"msg":"未登录"})
    data=await request.json(); room_id=data.get("room_id","").strip()
    if not room_id: return web.json_response({"code":400,"msg":"缺少房间ID"})
    room=game_lobby.get_room_info(room_id)
    if not room or room["game_type"]!="gomoku": return web.json_response({"code":400,"msg":"无效的五子棋房间"})
    if username not in room["players"]: return web.json_response({"code":400,"msg":"你不在该房间中"})
    g=gomoku_manager.get_or_create(room_id); g.assign_color(username)
    success,both_ready=g.set_ready(username)
    if not success: return web.json_response({"code":400,"msg":"当前状态不能准备"})
    await broadcast_to_room(room_id,{"type":"gomoku_state","room_id":room_id,"event":"ready_update","username":username,**g.to_dict()})
    if both_ready:
        room["status"]="playing"; g._start_game(); db_update_room_status(room_id,"playing")
        await broadcast_to_room(room_id,{"type":"gomoku_state","room_id":room_id,"event":"game_started","username":"",**g.to_dict()})
        await broadcast_room_update(room_id,"game_start",username); await broadcast_lobby_update()
    return web.json_response({"code":200,"msg":"准备成功" if not both_ready else "双方已准备，游戏开始","data":g.to_dict()})

async def api_gomoku_move(request):
    username=get_auth_user(request)
    if not username: return web.json_response({"code":401,"msg":"未登录"})
    data=await request.json(); room_id=data.get("room_id","").strip(); x=data.get("x"); y=data.get("y")
    if not room_id or x is None or y is None: return web.json_response({"code":400,"msg":"参数不完整"})
    g=gomoku_manager.get(room_id)
    if not g: return web.json_response({"code":400,"msg":"五子棋对局不存在"})
    if g.status!="playing": return web.json_response({"code":400,"msg":"游戏未开始"})
    success,msg,winner=g.make_move(username,int(x),int(y))
    if not success: return web.json_response({"code":400,"msg":msg})
    color=g.get_color(username)
    await broadcast_to_room(room_id,{"type":"gomoku_state","room_id":room_id,"event":"move","username":username,"x":x,"y":y,"color":color,**g.to_dict()})
    if winner or msg=="平局":
        room=game_lobby.get_room_info(room_id)
        if room: room["status"]="waiting"
        g.reset_game(); db_update_room_status(room_id,"waiting")
        await broadcast_to_room(room_id,{"type":"gomoku_state","room_id":room_id,"event":"game_over","username":username,"winner":winner,"is_draw":msg=="平局"})
        await broadcast_room_update(room_id,"game_finish",username); await broadcast_lobby_update()
    return web.json_response({"code":200,"msg":msg,"data":{"winner":winner,**g.to_dict()}})

async def api_gomoku_reset(request):
    username=get_auth_user(request)
    if not username: return web.json_response({"code":401,"msg":"未登录"})
    data=await request.json(); room_id=data.get("room_id","").strip()
    if not room_id: return web.json_response({"code":400,"msg":"缺少房间ID"})
    g=gomoku_manager.get(room_id)
    if not g: return web.json_response({"code":400,"msg":"五子棋对局不存在"})
    room=game_lobby.get_room_info(room_id)
    if not room or username not in room["players"]: return web.json_response({"code":400,"msg":"你不在该房间中"})
    g.reset_game(); room["status"]="waiting"; db_update_room_status(room_id,"waiting")
    await broadcast_to_room(room_id,{"type":"gomoku_state","room_id":room_id,"event":"game_reset","username":username,**g.to_dict()})
    await broadcast_room_update(room_id,"game_finish",username); await broadcast_lobby_update()
    return web.json_response({"code":200,"msg":"游戏已重置","data":g.to_dict()})

async def api_gomoku_state(request):
    username=get_auth_user(request)
    if not username: return web.json_response({"code":401,"msg":"未登录"})
    room_id=request.query.get("room_id","").strip()
    if not room_id: return web.json_response({"code":400,"msg":"缺少房间ID"})
    g=gomoku_manager.get(room_id)
    if not g: return web.json_response({"code":404,"msg":"五子棋对局不存在"})
    return web.json_response({"code":200,"data":g.to_dict()})

# 中国象棋API
async def api_chess_ready(request):
    username=get_auth_user(request)
    if not username: return web.json_response({"code":401,"msg":"未登录"})
    data=await request.json(); room_id=data.get("room_id","").strip()
    if not room_id: return web.json_response({"code":400,"msg":"缺少房间ID"})
    g=chess_manager.get(room_id)
    if not g: return web.json_response({"code":400,"msg":"象棋对局不存在"})
    room=game_lobby.get_room_info(room_id)
    if not room or username not in room["players"]: return web.json_response({"code":400,"msg":"你不在该房间中"})
    color=g.get_color(username)
    if not color: return web.json_response({"code":400,"msg":"你未分配到颜色"})
    is_ready=(color==CC_RED and g.red_ready) or (color==CC_BLACK and g.black_ready)
    if is_ready: g.unready(username); await broadcast_room_update(room_id,"ready_update",username); await broadcast_lobby_update(); return web.json_response({"code":200,"msg":"已取消准备","data":g.to_dict()})
    success,both_ready=g.set_ready(username)
    if not success: return web.json_response({"code":400,"msg":"准备失败"})
    await broadcast_room_update(room_id,"ready_update",username); await broadcast_lobby_update()
    if both_ready: db_chess_start(room_id,g.player_red,g.player_black); await broadcast_to_room(room_id,{"type":"chess_state","room_id":room_id,"event":"game_start",**g.to_dict()}); await broadcast_room_update(room_id,"game_start",username); await broadcast_lobby_update()
    return web.json_response({"code":200,"msg":"准备成功","data":g.to_dict()})

async def api_chess_move(request):
    username=get_auth_user(request)
    if not username: return web.json_response({"code":401,"msg":"未登录"})
    data=await request.json(); room_id=data.get("room_id","").strip(); fr=data.get("from_row"); fc=data.get("from_col"); tr=data.get("to_row"); tc=data.get("to_col")
    if not room_id or fr is None or fc is None or tr is None or tc is None: return web.json_response({"code":400,"msg":"缺少参数"})
    g=chess_manager.get(room_id)
    if not g: return web.json_response({"code":400,"msg":"象棋对局不存在"})
    color=g.get_color(username); success,msg,extra=g.make_move(username,int(fr),int(fc),int(tr),int(tc))
    if not success: return web.json_response({"code":400,"msg":msg})
    await broadcast_to_room(room_id,{"type":"chess_state","room_id":room_id,"event":"move","username":username,"from_row":fr,"from_col":fc,"to_row":tr,"to_col":tc,"color":color,"is_check":extra.get("is_check",False),"is_checkmate":extra.get("is_checkmate",False),**g.to_dict()})
    if extra.get("is_check") and not extra.get("is_checkmate"):
        opp=CC_BLACK if color==CC_RED else CC_RED; oname=g.player_red if opp==CC_RED else g.player_black
        await broadcast_to_room(room_id,{"type":"chess_state","room_id":room_id,"event":"check","username":oname,**g.to_dict()})
    if extra.get("winner"):
        db_chess_end(room_id,extra["winner"],g.move_count); room=game_lobby.get_room_info(room_id)
        if room: room["status"]="waiting"
        await broadcast_to_room(room_id,{"type":"chess_state","room_id":room_id,"event":"checkmate","username":username,"winner":extra["winner"],**g.to_dict()})
        await broadcast_room_update(room_id,"game_finish",username); await broadcast_lobby_update()
    return web.json_response({"code":200,"msg":"落子成功","data":g.to_dict()})

async def api_chess_state(request):
    username=get_auth_user(request)
    if not username: return web.json_response({"code":401,"msg":"未登录"})
    room_id=request.query.get("room_id","").strip()
    if not room_id: return web.json_response({"code":400,"msg":"缺少房间ID"})
    g=chess_manager.get(room_id)
    if not g: return web.json_response({"code":404,"msg":"象棋对局不存在"})
    return web.json_response({"code":200,"data":g.to_dict()})

async def api_chess_undo_request(request):
    username=get_auth_user(request)
    if not username: return web.json_response({"code":401,"msg":"未登录"})
    data=await request.json(); room_id=data.get("room_id","").strip()
    g=chess_manager.get(room_id)
    if not g: return web.json_response({"code":400,"msg":"象棋对局不存在"})
    # Simple undo: broadcast request
    opponent=g.player_red if g.player_black==username else g.player_black
    for ws,user_name in chat_room.user_map.items():
        if user_name==opponent: await ws.send_str(json.dumps({"type":"chess_state","room_id":room_id,"event":"undo_request","username":username})); break
    return web.json_response({"code":200,"msg":"悔棋申请已发送"})

async def api_chess_undo_agree(request):
    username=get_auth_user(request)
    if not username: return web.json_response({"code":401,"msg":"未登录"})
    data=await request.json(); room_id=data.get("room_id","").strip()
    g=chess_manager.get(room_id)
    if not g: return web.json_response({"code":400,"msg":"象棋对局不存在"})
    # Simple undo: reverse last 2 moves
    if g.move_history: g.move_history.pop()
    if g.move_history:
        last=g.move_history.pop()
        g.board[last["from"][0]][last["from"][1]]=g.board[last["to"][0]][last["to"][1]]
        g.board[last["to"][0]][last["to"][1]]=last["captured"]
        g.current_turn=last["color"]
    await broadcast_to_room(room_id,{"type":"chess_state","room_id":room_id,"event":"undo_done","username":username,**g.to_dict()})
    return web.json_response({"code":200,"msg":"悔棋成功","data":g.to_dict()})

async def api_chess_reset(request):
    username=get_auth_user(request)
    if not username: return web.json_response({"code":401,"msg":"未登录"})
    data=await request.json(); room_id=data.get("room_id","").strip()
    g=chess_manager.get(room_id)
    if not g: return web.json_response({"code":400,"msg":"象棋对局不存在"})
    room=game_lobby.get_room_info(room_id)
    if not room or username not in room["players"]: return web.json_response({"code":400,"msg":"你不在该房间中"})
    g.reset_game(); room["status"]="waiting"; db_update_room_status(room_id,"waiting")
    await broadcast_to_room(room_id,{"type":"chess_state","room_id":room_id,"event":"game_reset","username":username,**g.to_dict()})
    await broadcast_room_update(room_id,"game_finish",username); await broadcast_lobby_update()
    return web.json_response({"code":200,"msg":"游戏已重置","data":g.to_dict()})

# 斗地主API
async def api_ddz_ready(request):
    username=get_auth_user(request)
    if not username: return web.json_response({"code":401,"msg":"未登录"})
    data=await request.json(); room_id=data.get("room_id","").strip()
    if not room_id: return web.json_response({"code":400,"msg":"缺少房间ID"})
    room=game_lobby.get_room_info(room_id)
    if not room or room["game_type"]!="doudizhu": return web.json_response({"code":400,"msg":"无效的斗地主房间"})
    d=ddz_manager.get_room(room_id)
    if not d: d=ddz_manager.create_room(room["room_name"]); d.room_id=room_id
    d.set_ready(username)
    await broadcast_to_room(room_id,{"type":"ddz_state","room_id":room_id,"event":"ready_update","state":d.to_dict()})
    if d.all_players_ready() and d.phase=='waiting':
        d.start_game()
        await broadcast_to_room(room_id,{"type":"ddz_state","room_id":room_id,"event":"game_start","state":d.to_dict()})
        for p in d.players:
            ps=d.get_player_state(p.username); await send_to_user(p.username,{"type":"ddz_private","room_id":room_id,"state":ps})
        await broadcast_lobby_update()
    return web.json_response({"code":200,"msg":"ok","data":d.to_dict()})

async def api_ddz_bid(request):
    username=get_auth_user(request)
    if not username: return web.json_response({"code":401,"msg":"未登录"})
    data=await request.json(); room_id=data.get("room_id","").strip(); score=int(data.get("score",0))
    d=ddz_manager.get_room(room_id)
    if not d: return web.json_response({"code":400,"msg":"房间不存在"})
    success,result=d.process_bid(username,score)
    if not success: return web.json_response({"code":400,"msg":result})
    action=result.get('action','')
    if action=='landlord':
        await broadcast_to_room(room_id,{"type":"ddz_state","room_id":room_id,"event":"landlord_determined","state":d.to_dict()})
        for p in d.players: ps=d.get_player_state(p.username); await send_to_user(p.username,{"type":"ddz_private","room_id":room_id,"state":ps})
    elif action=='redeal':
        await broadcast_to_room(room_id,{"type":"ddz_state","room_id":room_id,"event":"redeal","state":d.to_dict()})
        for p in d.players: ps=d.get_player_state(p.username); await send_to_user(p.username,{"type":"ddz_private","room_id":room_id,"state":ps})
    else: await broadcast_to_room(room_id,{"type":"ddz_state","room_id":room_id,"event":"bid_update","state":d.to_dict()})
    return web.json_response({"code":200,"msg":"ok","data":d.to_dict()})

async def api_ddz_play(request):
    username=get_auth_user(request)
    if not username: return web.json_response({"code":401,"msg":"未登录"})
    data=await request.json(); room_id=data.get("room_id","").strip(); card_indices=data.get("card_indices",[])
    d=ddz_manager.get_room(room_id)
    if not d: return web.json_response({"code":400,"msg":"房间不存在"})
    success,msg,extra=d.process_play(username,card_indices)
    if not success: return web.json_response({"code":400,"msg":msg})
    if d.phase=='settlement':
        settlement=calculate_settlement(d)
        await broadcast_to_room(room_id,{"type":"ddz_state","room_id":room_id,"event":"game_over","state":d.to_dict(),"settlement":settlement})
        room=game_lobby.get_room_info(room_id)
        if room: room["status"]="waiting"
        await broadcast_lobby_update()
    else: await broadcast_to_room(room_id,{"type":"ddz_state","room_id":room_id,"event":"cards_played","state":d.to_dict()})
    return web.json_response({"code":200,"msg":msg,"data":d.to_dict()})

async def api_ddz_pass(request):
    username=get_auth_user(request)
    if not username: return web.json_response({"code":401,"msg":"未登录"})
    data=await request.json(); room_id=data.get("room_id","").strip()
    d=ddz_manager.get_room(room_id)
    if not d: return web.json_response({"code":400,"msg":"房间不存在"})
    success,msg=d.process_pass(username)
    if not success: return web.json_response({"code":400,"msg":msg})
    await broadcast_to_room(room_id,{"type":"ddz_state","room_id":room_id,"event":"player_passed","state":d.to_dict()})
    return web.json_response({"code":200,"msg":msg,"data":d.to_dict()})

async def api_ddz_state(request):
    username=get_auth_user(request)
    if not username: return web.json_response({"code":401,"msg":"未登录"})
    room_id=request.query.get("room_id","").strip()
    d=ddz_manager.get_room(room_id)
    if not d: return web.json_response({"code":404,"msg":"房间不存在"})
    return web.json_response({"code":200,"data":d.to_dict(),"private":d.get_player_state(username)})

async def api_ddz_reset(request):
    username=get_auth_user(request)
    if not username: return web.json_response({"code":401,"msg":"未登录"})
    data=await request.json(); room_id=data.get("room_id","").strip()
    d=ddz_manager.get_room(room_id)
    if not d: return web.json_response({"code":400,"msg":"房间不存在"})
    d.reset_game(); room=game_lobby.get_room_info(room_id)
    if room: room["status"]="waiting"
    await broadcast_to_room(room_id,{"type":"ddz_state","room_id":room_id,"event":"game_reset","state":d.to_dict()}); await broadcast_lobby_update()
    return web.json_response({"code":200,"msg":"游戏已重置"})

# 国际象棋API
async def api_intl_chess_ready(request):
    username=get_auth_user(request)
    if not username: return web.json_response({"code":401,"msg":"未登录"})
    data=await request.json(); room_id=data.get("room_id","").strip()
    if not room_id: return web.json_response({"code":400,"msg":"缺少房间ID"})
    g=chess_intl_manager.get_or_create(room_id)
    is_ready=(g.white_ready and g.get_color(username)=='white') or (g.black_ready and g.get_color(username)=='black')
    if is_ready: g.unready(username); await broadcast_room_update(room_id,"ready_update",username); return web.json_response({"code":200,"msg":"已取消准备","data":g.to_dict()})
    success,both_ready=g.set_ready(username)
    if not success: return web.json_response({"code":400,"msg":"准备失败"})
    if both_ready: await broadcast_to_room(room_id,{"type":"intl_chess_state","room_id":room_id,"event":"game_start","state":g.to_dict()}); await broadcast_room_update(room_id,"game_start",username); await broadcast_lobby_update()
    else: await broadcast_room_update(room_id,"ready_update",username)
    return web.json_response({"code":200,"msg":"准备成功","data":g.to_dict()})

async def api_intl_chess_move(request):
    username=get_auth_user(request)
    if not username: return web.json_response({"code":401,"msg":"未登录"})
    data=await request.json(); room_id=data.get("room_id","").strip(); fsq=data.get("from",""); tsq=data.get("to",""); prom=data.get("promotion",None)
    if not room_id or not fsq or not tsq: return web.json_response({"code":400,"msg":"参数不完整"})
    g=chess_intl_manager.get(room_id)
    if not g: return web.json_response({"code":400,"msg":"对局不存在"})
    success,msg,extra=g.make_move(username,fsq,tsq,prom)
    if not success: return web.json_response({"code":400,"msg":msg,"data":extra})
    await broadcast_to_room(room_id,{"type":"intl_chess_state","room_id":room_id,"event":"move","username":username,"state":g.to_dict(),"extra":extra})
    if g.status=='finished':
        room=game_lobby.get_room_info(room_id)
        if room: room["status"]="waiting"
        await broadcast_to_room(room_id,{"type":"intl_chess_state","room_id":room_id,"event":"game_over","state":g.to_dict(),"winner":g.winner})
        await broadcast_room_update(room_id,"game_finish",username); await broadcast_lobby_update()
    return web.json_response({"code":200,"msg":msg,"data":g.to_dict()})

async def api_intl_chess_state(request):
    username=get_auth_user(request)
    if not username: return web.json_response({"code":401,"msg":"未登录"})
    room_id=request.query.get("room_id","").strip()
    g=chess_intl_manager.get(room_id)
    if not g: return web.json_response({"code":404,"msg":"对局不存在"})
    return web.json_response({"code":200,"data":g.to_dict()})

async def api_intl_chess_reset(request):
    username=get_auth_user(request)
    if not username: return web.json_response({"code":401,"msg":"未登录"})
    data=await request.json(); room_id=data.get("room_id","").strip()
    g=chess_intl_manager.get(room_id)
    if not g: return web.json_response({"code":400,"msg":"对局不存在"})
    room=game_lobby.get_room_info(room_id)
    if not room or username not in room["players"]: return web.json_response({"code":400,"msg":"你不在该房间中"})
    g.reset_game()
    if room: room["status"]="waiting"
    await broadcast_to_room(room_id,{"type":"intl_chess_state","room_id":room_id,"event":"game_reset","username":username,"state":g.to_dict()})
    await broadcast_room_update(room_id,"game_finish",username); await broadcast_lobby_update()
    return web.json_response({"code":200,"msg":"游戏已重置","data":g.to_dict()})

# 邀请API
async def api_game_invite(request):
    username=get_auth_user(request)
    if not username: return web.json_response({"code":401,"msg":"未登录"})
    data=await request.json(); room_id=data.get("room_id","").strip(); invite_user=data.get("invite_user","").strip()
    if not room_id or not invite_user: return web.json_response({"code":400,"msg":"参数不完整"})
    if invite_user==username: return web.json_response({"code":400,"msg":"不能邀请自己"})
    room=game_lobby.get_room_info(room_id)
    if not room: return web.json_response({"code":400,"msg":"房间不存在"})
    if username not in room["players"]: return web.json_response({"code":400,"msg":"你不在该房间中"})
    if not chat_room.is_online(invite_user): return web.json_response({"code":400,"msg":f"{invite_user}不在线"})
    if not are_friends(username,invite_user): return web.json_response({"code":400,"msg":"只能邀请好友"})
    await send_to_user(invite_user,{"type":"game_invite","room_id":room_id,"room_name":room["room_name"],"game_type":room["game_type"],"game_name":GAME_TYPES.get(room["game_type"],{}).get("name",room["game_type"]),"inviter":username})
    return web.json_response({"code":200,"msg":f"邀请已发送给{invite_user}"})

# ==================== 广播辅助 ====================
async def broadcast_lobby_update():
    await chat_room.broadcast(json.dumps({"type":"lobby_update","room_count":len(game_lobby.rooms),"online_count":chat_room.online_count}))

async def broadcast_room_update(room_id,event,username,**extra):
    room=game_lobby.get_room_info(room_id)
    if not room: return
    gt=room["game_type"]
    if gt=="gomoku" and "gomoku" not in extra: g=gomoku_manager.get(room_id); g and extra.update({"gomoku":g.to_dict()})
    if gt=="chinese_chess" and "chess" not in extra: g=chess_manager.get(room_id); g and extra.update({"chess":g.to_dict()})
    if gt=="doudizhu" and "ddz" not in extra: d=ddz_manager.get_room(room_id); d and extra.update({"ddz":d.to_dict()})
    if gt=="chess" and "intl_chess" not in extra: g=chess_intl_manager.get(room_id); g and extra.update({"intl_chess":g.to_dict()})
    msg=json.dumps({"type":"room_update","room_id":room_id,"event":event,"username":username,"current_players":room["current_players"],"status":room["status"],"owner_id":room["owner_id"],**extra})
    for ws,user_name in chat_room.user_map.items():
        if user_name in game_lobby.get_all_users_in_room(room_id):
            try: await ws.send_str(msg)
            except Exception: pass

async def broadcast_to_room(room_id,msg):
    msg_json=json.dumps(msg)
    for ws,user_name in chat_room.user_map.items():
        if user_name in game_lobby.get_all_users_in_room(room_id):
            try: await ws.send_str(msg_json)
            except Exception: pass

async def broadcast_room_dismissed(room_id,players):
    msg=json.dumps({"type":"room_dismissed","room_id":room_id})
    for ws,user_name in chat_room.user_map.items():
        if user_name in players:
            try: await ws.send_str(msg)
            except Exception: pass

async def send_to_user(username,msg):
    msg_json=json.dumps(msg)
    for ws,user_name in chat_room.user_map.items():
        if user_name==username:
            try: await ws.send_str(msg_json)
            except Exception: pass

async def broadcast_friend_status(username,status):
    conn=sqlite3.connect(str(DB_PATH)); c=conn.cursor()
    c.execute("SELECT CASE WHEN user1=? THEN user2 ELSE user1 END FROM friends WHERE user1=? OR user2=?",(username,username,username))
    friends=[row[0] for row in c.fetchall()]; conn.close()
    msg=json.dumps({"type":"friend_status","username":username,"status":status})
    for ws,user_name in chat_room.user_map.items():
        if user_name in friends:
            try: await ws.send_str(msg)
            except Exception: pass

# ==================== WebSocket ====================
async def handle_game_ws_message(username,data):
    sub_type=data.get("sub_type",""); room_id=data.get("room_id","")
    room=game_lobby.get_room_info(room_id); all_users=game_lobby.get_all_users_in_room(room_id) if room else set()
    if not room or username not in all_users: return
    if sub_type=="gomoku_move":
        x,y=data.get("x"),data.get("y")
        if x is None or y is None: return
        g=gomoku_manager.get(room_id)
        if not g or g.status!="playing": return
        success,msg,winner=g.make_move(username,int(x),int(y))
        if not success:
            for ws,user_name in chat_room.user_map.items():
                if user_name==username: await ws.send_str(json.dumps({"type":"gomoku_error","room_id":room_id,"msg":msg})); break
            return
        color=g.get_color(username)
        await broadcast_to_room(room_id,{"type":"gomoku_state","room_id":room_id,"event":"move","username":username,"x":x,"y":y,"color":color,**g.to_dict()})
        if winner or msg=="平局":
            if room: room["status"]="waiting"
            g.reset_game(); db_update_room_status(room_id,"waiting")
            await broadcast_to_room(room_id,{"type":"gomoku_state","room_id":room_id,"event":"game_over","username":username,"winner":winner,"is_draw":msg=="平局"})
            await broadcast_room_update(room_id,"game_finish",username); await broadcast_lobby_update()
    elif sub_type=="chess_move":
        fr,fc=data.get("from_row"),data.get("from_col"); tr,tc=data.get("to_row"),data.get("to_col")
        if fr is None: return
        g=chess_manager.get(room_id)
        if not g or g.status!="playing": return
        color=g.get_color(username); success,msg,extra=g.make_move(username,int(fr),int(fc),int(tr),int(tc))
        if not success:
            for ws,user_name in chat_room.user_map.items():
                if user_name==username: await ws.send_str(json.dumps({"type":"chess_error","room_id":room_id,"msg":msg})); break
            return
        await broadcast_to_room(room_id,{"type":"chess_state","room_id":room_id,"event":"move","username":username,"from_row":fr,"from_col":fc,"to_row":tr,"to_col":tc,"color":color,**g.to_dict(),"is_check":extra.get("is_check",False)})
        if extra.get("winner"):
            db_chess_end(room_id,extra["winner"],g.move_count)
            if room: room["status"]="waiting"
            db_update_room_status(room_id,"waiting")
            await broadcast_to_room(room_id,{"type":"chess_state","room_id":room_id,"event":"checkmate","username":username,"winner":extra["winner"],**g.to_dict()})
            await broadcast_room_update(room_id,"game_finish",username); await broadcast_lobby_update()
    else:
        forward={"type":"game_message","username":username,"time":datetime.now().strftime("%H:%M:%S")}; forward.update(data)
        msg=json.dumps(forward)
        for ws,user_name in chat_room.user_map.items():
            if user_name in all_users:
                try: await ws.send_str(msg)
                except Exception: pass

async def auto_leave_game_rooms(username):
    room,role=game_lobby.get_user_in_room(username)
    if not room: return
    room_id=room["room_id"]; was_player=role=="player"
    success,msg,new_owner=game_lobby.leave_room(room_id,username)
    if not success: return
    if was_player:
        db_remove_player(room_id,username)
        gt=room["game_type"]
        if gt=="gomoku": g=gomoku_manager.get(room_id); g and g.remove_player(username)
        elif gt=="chinese_chess": g=chess_manager.get(room_id); g and g.remove_player(username)
        elif gt=="doudizhu": d=ddz_manager.get_room(room_id); d and d.remove_player(username)
        elif gt=="chess": g=chess_intl_manager.get(room_id); g and g.remove_player(username)
    if new_owner: db_update_room_owner(room_id,new_owner)
    cr=game_lobby.get_room_info(room_id)
    if cr: await broadcast_room_update(room_id,"player_leave",username,new_owner=new_owner)
    await broadcast_lobby_update()

async def websocket_handler(request):
    ws=web.WebSocketResponse(); await ws.prepare(request); client_ip=get_client_ip(request)
    print(f"[连接] 新的WebSocket连接 (IP: {client_ip})")
    await handle_chat(ws,client_ip); return ws

async def handle_chat(websocket,client_ip='unknown'):
    username=None
    try:
        try:
            first_msg=await asyncio.wait_for(websocket.receive(),timeout=30)
            if first_msg.type!=web.WSMsgType.TEXT: return
            data=json.loads(first_msg.data); token=data.get("token","")
            username=chat_room.tokens.get(token)
            if not username: username=get_session_user(token)
            if username and token not in chat_room.tokens: chat_room.tokens[token]=username
            if not username:
                await websocket.send_str(json.dumps({"type":"error","content":"认证失败，请重新登录"})); return
        except asyncio.TimeoutError: return
        # 会话互斥：踢掉旧连接
        for old_ws,old_user in list(chat_room.user_map.items()):
            if old_user==username:
                try: await old_ws.send_str(json.dumps({"type":"kicked","content":"您的账号已在别处登录，当前连接被踢下线"}))
                except Exception: pass
                try: await old_ws.close()
                except Exception: pass
                await chat_room.remove_client(old_ws)
                print(f"[!] 踢掉旧连接: {username}")
        await chat_room.add_client(websocket,username)
        print(f"[+] {username} 加入聊天室 (IP: {client_ip})，当前在线: {chat_room.online_count}")
        await chat_room.broadcast(json.dumps({"type":"system","content":f"{username} 进入聊天室","time":datetime.now().strftime("%H:%M:%S"),"online_count":chat_room.online_count}))
        await broadcast_friend_status(username,"online")
        async for raw_msg in websocket:
            try:
                if raw_msg.type!=web.WSMsgType.TEXT:
                    if raw_msg.type in(web.WSMsgType.CLOSE,web.WSMsgType.ERROR): break
                    continue
                msg_data=json.loads(raw_msg.data); msg_type=msg_data.get("type","text"); content=msg_data.get("content","").strip(); time_str=datetime.now().strftime("%H:%M:%S")
                if msg_type=="game_message": await handle_game_ws_message(username,msg_data); continue
                if not content and msg_type=="text": continue
                channel_id=msg_data.get("channel_id","general")
                reply_to=msg_data.get("reply_to")  # {id, content, username}
                reply_to_id=reply_to.get("id") if reply_to else None
                reply_to_content=reply_to.get("content","") if reply_to else ""
                reply_to_username=reply_to.get("username","") if reply_to else ""
                msg_json=json.dumps({"type":msg_type,"username":username,"content":content,"filename":msg_data.get("filename",""),"time":time_str,"channel_id":channel_id,"reply_to":reply_to})
                await chat_room.broadcast(msg_json)
                save_message(username,msg_type,content,time_str,msg_data.get("filename",""),channel_id,reply_to_id,reply_to_content,reply_to_username)
            except json.JSONDecodeError: continue
    except Exception as e: print(f"WebSocket错误: {e}")
    finally:
        if username:
            leave_user=await chat_room.remove_client(websocket)
            if leave_user:
                print(f"[-] {leave_user} 离开 (IP: {client_ip})，当前在线: {chat_room.online_count}")
                await chat_room.broadcast(json.dumps({"type":"system","content":f"{leave_user} 离开聊天室","time":datetime.now().strftime("%H:%M:%S"),"online_count":chat_room.online_count}))
                if not chat_room.is_online(leave_user): await broadcast_friend_status(leave_user,"offline"); await auto_leave_game_rooms(leave_user)

# ==================== 页面路由 ====================
async def web_index(request):
    html_path=BASE_DIR/"frontend"/"index.html"
    if not html_path.exists(): return web.Response(text="index.html not found",status=404)
    return web.Response(text=open(html_path,"r",encoding="utf-8").read(),content_type="text/html")

async def web_games(request):
    html_path=BASE_DIR/"frontend"/"games.html"
    if not html_path.exists(): return web.Response(text="games.html not found",status=404)
    return web.Response(text=open(html_path,"r",encoding="utf-8").read(),content_type="text/html")

# ==================== 主函数 ====================
async def main():
    init_db(); load_sessions(); print("数据库初始化完成")
    app=web.Application()
    # 页面
    app.router.add_get("/",web_index); app.router.add_get("/games",web_games)
    # 认证
    app.router.add_post("/api/register",api_register); app.router.add_post("/api/login",api_login); app.router.add_post("/api/logout",api_logout)
    app.router.add_get("/api/check_login",api_check_login); app.router.add_get("/api/server_info",api_server_info)
    # 上传/历史
    app.router.add_post("/api/upload",api_upload); app.router.add_get("/api/history",api_history)
    # 私聊
    app.router.add_post("/api/private/send",api_send_private); app.router.add_get("/api/private/history",api_get_private_history)
    app.router.add_get("/api/private/chats",api_get_chat_list); app.router.add_get("/api/users",api_get_all_users)
    app.router.add_post("/api/user/remark",api_set_remark); app.router.add_get("/api/user/remarks",api_get_remarks)
    # 频道
    app.router.add_get("/api/channels",api_get_channels); app.router.add_post("/api/channel/create",api_create_channel)
    app.router.add_post("/api/channel/delete",api_delete_channel)
    # 帖子
    app.router.add_post("/api/post/create",api_create_post); app.router.add_get("/api/posts",api_get_posts)
    app.router.add_get("/api/post/detail",api_get_post_detail); app.router.add_post("/api/post/like",api_like_post)
    app.router.add_post("/api/comment/add",api_add_comment)
    # 好友
    app.router.add_get("/api/friend/search",api_search_users); app.router.add_post("/api/friend/request",api_send_friend_request)
    app.router.add_get("/api/friend/requests",api_get_friend_requests); app.router.add_get("/api/friend/request_count",api_friend_request_count)
    app.router.add_post("/api/friend/accept",api_accept_friend_request); app.router.add_post("/api/friend/reject",api_reject_friend_request)
    app.router.add_post("/api/friend/delete",api_delete_friend); app.router.add_get("/api/friend/list",api_get_friend_list)
    # 游戏大厅
    app.router.add_get("/api/game/lobby",api_game_lobby_info); app.router.add_post("/api/game/room/create",api_create_room)
    app.router.add_post("/api/game/room/join",api_join_room); app.router.add_post("/api/game/room/leave",api_leave_room)
    app.router.add_post("/api/game/room/dismiss",api_dismiss_room); app.router.add_post("/api/game/start",api_game_start)
    app.router.add_post("/api/game/finish",api_game_finish); app.router.add_get("/api/game/room/info",api_get_room_info)
    app.router.add_post("/api/game/invite",api_game_invite)
    # 五子棋
    app.router.add_post("/api/gomoku/ready",api_gomoku_ready); app.router.add_post("/api/gomoku/move",api_gomoku_move)
    app.router.add_post("/api/gomoku/reset",api_gomoku_reset); app.router.add_get("/api/gomoku/state",api_gomoku_state)
    # 中国象棋
    app.router.add_post("/api/chess/ready",api_chess_ready); app.router.add_post("/api/chess/move",api_chess_move)
    app.router.add_get("/api/chess/state",api_chess_state); app.router.add_post("/api/chess/undo/request",api_chess_undo_request)
    app.router.add_post("/api/chess/undo/agree",api_chess_undo_agree); app.router.add_post("/api/chess/reset",api_chess_reset)
    # 斗地主
    app.router.add_post("/api/ddz/ready",api_ddz_ready); app.router.add_post("/api/ddz/bid",api_ddz_bid)
    app.router.add_post("/api/ddz/play",api_ddz_play); app.router.add_post("/api/ddz/pass",api_ddz_pass)
    app.router.add_get("/api/ddz/state",api_ddz_state); app.router.add_post("/api/ddz/reset",api_ddz_reset)
    # 国际象棋
    app.router.add_post("/api/intl_chess/ready",api_intl_chess_ready); app.router.add_post("/api/intl_chess/move",api_intl_chess_move)
    app.router.add_get("/api/intl_chess/state",api_intl_chess_state); app.router.add_post("/api/intl_chess/reset",api_intl_chess_reset)
    # WebSocket + 静态
    app.router.add_get("/ws",websocket_handler); app.router.add_static("/uploads/",UPLOAD_DIR,show_index=True)
    runner=web.AppRunner(app); await runner.setup()
    http_site=web.TCPSite(runner,HOST,PORT); await http_site.start()
    lb=f"http://localhost:{PORT}"; lw=f"ws://localhost:{PORT}/ws"; pb=PUBLIC_BASE_URL or lb; pw=PUBLIC_WS_URL or lw
    print("="*50); print("  社区聊天室启动成功！"); print("-"*50); print(f"  本地访问: {lb}"); print(f"  WebSocket: {lw}")
    if PUBLIC_BASE_URL: print("-"*50); print(f"  外部访问: {pb}"); print(f"  外部 WS:  {pw}")
    print("="*50); await asyncio.Future()

if __name__=="__main__": asyncio.run(main())
