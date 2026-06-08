# -*- coding: utf-8 -*-
"""
chess_game.py - 国际象棋游戏逻辑模块
使用 python-chess 库，适配 aiohttp + 原生 WebSocket
"""
import uuid
import chess
import chess.pgn
from datetime import datetime
from io import StringIO


class ChessGame:
    """国际象棋单局游戏"""
    def __init__(self, room_id: str, game_id: str = None):
        self.room_id = room_id
        self.game_id = game_id or uuid.uuid4().hex[:12]
        self.board = chess.Board()          # python-chess 棋盘对象
        self.player_white = None            # 白方用户名
        self.player_black = None            # 黑方用户名
        self.white_ready = False
        self.black_ready = False
        self.status = 'waiting'             # waiting/playing/finished
        self.winner = None                  # 'white'/'black'/'draw'
        self.move_count = 0
        self.move_history = []              # SAN 走法列表
        self.started_at = None
        self.ended_at = None

    def assign_color(self, username: str) -> str:
        """分配颜色，返回 'white'/'black'/None"""
        if self.player_white is None:
            self.player_white = username
            return 'white'
        elif self.player_black is None:
            self.player_black = username
            return 'black'
        return None

    def get_color(self, username: str) -> str:
        if username == self.player_white:
            return 'white'
        if username == self.player_black:
            return 'black'
        return None

    def get_opponent(self, username: str) -> str:
        if username == self.player_white:
            return self.player_black
        if username == self.player_black:
            return self.player_white
        return None

    def remove_player(self, username: str):
        if username == self.player_white:
            self.player_white = None
            self.white_ready = False
        elif username == self.player_black:
            self.player_black = None
            self.black_ready = False
        self._check_players()

    def _check_players(self):
        actual = [p for p in (self.player_white, self.player_black) if p is not None]
        if len(actual) < 2 and self.status == 'playing':
            self.status = 'waiting'
            self.white_ready = False
            self.black_ready = False
        if len(actual) == 0:
            self.reset_game()

    def set_ready(self, username: str) -> tuple:
        """准备，双方准备后自动开始。返回 (success, both_ready)"""
        if self.status != 'waiting':
            return False, False
        if username == self.player_white:
            self.white_ready = True
        elif username == self.player_black:
            self.black_ready = True
        else:
            return False, False

        both = self.white_ready and self.black_ready
        if both:
            self._start_game()
        return True, both

    def unready(self, username: str):
        if self.status != 'waiting':
            return
        if username == self.player_white:
            self.white_ready = False
        elif username == self.player_black:
            self.black_ready = False

    def _start_game(self):
        """开始新对局"""
        self.board = chess.Board()
        self.status = 'playing'
        self.winner = None
        self.move_count = 0
        self.move_history = []
        self.started_at = datetime.now()
        self.ended_at = None

    def reset_game(self):
        self.status = 'waiting'
        self.board = chess.Board()
        self.winner = None
        self.move_count = 0
        self.move_history = []
        self.white_ready = False
        self.black_ready = False
        self.started_at = None
        self.ended_at = None

    def make_move(self, username: str, from_square: str, to_square: str, promotion: str = None) -> tuple:
        """
        走棋。返回 (success, msg, extra_info)
        from_square/to_square: UCI 格式如 'e2'、'e4'
        promotion: 'q'/'r'/'b'/'n' 升变
        """
        if self.status != 'playing':
            return False, '游戏未开始', {}

        color = self.get_color(username)
        if color is None:
            return False, '你不是对局玩家', {}
        
        turn_color = 'white' if self.board.turn == chess.WHITE else 'black'
        if color != turn_color:
            return False, '还没轮到你', {}

        try:
            # 构建 UCI 走法
            move_str = from_square + to_square
            if promotion:
                move_str += promotion
            move = chess.Move.from_uci(move_str)

            if move not in self.board.legal_moves:
                return False, '不合法的走法', {
                    'legal_moves': [m.uci() for m in self.board.legal_moves]
                }

            san = self.board.san(move)
            self.board.push(move)
            self.move_count += 1
            self.move_history.append(san)

            extra = {
                'san': san,
                'fen': self.board.fen(),
                'is_check': self.board.is_check(),
                'is_checkmate': self.board.is_checkmate(),
                'is_stalemate': self.board.is_stalemate(),
                'is_game_over': self.board.is_game_over(),
                'is_insufficient_material': self.board.is_insufficient_material(),
                'is_fifty_moves': self.board.is_fifty_moves(),
                'is_repetition': self.board.is_repetition(3),
            }

            # 检测游戏结束
            if self.board.is_game_over():
                self.status = 'finished'
                self.ended_at = datetime.now()
                if self.board.is_checkmate():
                    loser_color = 'white' if self.board.turn == chess.WHITE else 'black'
                    self.winner = 'black' if loser_color == 'white' else 'white'
                    extra['winner'] = 'black' if loser_color == 'white' else 'white'
                    extra['winner_reason'] = 'checkmate'
                else:
                    self.winner = 'draw'
                    extra['winner'] = 'draw'
                    extra['winner_reason'] = 'stalemate' if self.board.is_stalemate() else 'draw'

            return True, '走棋成功', extra

        except ValueError as e:
            return False, f'无效的走法: {str(e)}', {}

    def get_legal_moves(self, username: str = None):
        """获取合法走法列表"""
        if self.status != 'playing':
            return []
        color = self.get_color(username) if username else None
        if color:
            turn_color = 'white' if self.board.turn == chess.WHITE else 'black'
            if color != turn_color:
                return []
        return [m.uci() for m in self.board.legal_moves]

    def get_board_state(self):
        """获取棋盘状态（用于前端同步）"""
        fen = self.board.fen()
        return {
            'fen': fen,
            'turn': 'white' if self.board.turn == chess.WHITE else 'black',
            'is_check': self.board.is_check(),
            'is_checkmate': self.board.is_checkmate(),
            'is_game_over': self.board.is_game_over(),
            'fullmove_number': self.board.fullmove_number,
        }

    def to_dict(self):
        """序列化为字典"""
        return {
            'gameId': self.game_id,
            'roomId': self.room_id,
            'playerWhite': self.player_white,
            'playerBlack': self.player_black,
            'whiteReady': self.white_ready,
            'blackReady': self.black_ready,
            'status': self.status,
            'winner': self.winner,
            'moveCount': self.move_count,
            'moveHistory': self.move_history,
            'fen': self.board.fen(),
            'turn': 'white' if self.board.turn == chess.WHITE else 'black',
            'isCheck': self.board.is_check(),
            'isCheckmate': self.board.is_checkmate(),
            'isGameOver': self.board.is_game_over(),
            'legalMoves': [m.uci() for m in self.board.legal_moves],
        }


class ChessManager:
    """国际象棋管理器"""
    def __init__(self):
        self.games = {}  # room_id -> ChessGame

    def get_or_create(self, room_id: str) -> ChessGame:
        if room_id not in self.games:
            self.games[room_id] = ChessGame(room_id)
        return self.games[room_id]

    def get(self, room_id: str) -> ChessGame:
        return self.games.get(room_id)

    def remove(self, room_id: str):
        if room_id in self.games:
            del self.games[room_id]


# 全局单例
chess_intl_manager = ChessManager()
