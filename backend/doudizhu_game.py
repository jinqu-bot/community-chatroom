# -*- coding: utf-8 -*-
"""
doudizhu_game.py - 斗地主游戏逻辑模块
适配 aiohttp + 原生 WebSocket 的社区聊天室
"""
import random
import copy
import uuid
from datetime import datetime

# ==================== 牌面常量 ====================
SUITS = ['♠', '♥', '♣', '♦']
RANKS = ['3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K', 'A', '2']
JOKER_SMALL = '小王'
JOKER_BIG = '大王'
RANK_VALUE = {
    '3': 3, '4': 4, '5': 5, '6': 6, '7': 7, '8': 8, '9': 9,
    '10': 10, 'J': 11, 'Q': 12, 'K': 13, 'A': 14, '2': 15,
    '小王': 16, '大王': 17,
}

# ==================== Card 类 ====================
class Card:
    """单张扑克牌"""
    def __init__(self, rank: str, suit: str = ''):
        self.rank = rank
        self.suit = suit
        self.value = RANK_VALUE[rank]

    def __repr__(self):
        if self.rank in (JOKER_SMALL, JOKER_BIG):
            return self.rank
        return f'{self.suit}{self.rank}'

    def __eq__(self, other):
        return isinstance(other, Card) and self.rank == other.rank and self.suit == other.suit

    def __hash__(self):
        return hash((self.rank, self.suit))

    def to_dict(self):
        return {'rank': self.rank, 'suit': self.suit, 'value': self.value, 'display': str(self)}


# ==================== 牌堆工具 ====================
def create_deck():
    """生成54张牌"""
    deck = []
    for suit in SUITS:
        for rank in RANKS:
            deck.append(Card(rank, suit))
    deck.append(Card(JOKER_SMALL))
    deck.append(Card(JOKER_BIG))
    return deck

def shuffle_deck(deck):
    random.shuffle(deck)
    return deck

def sort_cards(cards):
    """大牌在前排序"""
    suit_order = {'♠': 0, '♥': 1, '♣': 2, '♦': 3}
    return sorted(cards, key=lambda c: (-c.value, suit_order.get(c.suit, 0)))

def cards_to_dict_list(cards):
    return [c.to_dict() for c in cards]

def dict_list_to_cards(data):
    return [Card(d['rank'], d['suit']) for d in data]


# ==================== Player 类 ====================
class DDZPlayer:
    """斗地主玩家"""
    def __init__(self, username: str):
        self.username = username
        self.cards = []          # Card对象列表
        self.role = 'farmer'     # farmer/landlord
        self.is_ready = False
        self.seat_index = -1     # 0/1/2
        self.is_landlord = False
        self.cards_count = 0

    def set_cards(self, cards):
        self.cards = sort_cards(cards)
        self.cards_count = len(self.cards)

    def remove_cards(self, cards_to_remove):
        for c in cards_to_remove:
            for i, mc in enumerate(self.cards):
                if mc == c:
                    self.cards.pop(i)
                    break
        self.cards_count = len(self.cards)

    def get_cards_info(self):
        return {
            'cards': cards_to_dict_list(self.cards),
            'count': self.cards_count
        }

    def reset(self):
        self.cards = []
        self.role = 'farmer'
        self.is_ready = False
        self.is_landlord = False
        self.cards_count = 0

    def to_dict(self):
        return {
            'username': self.username,
            'role': self.role,
            'seatIndex': self.seat_index,
            'isLandlord': self.is_landlord,
            'cardsCount': self.cards_count,
            'isReady': self.is_ready,
        }


# ==================== 牌型规则引擎 ====================
class CardType:
    INVALID = -1
    SINGLE = 0
    PAIR = 1
    TRIPLE = 2
    TRIPLE_ONE = 3
    TRIPLE_TWO = 4
    STRAIGHT = 5
    STRAIGHT_PAIRS = 6
    AIRPLANE = 7
    AIRPLANE_WINGS = 8
    FOUR_TWO = 9
    BOMB = 10
    ROCKET = 11

    NAMES = {
        -1: '无效', 0: '单张', 1: '对子', 2: '三张', 3: '三带一',
        4: '三带二', 5: '顺子', 6: '连对', 7: '飞机', 8: '飞机带翅膀',
        9: '四带二', 10: '炸弹', 11: '王炸'
    }


def is_bomb(card_type):
    return card_type in (CardType.BOMB, CardType.ROCKET)


class PlayedHand:
    """一手出牌"""
    def __init__(self, card_type, main_value, cards, length=0):
        self.card_type = card_type
        self.main_value = main_value
        self.cards = cards
        self.length = length
        self.type_name = CardType.NAMES.get(card_type, '未知')

    def can_beat(self, other):
        """能否大过对方"""
        if self.card_type == CardType.ROCKET:
            return True
        if other.card_type == CardType.ROCKET:
            return False
        if self.card_type == CardType.BOMB:
            if other.card_type != CardType.BOMB:
                return True
            return self.main_value > other.main_value
        if other.card_type == CardType.BOMB:
            return False
        if self.card_type == other.card_type and self.length == other.length:
            return self.main_value > other.main_value
        return False

    def to_dict(self):
        return {
            'cardType': self.card_type,
            'mainValue': self.main_value,
            'length': self.length,
            'typeName': self.type_name,
            'cards': cards_to_dict_list(self.cards),
        }


def _get_value_counts(cards):
    """统计每个牌值的数量 {value: count}"""
    counts = {}
    for c in cards:
        counts[c.value] = counts.get(c.value, 0) + 1
    return counts


def _find_straight(cards, min_len=5):
    """寻找最长的顺子（不含2和王）"""
    counts = _get_value_counts(cards)
    sorted_vals = sorted([v for v, cnt in counts.items() if v <= 14 and cnt >= 1])
    for end in range(len(sorted_vals) - 1, min_len - 2, -1):
        for start in range(end - min_len + 2):
            if all(sorted_vals[i] + 1 == sorted_vals[i + 1] for i in range(start, end)):
                return sorted_vals[start:end], sorted_vals[end]
    return None, None


def _find_straight_pairs(cards, min_len=3):
    """寻找最长连对"""
    counts = _get_value_counts(cards)
    sorted_vals = sorted([v for v, cnt in counts.items() if v <= 14 and cnt >= 2])
    for end in range(len(sorted_vals) - 1, min_len - 2, -1):
        for start in range(end - min_len + 2):
            if all(sorted_vals[i] + 1 == sorted_vals[i + 1] for i in range(start, end)):
                return sorted_vals[start:end], sorted_vals[end], [start, end]
    return None, None, None


def _find_airplane(cards, min_len=2):
    """寻找最长连续的飞机（三张连续段）"""
    counts = _get_value_counts(cards)
    sorted_vals = sorted([v for v, cnt in counts.items() if v <= 14 and cnt >= 3])
    for end in range(len(sorted_vals) - 1, min_len - 2, -1):
        for start in range(end - min_len + 2):
            if all(sorted_vals[i] + 1 == sorted_vals[i + 1] for i in range(start, end)):
                seg = sorted_vals[start:end + 1]
                return seg, sorted_vals, [start, end + 1]
    return None, None, None


def _get_cards_by_values(cards, values, count=1):
    """按牌值获取卡牌"""
    result = []
    used = set()
    for v in values:
        found = 0
        for c in cards:
            if id(c) in used:
                continue
            if c.value == v:
                result.append(c)
                used.add(id(c))
                found += 1
                if found >= count:
                    break
    return result


def analyze_cards(cards):
    """
    分析一手牌的牌型，返回 PlayedHand
    cards: Card对象列表
    """
    if not cards:
        return PlayedHand(CardType.INVALID, 0, [], 0)

    n = len(cards)
    counts = _get_value_counts(cards)
    sorted_cards = sort_cards(cards)

    # 王炸
    if n == 2:
        vals = [c.value for c in cards]
        if 16 in vals and 17 in vals:
            return PlayedHand(CardType.ROCKET, 17, sorted_cards, 2)

    # 炸弹（4张相同）
    if n == 4 and len(counts) == 1 and list(counts.values())[0] == 4:
        v = list(counts.keys())[0]
        return PlayedHand(CardType.BOMB, v, sorted_cards, 4)

    # 单张
    if n == 1:
        return PlayedHand(CardType.SINGLE, sorted_cards[0].value, sorted_cards, 1)

    # 对子
    if n == 2 and sorted_cards[0].rank == sorted_cards[1].rank:
        return PlayedHand(CardType.PAIR, sorted_cards[0].value, sorted_cards, 2)

    # 三张
    if n == 3 and len(counts) == 1 and list(counts.values())[0] == 3:
        v = list(counts.keys())[0]
        return PlayedHand(CardType.TRIPLE, v, sorted_cards, 3)

    # 顺子（>=5张连续单张，不含2和王）
    if n >= 5:
        seg, last = _find_straight(cards, n)
        if seg and len(seg) == n and last == seg[-1]:
            return PlayedHand(CardType.STRAIGHT, seg[-1], sorted_cards, n)

    # 连对（>=3对连续）
    if n >= 6 and n % 2 == 0:
        seg, last, _ = _find_straight_pairs(cards, n // 2)
        if seg and len(seg) == n // 2:
            return PlayedHand(CardType.STRAIGHT_PAIRS, seg[-1], sorted_cards, n // 2)

    # 三带一 / 三带二
    if n in (4, 5):
        for v, cnt in counts.items():
            if cnt == 3:
                if n == 4:
                    main_cards = _get_cards_by_values(cards, [v], 3)
                    return PlayedHand(CardType.TRIPLE_ONE, v, main_cards + [c for c in sorted_cards if c.value != v], 1)
                elif n == 5:
                    # 三带二需要一对
                    other_counts = {kv: kc for kv, kc in counts.items() if kv != v}
                    if 2 in other_counts.values():
                        main_cards = _get_cards_by_values(cards, [v], 3)
                        return PlayedHand(CardType.TRIPLE_TWO, v, main_cards + [c for c in sorted_cards if c.value != v][:2], 1)

    # 飞机 / 飞机带翅膀
    if n >= 6:
        airplane_seg, _, _ = _find_airplane(cards, 2)
        if airplane_seg:
            seg_len = len(airplane_seg)
            main_count = seg_len * 3
            # 纯飞机
            if n == main_count:
                return PlayedHand(CardType.AIRPLANE, airplane_seg[-1], sorted_cards, seg_len)
            # 飞机带单翅膀
            if n == main_count + seg_len:
                return PlayedHand(CardType.AIRPLANE_WINGS, airplane_seg[-1], sorted_cards, seg_len)
            # 飞机带双翅膀
            if n == main_count + seg_len * 2:
                return PlayedHand(CardType.AIRPLANE_WINGS, airplane_seg[-1], sorted_cards, seg_len)

    # 四带二（单或对）
    if n == 6 or n == 8:
        for v, cnt in counts.items():
            if cnt == 4:
                return PlayedHand(CardType.FOUR_TWO, v, sorted_cards, 1)

    return PlayedHand(CardType.INVALID, 0, sorted_cards, 0)


# ==================== 结算系统 ====================
def calculate_settlement(room):
    """
    计算结算结果
    返回: {winnerSeat, landlordWins, multiplier, bombCount, scores}
    """
    winner_player = None
    for p in room.players:
        if p.cards_count == 0:
            winner_player = p
            break

    if winner_player is None:
        return None

    landlord_wins = winner_player.is_landlord
    bomb_count = room.bomb_count
    multiplier = room.multiplier * (2 ** bomb_count)
    base = room.base_score

    scores = {}
    for p in room.players:
        if p.is_landlord:
            scores[p.username] = multiplier * base * (1 if landlord_wins else -1) * 2
        else:
            scores[p.username] = multiplier * base * (-1 if landlord_wins else 1)

    return {
        'winnerSeat': winner_player.seat_index,
        'landlordWins': landlord_wins,
        'multiplier': multiplier,
        'bombCount': bomb_count,
        'scores': scores,
        'winnerName': winner_player.username,
    }


# ==================== 房间类 ====================
class DDZRoom:
    """斗地主单个房间"""
    def __init__(self, room_id, room_name):
        self.room_id = room_id
        self.room_name = room_name
        self.players = []        # DDZPlayer列表，按seat_index排序
        self.seats = [None, None, None]  # 3个座位
        self.phase = 'waiting'    # waiting/ready/dealing/bidding/playing/settlement
        self.deck = []
        self.hole_cards = []      # 3张底牌
        self.landlord_index = -1  # 地主座位号
        self.current_turn = -1
        self.last_played = None   # PlayedHand
        self.last_play_seat = -1
        self.pass_count = 0
        self.bidding_seat = -1
        self.bidding_scores = {}  # seat -> score (0=不叫, 1/2/3=叫分)
        self.highest_bid = 0
        self.highest_bidder = -1
        self.multiplier = 1
        self.base_score = 1
        self.bomb_count = 0
        self.game_history = []

    def add_player(self, username):
        """添加玩家"""
        for p in self.players:
            if p.username == username:
                return False, '已在房间中'
        if len(self.players) >= 3:
            return False, '房间已满'
        player = DDZPlayer(username)
        # 分配座位
        for i in range(3):
            if self.seats[i] is None:
                player.seat_index = i
                self.seats[i] = player
                break
        self.players.append(player)
        return True, player.seat_index

    def remove_player(self, username):
        """移除玩家"""
        for p in self.players[:]:
            if p.username == username:
                self.seats[p.seat_index] = None
                self.players.remove(p)
                return True
        return False

    def is_full(self):
        return len(self.players) == 3

    def is_empty(self):
        return len(self.players) == 0

    def all_players_ready(self):
        return len(self.players) >= 2 and all(p.is_ready for p in self.players)

    def set_ready(self, username):
        """设置准备状态"""
        for p in self.players:
            if p.username == username:
                p.is_ready = not p.is_ready  # Toggle
                return True
        return False

    def start_game(self):
        """开始游戏：洗牌→发牌→进入叫地主阶段"""
        self.deck = shuffle_deck(create_deck())
        # 每人17张
        for i, p in enumerate(self.players):
            p.set_cards(self.deck[i * 17:(i + 1) * 17])
        # 3张底牌
        self.hole_cards = self.deck[51:54]
        self.phase = 'bidding'
        self.bidding_seat = 0
        self.bidding_scores = {}
        self.highest_bid = 0
        self.highest_bidder = -1
        self.multiplier = 1
        self.bomb_count = 0
        self.game_history = []
        return True

    def process_bid(self, username, score):
        """处理叫地主"""
        player = None
        for p in self.players:
            if p.username == username:
                player = p
                break
        if not player or self.phase != 'bidding':
            return False, '不在叫地主阶段'
        if player.seat_index != self.bidding_seat:
            return False, '还没轮到你叫'

        self.bidding_scores[player.seat_index] = score

        if score > self.highest_bid:
            self.highest_bid = score
            self.highest_bidder = player.seat_index
            self.multiplier = score

        # 确定叫地主结束
        # 3分直接当地主，或所有人叫完后最高分当地主
        if score == 3:
            self._set_landlord(player.seat_index)
            return True, {'action': 'landlord', 'seat': player.seat_index}
        
        if len(self.bidding_scores) >= 3:
            if self.highest_bidder >= 0:
                self._set_landlord(self.highest_bidder)
                return True, {'action': 'landlord', 'seat': self.highest_bidder}
            else:
                # 没人叫，重新发牌
                self.start_game()
                return True, {'action': 'redeal'}
        
        # 下一个人叫
        self.bidding_seat = (self.bidding_seat + 1) % 3
        return True, {'action': 'next', 'seat': self.bidding_seat}

    def _set_landlord(self, seat):
        """确定地主"""
        self.landlord_index = seat
        self.phase = 'playing'
        self.current_turn = seat
        self.last_played = None
        self.last_play_seat = -1
        self.pass_count = 0

        for p in self.players:
            if p.seat_index == seat:
                p.role = 'landlord'
                p.is_landlord = True
                p.set_cards(p.cards + self.hole_cards)
            else:
                p.role = 'farmer'
                p.is_landlord = False

    def process_play(self, username, card_indices):
        """处理出牌"""
        player = None
        for p in self.players:
            if p.username == username:
                player = p
                break
        if not player or self.phase != 'playing':
            return False, '不在出牌阶段', None
        if player.seat_index != self.current_turn:
            return False, '还没轮到你', None

        # 按索引取牌
        indices = sorted(card_indices, reverse=True)
        played = []
        for idx in indices:
            if 0 <= idx < len(player.cards):
                played.append(player.cards[idx])
        if not played:
            return False, '请选择要出的牌', None

        hand = analyze_cards(played)
        if hand.card_type == CardType.INVALID:
            return False, '无效的牌型', None

        # 比大小
        if self.last_played is not None and self.last_play_seat != player.seat_index:
            if not hand.can_beat(self.last_played):
                return False, '打不过对方，请选择更大的牌', None

        # 出牌
        player.remove_cards(hand.cards)
        self.last_played = hand
        self.last_play_seat = player.seat_index
        self.pass_count = 0

        if hand.card_type == CardType.BOMB:
            self.bomb_count += 1
        elif hand.card_type == CardType.ROCKET:
            self.bomb_count += 1

        self.game_history.append({
            'username': username,
            'seat': player.seat_index,
            'play': hand.to_dict(),
            'time': datetime.now().strftime('%H:%M:%S'),
        })

        # 检查胜利
        if player.cards_count == 0:
            self.phase = 'settlement'
            return True, '出牌成功', {'winner': username, 'seat': player.seat_index}

        # 下一个
        self.current_turn = (self.current_turn + 1) % 3
        return True, '出牌成功', {'nextSeat': self.current_turn}

    def process_pass(self, username):
        """不出"""
        player = None
        for p in self.players:
            if p.username == username:
                player = p
                break
        if not player or self.phase != 'playing':
            return False, '不在出牌阶段'
        if player.seat_index != self.current_turn:
            return False, '还没轮到你'
        if self.last_played is None:
            return False, '你是新一轮第一个出牌者，必须出牌'

        self.pass_count += 1
        self.current_turn = (self.current_turn + 1) % 3

        # 连续2人不出，新一轮（最后出牌者开始）
        if self.pass_count >= 2:
            self.pass_count = 0
            self.last_played = None
            self.last_play_seat = -1
            self.current_turn = (self.last_play_seat + 3) % 3 if self.last_play_seat >= 0 else 0
            # 找到上一个出牌的
            for p in self.players:
                if p.seat_index == self.current_turn:
                    break

        return True, '不出'

    def reset_game(self):
        """重置游戏"""
        self.phase = 'waiting'
        self.deck = []
        self.hole_cards = []
        self.landlord_index = -1
        self.current_turn = -1
        self.last_played = None
        self.last_play_seat = -1
        self.pass_count = 0
        self.bidding_seat = -1
        self.bidding_scores = {}
        self.highest_bid = 0
        self.highest_bidder = -1
        self.multiplier = 1
        self.bomb_count = 0
        self.game_history = []
        for p in self.players:
            p.reset()
            p.is_ready = False

    def to_dict(self, viewer_username=None):
        """序列化房间公共信息"""
        return {
            'roomId': self.room_id,
            'roomName': self.room_name,
            'phase': self.phase,
            'players': [p.to_dict() for p in self.players],
            'landlordIndex': self.landlord_index,
            'currentTurn': self.current_turn,
            'lastPlayed': self.last_played.to_dict() if self.last_played else None,
            'lastPlaySeat': self.last_play_seat,
            'passCount': self.pass_count,
            'biddingSeat': self.bidding_seat,
            'biddingScores': self.bidding_scores,
            'highestBid': self.highest_bid,
            'multiplier': self.multiplier,
            'holeCards': cards_to_dict_list(self.hole_cards) if self.phase in ('playing', 'settlement') else [],
            'gameHistory': self.game_history[-20:],
        }

    def get_player_state(self, username):
        """获取单个玩家的私有状态"""
        for p in self.players:
            if p.username == username:
                return {
                    'myCards': p.get_cards_info()['cards'],
                    'mySeat': p.seat_index,
                    'myRole': p.role,
                }
        return None


# ==================== 斗地主管理器 ====================
class DDZManager:
    """管理所有斗地主房间"""
    def __init__(self):
        self.rooms = {}  # room_id -> DDZRoom

    def create_room(self, room_name):
        room_id = uuid.uuid4().hex[:8]
        room = DDZRoom(room_id, room_name)
        self.rooms[room_id] = room
        return room

    def get_room(self, room_id):
        return self.rooms.get(room_id)

    def remove_room(self, room_id):
        if room_id in self.rooms:
            del self.rooms[room_id]

    def get_all_rooms(self):
        return list(self.rooms.values())


# 全局单例
ddz_manager = DDZManager()
