from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from dataclasses import asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from rules import PRESETS, RulePreset, DeckRule, PenaltyRule, PrimeRule
from registered_primes import parse_registered_composite_text, parse_registered_prime_text
import json
import random
from random import randrange
import secrets
import uuid
import asyncio
import os, httpx
from math import gcd
import time

WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
SERVER_DIR = Path(__file__).resolve().parent
SAMPLE_MEMORY_JSON = SERVER_DIR / "sample_memory.json"
app = FastAPI()

################################################
# 素数判定
################################################

_SMALL_PRIMES = (2,3,5,7,11,13,17,19,23,29,31,37)

def is_prime(n: int, k: int = 16) -> bool:
    if n < 2:
        return False
    # 小素数チェック（高速化 & 明確化）
    for p in _SMALL_PRIMES:
        if n == p:
            return True
        if n % p == 0:
            return False

    # n-1 = d * 2^s
    m = n - 1
    lsb = m & -m
    s = lsb.bit_length() - 1
    d = m // lsb

    def check(a: int) -> bool:
        x = pow(a, d, n)
        if x == 1 or x == n - 1:
            return True
        for _ in range(s - 1):
            x = (x * x) % n
            if x == n - 1:
                return True
        return False  # 合成数確定

    # 2^64 未満は決定的な既知の底集合で完全判定
    if n < (1 << 64):
        for a in (2,3,5,7,11,13,17,19,23,29,31,37):
            if not check(a):
                return False
        return True

    # それ以上（=72桁含む）は確率的に k ラウンド
    for _ in range(k):
        a = randrange(2, n - 1)
        if not check(a):
            return False
    return True

def is_twin_quadruplet_prime(n: int) -> bool:
    """
    四つ子素数判定。
    n が四つ組 {a, a+2, a+6, a+8} のいずれかに属し、
    その4つがすべて素数なら True。
    例外として 5,7,11,13 も True。
    """
    if n in {5, 7, 11, 13}:
        return True

    if not is_prime(n):
        return False

    # n が四つ組のどの位置かで候補の開始点 a を調べる
    candidates = [n, n - 2, n - 6, n - 8]

    for a in candidates:
        if a < 2:
            continue
        quad = [a, a + 2, a + 6, a + 8]
        if n in quad and all(is_prime(x) for x in quad):
            return True

    return False

def find_prime_factor(n: int, time_limit: float = 2.0) -> int:
    """
    Pollard Rho + 最後の保険の試し割りで n の素因数を1つ返す。
    - できるだけ最後まで粘る
    - ただし安全のため time_limit 秒で打ち切る
    - n が素数なら n 自身を返す
    """
    start_time = time.perf_counter()

    if n % 2 == 0:
        return 2
    if n % 3 == 0:
        return 3
    if is_prime(n):
        return n

    def timed_out() -> bool:
        return (time.perf_counter() - start_time) >= time_limit

    m = int(n ** 0.125) + 1
    c = 1

    while not timed_out():
        f = lambda a, c=c: (pow(a, 2, n) + c) % n
        y = 2
        g = q = 1
        r = 1
        ys = 0

        while g == 1 and not timed_out():
            x = y
            k = 0
            q = 1

            while k < r and g == 1 and not timed_out():
                ys = y
                upper = min(m, r - k)
                for _ in range(upper):
                    y = f(y)
                    q = (q * abs(x - y)) % n
                g = gcd(q, n)
                k += upper

            r *= 2

        if timed_out():
            break

        if g == n:
            g = 1
            y = ys
            while g == 1 and not timed_out():
                y = f(y)
                g = gcd(abs(x - y), n)

        if timed_out():
            break

        if 1 < g < n:
            if is_prime(g):
                return g
            return find_prime_factor(g, time_limit=max(0.1, time_limit - (time.perf_counter() - start_time)))

        c += 1

    # ---- 保険: 時間が残っていれば試し割り ----
    d = 5
    while d * d <= n and not timed_out():
        if n % d == 0:
            return d
        if n % (d + 2) == 0:
            return d + 2
        d += 6

    # 見つからなければ n を返す
    return n

def is_semiprime(n: int) -> bool:
    """
    半素数判定。
    素数2個の積なら True（平方も可）。
    """
    if n < 4:
        return False

    if is_prime(n):
        return False

    p = find_prime_factor(n, time_limit=2.0)
    if p <= 1 or p == n:
        return False

    q, r = divmod(n, p)
    if r != 0:
        return False

    return is_prime(p) and is_prime(q)

def is_valid_prime_by_rule(n: int, rule: RulePreset) -> bool:
    if rule.prime_rule is PrimeRule.NORMAL:
        return is_prime(n)
    if rule.prime_rule is PrimeRule.TETRAD:
        return is_twin_quadruplet_prime(n)
    if rule.prime_rule is PrimeRule.SEMIPRIME:
        if n >= 10**24:
            return False
        return is_semiprime(n)
    return is_prime(n)

def is_valid_prime_for_player(n: int, player: "Player", rule: RulePreset) -> bool:
    if rule.prime_rule is PrimeRule.REGISTERED:
        return player.can_use_registered_prime(n)
    return is_valid_prime_by_rule(n, rule)

def rule_display_name(prime_rule: PrimeRule) -> str:
    if prime_rule is PrimeRule.TETRAD:
        return "四つ子素数"
    if prime_rule is PrimeRule.SEMIPRIME:
        return "半素数"
    if prime_rule is PrimeRule.REGISTERED:
        return "登録済み素数"
    return "素数"

################################################
# クラス定義
################################################

class Room:
    def __init__(self, room_id: str, rule: RulePreset):
        self.room_id = room_id
        self.rule: RulePreset = rule
        self.players = []    # Playerオブジェクトのリスト
        self.state = "waiting"
        self.deck = []
        self.field = []      # 場に出ているカード
        self.reserve = [] # 山札予備軍
        self.last_number = None     # “場に出ている”最後の数値を保持
        self.current_turn_id = None
        self.has_drawn = False
        self.reverse_order = False
        self.score_log = []

    async def broadcast(self, message: dict):
        for p in self.players:
            await p.send_json(message)

    async def update_room_status(self):
        message = {
            "type": "update_room_status",
            "room_id": self.room_id,
            "rule": self.rule.label,
            "allow_composite": self.rule.allow_composite,
            "prime_rule": self.rule.prime_rule.name.lower(),
            "count": len(self.players),
            "player_list": [
                {
                    "id": p.id,
                    "name": p.name,
                    "status": p.status,
                    "registered_prime_count": len(p.registered_primes),
                    "registered_composite_count": len(p.registered_composites),
                }
                for p in self.players
            ],
            "waiting_count": len([p for p in self.players if p.status == "waiting"])
        }
        await self.broadcast(message)

    async def log_chat(self, message: str, sender="system"):
        await self.broadcast({"type": "chat", "sender": sender, "message": message})

    # その他、ルームに関連するロジック（プレイヤー追加、削除、ゲーム開始、次のターンなど）をメソッドとして実装
    async def update_game_state(self):
        current_player = next((p for p in self.players if p.id == self.current_turn_id), None)
        current_name = current_player.name if current_player else None
        state_msg = {
            "type": "game_update",
            "room_id": self.room_id,
            "state": self.state,
            "current_turn": current_name,
            "revolution": self.reverse_order,
            "allow_composite": self.rule.allow_composite,
            "prime_rule": self.rule.prime_rule.name.lower(),
            "deck_count": len(self.deck),
            "field": self.field,
            "player_list": [
                {
                    "id": p.id,
                    "name": p.name,
                    "status": p.status,
                    "registered_prime_count": len(p.registered_primes),
                    "registered_composite_count": len(p.registered_composites),
                }
                for p in self.players
            ],
            "hand_counts": [
                {"id": p.id, "name": p.name, "count": len(p.hand)}
                for p in get_active_players(self)
            ]
        }
        await self.broadcast(state_msg)

    async def try_end_game(self) -> bool:
        """勝者がいれば game_over を投げて True、なければ False を返す"""
        winner = check_win_condition(self)
        if winner is not None:
            self.state = "waiting"
            await self.broadcast({"type": "game_over", "winner": winner, "state": self.state})
            await self.log_chat(f"{winner}が勝利しました")
            await publish_score_log(self, winner)
            return True
        return False


# アプリケーションの初期化時にRoomインスタンスを必要な数だけ作成しておく
ROOM_CONFIG = [
    ("room_1", PRESETS["std-5-1"]),
    ("room_2", PRESETS["std-7-1"]),
    ("room_3", PRESETS["std-11-f"]),
    ("room_4", PRESETS["half-5-f"]),
    ("room_5", PRESETS["half-7-1-c"]),
    ("百鬼夜行1", PRESETS["half-7-1-c"]),
    ("百鬼夜行2", PRESETS["half-7-1-c"]),
    ("百鬼夜行3", PRESETS["half-7-1-c"]),
    ("room_6", PRESETS["std-11-f-c"]),
    ("room_7", PRESETS["std-11-n-c"]),
    ("room_8", PRESETS["std-11-n-c-rev"]),
    ("room_9", PRESETS["tetrad-11-n"]),
    ("room_10", PRESETS["tetrad-11-n-c"]),
    ("room_11", PRESETS["semiprime-11-n"]),
    ("room_12", PRESETS["semiprime-11-n-c"]),
    ("room_13", PRESETS["registered-11-n"]),
]
rooms = {rid: Room(rid, rule) for rid, rule in ROOM_CONFIG}

class Player:
    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.id = secrets.token_hex(16)
        suffix = int(self.id, 16) % 10000
        self.name = f"プレイヤー{suffix:04d}"
        self.room = None  # 所属ルーム（Roomオブジェクト）
        self.status = "watching"  # 初期状態は観戦中
        self.hand = []  # プレイヤーが持つカードリスト
        self.registered_primes: set[int] = set()
        self.registered_composites: set[int] = set()

    async def send_json(self, message: dict):
        """WebSocketを通じてJSONメッセージを送信する"""
        await self.ws.send_json(message)

    async def send_hand_update(self):
        """手札の変更通知をクライアントに送信する"""
        message = {
            "type": "hand_update",
            "your_hand": self.hand
        }
        await self.send_json(message)

    def sort_hand(self):
        """手札をランク順（必要に応じてスートも考慮）に並び替える"""
        # ここでは単純にカードの"rank"で昇順にソート
        self.hand.sort(key=lambda card: card["rank"])

    def add_card(self, card: dict):
        """手札にカードを追加する"""
        self.hand.append(card)
        self.sort_hand()  # カード追加後に手札を並び替え

    def remove_card(self, card: dict) -> bool:
        """手札から指定のカードを削除する。存在すればTrue、なければFalseを返す"""
        if card in self.hand:
            self.hand.remove(card)
            return True
        return False

    def has_cards(self, cards: List[dict]) -> bool:
        """指定されたカード群が自分の手札に存在するかチェックする"""
        temp = self.hand[:]  # コピーを使ってチェック
        for card in cards:
            if card in temp:
                temp.remove(card)
            else:
                return False
        return True

    def remove_cards(self, cards: List[dict]) -> bool:
        """指定されたカード群を手札から削除する。すべて削除できた場合にTrueを返す"""
        if not self.has_cards(cards):
            return False
        for card in cards:
            self.remove_card(card)
        return True

    def clear_hand(self):
        """手札をクリアする"""
        self.hand = []

    def replace_registered_primes(self, values: set[int]) -> None:
        self.registered_primes = set(values)

    def can_use_registered_prime(self, n: int) -> bool:
        return n in self.registered_primes

    def replace_registered_composites(self, values: set[int]) -> None:
        self.registered_composites = set(values)

    def can_use_registered_composite(self, n: int) -> bool:
        return n in self.registered_composites

################################################
# 勝敗判定ロジック
################################################

def check_win_condition(room):
    active_players = get_active_players(room)

    # 1. 対戦参加者が1人だけになった場合の特殊勝利
    if room.state == "playing" and len(active_players) == 1:
        return active_players[0].name

    if len(active_players) == 0:
        return None

    # 2. 現在プレイヤーの手札0枚による通常勝利
    current_turn_id = room.current_turn_id
    if current_turn_id is None:
        return None
    current_player = next((p for p in active_players if p.id == current_turn_id), None)
    if current_player is not None:
        if len(current_player.hand) == 0:
            # 勝利者のIDまたはPlayerオブジェクトそのものを返す（要件に応じて）
            return current_player.name
    return None

def get_active_players(room) -> List["Player"]:
    return [p for p in room.players if p.status == "waiting"]

def score_card_symbol(card: dict) -> str:
    if card.get("is_joker") or card.get("suit") == "X":
        return "X"
    return score_value_symbol(card.get("rank"))

def score_value_symbol(value) -> str:
    value = str(value)
    return {
        "1": "A",
        "10": "T",
        "11": "J",
        "12": "Q",
        "13": "K",
    }.get(value, value)

def score_sort_key(card: dict) -> int:
    if card.get("is_joker") or card.get("suit") == "X":
        return 14
    return int(card.get("rank", 0))

def score_cards_text(cards: List[dict], sort_cards: bool = False) -> str:
    ordered = sorted(cards, key=score_sort_key) if sort_cards else cards
    return "".join(score_card_symbol(c) for c in ordered)

def score_joker_suffix(cards: List[dict], assigned_values: List[str]) -> str:
    suffixes = []
    joker_index = 0
    for card in cards:
        if not (card.get("is_joker") or card.get("suit") == "X"):
            continue
        if joker_index >= len(assigned_values):
            break
        value = str(assigned_values[joker_index])
        joker_index += 1
        if value != "inf":
            suffixes.append(f"|X={score_value_symbol(value)}")
    return "".join(suffixes)

def score_state_prefix(room: Room) -> str:
    return "[R]" if room.reverse_order else ""

def score_win_suffix(player: "Player") -> str:
    return "#" if len(player.hand) == 0 else ""

def score_tokens_text(tokens: List[dict], cards_by_id: Dict[str, dict]) -> str:
    parts = []
    for token in tokens:
        if token.get("kind") == "card":
            card = cards_by_id.get(token.get("card_id"))
            parts.append(score_card_symbol(card) if card else "?")
        elif token.get("kind") == "op":
            parts.append("*" if token.get("op") == "×" else token.get("op", "?"))
    return "".join(parts)

def record_score_line(room: Room, line: str) -> None:
    room.score_log.append({
        "turn": len(room.score_log) + 1,
        "line": line,
    })

def record_score_event(room: Room, player: "Player", notation: str, result: str) -> None:
    line = f"{player.name}:{notation}"
    room.score_log.append({
        "turn": len(room.score_log) + 1,
        "player": player.name,
        "notation": notation,
        "result": result,
        "line": line,
    })

async def publish_score_log(room: Room, winner: Optional[str]) -> None:
    if not room.score_log:
        return
    await room.broadcast({
        "type": "score_record",
        "sender": "system",
        "winner": winner,
        "records": room.score_log,
        "lines": [record.get("line", "") for record in room.score_log if record.get("line")],
    })

################################################
# カード生成と配布のユーティリティ
################################################
def generate_deck() -> List[dict]:
    deck = []
    for suit in ["S","H","D","C"]:
        for rank in range(1,14):
            deck.append({
                "card_id": str(uuid.uuid4()),
                "suit": suit,
                "rank": rank,
                "is_joker": False
            })
    # ジョーカー２枚にも同様にIDを
    for _ in range(2):
        deck.append({
            "card_id": str(uuid.uuid4()),
            "suit": "X",
            "rank": 0,
            "is_joker": True
        })
    random.shuffle(deck)
    return deck

def build_deck(rule: RulePreset) -> List[dict]:
    deck = generate_deck()
    if rule.deck_rule is DeckRule.EVEN_HALVED:
        # 偶数で、かつスートが D/H のカードだけを除去（Jokerは除外）
        deck = [
            c for c in deck
            if not (
                (not c["is_joker"]) and
                (c["rank"] % 2 == 0) and
                (c["suit"] in ("D", "H"))
            )
        ]
    random.shuffle(deck)
    return deck

def shuffle_and_deal(deck: List[dict], hand_n: int, num_players: int = 2
                     ) -> Tuple[List[List[dict]], List[dict]]:
    """
    deck をシャッフルして num_players 人へ hand_n 枚ずつ順番配り。
    返り値: hands[プレイヤーごとの手札], remaining_deck
    """
    deck = deck[:]            # 破壊的変更を避ける
    random.shuffle(deck)

    hands = [[] for _ in range(num_players)]
    total_needed = hand_n * num_players
    if len(deck) < total_needed:
        total_needed = len(deck) - (len(deck) % num_players)
        hand_n = total_needed // num_players  # 足りない場合は配れるだけ配る

    # ラウンドロビンで配る（将来のバグ予防：順番性が必要な場合に備える）
    for r in range(hand_n):
        for i in range(num_players):
            hands[i].append(deck.pop(0))
    return hands, deck

def push_to_reserve(room: Room, cards: List[dict]) -> None:
    """出した札を、出した順番のまま予備軍へ積む（重複登録は呼び出し側で避ける）"""
    if cards:
        room.reserve.extend(cards)

def flow_field(room: Room) -> None:
    """場が流れたときの共通処理：場を空にし、予備軍を山札の“下”に戻す（順序保持）"""
    room.field = []
    room.last_number = None
    if room.reserve:
        room.deck.extend(room.reserve)  # pop(0)で上から引く設計なので、extendは“下に戻す”
        room.reserve.clear()

def return_cards_to_deck_bottom(room, cards: List[dict]) -> None:
    """合成数の『消費カード』を即座に山札の底に戻す。場は流さない。"""
    if not cards:
        return
    room.deck.extend(cards)

def get_penalty_card_count(rule: PenaltyRule, field_card_count: int, normal_card_count: int) -> int:
    """
    ペナルティ枚数を返す。
      ALWAYS_1    -> 1
      FIELD_COUNT -> 場の枚数
      NORMAL      -> 通常ルールの枚数
    """
    if rule is PenaltyRule.ALWAYS_1:
        return 1
    if rule is PenaltyRule.FIELD_COUNT:
        return field_card_count
    if rule is PenaltyRule.NORMAL:
        return normal_card_count
    return normal_card_count

def missing_registered_prime_players(room: Room) -> List["Player"]:
    if room.rule.prime_rule is not PrimeRule.REGISTERED:
        return []
    return [
        p for p in get_active_players(room)
        if not p.registered_primes and not p.registered_composites
    ]

def registered_numbers_update_payload(prime_result, composite_result) -> dict:
    return {
        "type": "registered_numbers_updated",
        "prime_count": len(prime_result.prime_values),
        "composite_count": len(composite_result.composite_values),
        "prime_duplicate_count": prime_result.duplicate_count,
        "composite_duplicate_count": composite_result.duplicate_count,
        "prime_errors": [asdict(error) for error in prime_result.errors],
        "composite_errors": [asdict(error) for error in composite_result.errors],
        "truncated": prime_result.truncated or composite_result.truncated,
    }

def replace_player_registered_numbers_from_text(
    player: "Player",
    prime_text: str,
    composite_text: str,
) -> dict:
    prime_result = parse_registered_prime_text(prime_text)
    composite_result = parse_registered_composite_text(composite_text)
    player.replace_registered_primes(set(prime_result.prime_values))
    player.replace_registered_composites(set(composite_result.composite_values))
    return registered_numbers_update_payload(prime_result, composite_result)

def load_sample_memory() -> tuple[tuple[int, ...], tuple[int, ...], str, str]:
    if not SAMPLE_MEMORY_JSON.exists():
        return (), (), "", ""
    data = json.loads(SAMPLE_MEMORY_JSON.read_text(encoding="utf-8-sig"))
    prime_text = str(data.get("primeText", "")).strip()
    composite_text = "\n".join(
        part.strip()
        for part in (
            str(data.get("compositeText", "")).strip(),
            str(data.get("additionalCompositeText", "")).strip(),
        )
        if part.strip()
    )
    values = []
    for line in prime_text.splitlines():
        token = line.strip()
        if token:
            values.append(int(token))
    composite_values = parse_registered_composite_text(composite_text).composite_values
    return tuple(sorted(set(values))), composite_values, prime_text, composite_text

(
    SAMPLE_REGISTERED_PRIMES,
    SAMPLE_REGISTERED_COMPOSITES,
    SAMPLE_REGISTERED_PRIME_TEXT,
    SAMPLE_REGISTERED_COMPOSITE_TEXT,
) = load_sample_memory()

def load_sample_registered_prime_payload(player: "Player") -> dict:
    player.replace_registered_primes(set(SAMPLE_REGISTERED_PRIMES))
    player.replace_registered_composites(set(SAMPLE_REGISTERED_COMPOSITES))
    return {
        "type": "registered_numbers_updated",
        "prime_count": len(player.registered_primes),
        "composite_count": len(player.registered_composites),
        "prime_duplicate_count": 0,
        "composite_duplicate_count": 0,
        "prime_errors": [],
        "composite_errors": [],
        "truncated": False,
        "sample": True,
        "sample_prime_text": SAMPLE_REGISTERED_PRIME_TEXT,
        "sample_composite_text": SAMPLE_REGISTERED_COMPOSITE_TEXT,
    }
################################################
# Webhook
################################################

async def notify_discord(content: str):
    if not WEBHOOK_URL:
        print("⚠️ Webhook URL が設定されていません")
        return

    try:
        async with httpx.AsyncClient() as client:
            await client.post(WEBHOOK_URL, json={"content": content})
    except Exception as e:
        # エラーをハンドリング
        print("notify_discord failed:", e)

################################################
# WebSocket処理
################################################

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    player = Player(websocket)  # 辞書ではなくPlayerクラスのインスタンスを生成

    try:
        # 自分のIDを通知
        await websocket.send_json({"type": "your_id", "id": player.id})

        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")
            room_id = player.room.room_id if player.room else None

            if msg_type == "set_name":
                # クライアントから名前を受け取る
                player.name = data.get("name", "").strip() or f"プレイヤー{player.id}"
                # 必要なら acknowledgment を返す
                await player.send_json({"type": "name_set", "name": player.name})
                continue
            elif msg_type in ("set_registered_numbers", "set_registered_primes"):
                if player.room and player.room.state == "playing":
                    await player.send_json({
                        "type": "error",
                        "message": "対戦中は登録内容を変更できません。",
                    })
                    continue

                prime_text = data.get("prime_text", data.get("text", ""))
                composite_text = data.get("composite_text", "")
                if not isinstance(prime_text, str) or not isinstance(composite_text, str):
                    await player.send_json({
                        "type": "error",
                        "message": "登録内容の入力形式が不正です。",
                    })
                    continue

                await player.send_json(replace_player_registered_numbers_from_text(
                    player,
                    prime_text,
                    composite_text,
                ))

                if player.room:
                    await player.room.update_room_status()
                continue
            elif msg_type == "load_sample_registered_primes":
                if player.room and player.room.state == "playing":
                    await player.send_json({
                        "type": "error",
                        "message": "対戦中は登録内容を変更できません。",
                    })
                    continue
                if not SAMPLE_REGISTERED_PRIMES and not SAMPLE_REGISTERED_COMPOSITES:
                    await player.send_json({
                        "type": "error",
                        "message": "サンプル登録メモリがサーバーに見つかりません。",
                    })
                    continue

                await player.send_json(load_sample_registered_prime_payload(player))

                if player.room:
                    await player.room.update_room_status()
                continue
            elif msg_type == "get_room_counts":
                counts = {room_id: len(room.players) for room_id, room in rooms.items()}
                rules  = {rid: room.rule.label for rid, room in rooms.items()}
                allow_composite = {rid: room.rule.allow_composite for rid, room in rooms.items()}
                prime_rules = {rid: room.rule.prime_rule.name.lower() for rid, room in rooms.items()}
                await websocket.send_json({
                    "type": "room_counts",
                    "counts": counts,
                    "rules": rules,
                    "allow_composite": allow_composite,
                    "prime_rules": prime_rules,
                })

            elif msg_type == "join_room":
                rid = data["room_id"]
                room = rooms[rid]

                if len(room.players) >= 10:
                    await websocket.send_json({"type": "error", "message": "部屋が満員です。"})
                    continue

                await room.log_chat(f"{player.name}が入室しました")
                # 同期処理の後で、バックグラウンドに通知タスクを投げる
                asyncio.create_task(
                    notify_discord(f"🎮 {player.name} が {room.room_id}（{room.rule.label}）に参加しました")
                )


                room.players.append(player)
                player.room = room
                player.status = "watching"  # 仮に入室したらwatchingに

                await room.update_room_status()
                await room.broadcast({
                    "type": "room_state_initialization",
                    "room_state": room.state,
                    "allow_composite": room.rule.allow_composite,
                    "prime_rule": room.rule.prime_rule.name.lower(),
                })

            elif msg_type == "leave_room":
                await leave_room(player)

            elif msg_type == "change_status":
                if not player.room:  # 部屋にいなければ無視
                    continue
                room = player.room
                new_status = data["status"]
                player.status = new_status
                if new_status != "waiting":
                    player.clear_hand()
                    await player.send_hand_update()
                await room.update_room_status()
                if room.state == "playing" and await room.try_end_game():
                    await room.update_room_status()

            elif msg_type == "start_game":
                if not player.room:
                    continue
                room = player.room

                # 対戦待ちプレイヤー確認
                waiting_players = get_active_players(room)
                if len(waiting_players) != 2:
                    await websocket.send_json({"type": "error", "message": "対戦待ちが2人必要です。"})
                    continue
                missing_registered = missing_registered_prime_players(room)
                if missing_registered:
                    names = ", ".join(p.name for p in missing_registered)
                    await websocket.send_json({
                        "type": "error",
                        "message": f"登録素数が未設定のプレイヤーがいます: {names}",
                    })
                    continue

                await start_game(room)

            elif msg_type == "play_card":
                if not player.room:
                    continue
                room = player.room
                if player.id != room.current_turn_id:
                    await websocket.send_json({"type": "error", "message": "あなたのターンではありません。"})
                    continue

                # モードごとに対応する関数を実行
                mode = (data.get("mode") or "prime").lower()
                try:
                    if mode == "composite":
                        if not room.rule.allow_composite:
                            await websocket.send_json({"type": "error", "message": "この部屋では合成数出しは使えません。"})
                            continue
                        await handle_composite_play(player, room, data)
                    else:
                        await handle_prime_play(player, room, data)
                except CompositeError as e:
                    await websocket.send_json({"type":"error","message":e.msg})



            elif msg_type == "draw_card":
                if not player.room:
                    continue
                room = player.room
                if player.id != room.current_turn_id:
                    await websocket.send_json({"type": "error", "message": "あなたのターンではありません。"})
                    continue

                # すでにこのターンでドローしているかチェック
                if room.has_drawn == True:
                    await websocket.send_json({"type": "error", "message": "このターンはすでにドロー済みです。"})
                    continue

                # ドロー処理
                if len(room.deck) > 0:
                    drawn = room.deck.pop(0)
                    player.add_card(drawn)
                    record_score_line(room, f"{player.name}:{score_state_prefix(room)}D({score_card_symbol(drawn)})")

                    # 自分に手札更新を送る
                    await player.send_hand_update()
                    await room.update_game_state()
                    # ドロー済みフラグを設定（このターンはこれ以上ドローできない）
                    room.has_drawn = True
                    # ※ここでは next_turn(room) は呼ばない → 手番は変わらない

            elif msg_type == "pass":
                if not player.room:
                    continue
                room = player.room
                if player.id != room.current_turn_id:
                    await websocket.send_json({"type": "error", "message": "あなたのターンではありません。"})
                    continue

                await player.send_hand_update()

                # パスの場合もフィールドをリセット
                flow_field(room)

                # passの通知
                await room.update_game_state()
                await room.broadcast({
                    "type": "action_result",
                    "action": "pass",
                    "player_id": player.id
                })
                # チャットにパスのログを流す
                await room.log_chat(f"{player.name}がパスしました")
                record_score_line(room, f"{player.name}:{score_state_prefix(room)}%")
                # 次のターンへ
                await next_turn(room)

            elif msg_type == "chat":
                if not player.room:
                    continue
                # 表示用に「プレイヤー」を追加
                display_sender = f"{player.name}"
                await room.broadcast({
                    "type": "chat",
                    "sender": display_sender,
                    "message": data["message"]
                })

    except WebSocketDisconnect:
        await leave_room(player)

################################################
# カードプレイ時の判定
################################################
async def handle_prime_play(player: Player, room: Room, data: dict) -> None:
    # 既存の "cards" + "assigned_numbers" で連結 → 特別数(57,1729) → 素数チェック
    played_cards = data.get("cards", [])
    score_prefix = score_state_prefix(room)
    # 手札にあるか検証
    if not player.has_cards(played_cards):
        await player.ws.send_json({"type": "error", "message": "そのカードは手札にありません。"})
        return

    # ジョーカー絡みの処理
    assigned_numbers = data.get("assigned_numbers", [])  # [ "inf" か 0〜13, ... ]
    # ―――――――――――――――――――
    # １）ジョーカーだけを単独で出す (グロタンカット相当)
    jokers = [c for c in played_cards if c["suit"] == "X"]
    if len(jokers) == 1 and len(played_cards) == 1:
        push_to_reserve(room, played_cards)
        # ジョーカー1枚だけ → 場を流す
        player.remove_card(jokers[0])
        # 場を流して予備軍を山へ戻す
        flow_field(room)
        room.has_drawn = False
        await player.send_hand_update()
        await room.log_chat(f"{player.name}がジョーカーを出しました、インフィニティ！")
        record_score_line(room, f"{player.name}:{score_prefix}X[IN]{score_win_suffix(player)}")
        await room.update_game_state()
        if await room.try_end_game():
            await room.update_room_status()
        return  # ターン継続
    # ２）ジョーカーを含む複数枚プレイ時は、置換して number を作成
    if jokers:
        if len(assigned_numbers) != len(jokers):
            await player.ws.send_json({
                "type": "error",
                "message": "ジョーカーの数字指定が不足しています。"
            })
            return
        if any(v == "inf" for v in assigned_numbers):
            await player.ws.send_json({
                "type": "error",
                "message": "複数枚出し時に「∞」指定はできません。"
            })
            return
        ranks = []
        joker_i = 0
        for c in played_cards:
            if c["suit"] == "X":
                val = assigned_numbers[joker_i]
                joker_i += 1
                ranks.append(str(val))
            else:
                ranks.append(str(c["rank"]))
        ranks_str = "".join(ranks)

        # 先頭が 0 の数字は許可しない
        if ranks_str.startswith("0"):
            await player.ws.send_json({
                "type": "error",
                "message": "最上位桁が0の数字は出せません。"
            })
            return

        try:
            number = int(ranks_str)
        except ValueError:
            number = -1
    else:
        # 通常カードのみ
        ranks_str = "".join(str(c["rank"]) for c in played_cards)
        try:
            number = int(ranks_str)
        except ValueError:
            number = -1

    # もしフィールドに既にカードが出ているなら、枚数と数の検証を行う
    if room.field:
        # ① 枚数チェック
        if len(played_cards) != len(room.field):
            await player.ws.send_json({"type": "error", "message": "枚数が違います。"})
            return

        # ② 数値チェック：フィールドのカードと比較
        field_number = room.last_number if room.last_number is not None else -1

        # 通常は「>」が必要、反転中は「<」を要求
        if not room.reverse_order:
            if number <= field_number:
                await player.ws.send_json({"type": "error", "message": "場より大きい数字を出してください。"})
                return
        else:
            if number >= field_number:
                await player.ws.send_json({"type": "error", "message": "場より小さい数字を出してください。(ラマヌジャン革命中)"})
                return

    # グロタンカット
    if number == 57:
        # 出した順そのまま予備軍に
        push_to_reserve(room, played_cards)
        for c in played_cards:
            player.remove_card(c)
        # 場を流して予備軍を山へ戻す
        flow_field(room)
        # 自分の手番を継続するため next_turn は呼ばない
        room.has_drawn = False
        # クライアントの表示を更新
        await player.send_hand_update()
        await room.log_chat(f"{player.name}が57を出しました、グロタンカット！")
        play_text = score_cards_text(played_cards) + score_joker_suffix(played_cards, assigned_numbers)
        record_score_line(room, f"{player.name}:{score_prefix}{play_text}[GC]{score_win_suffix(player)}")
        await room.update_game_state()
        if await room.try_end_game():
            await room.update_room_status()
            return
        return  # 次の処理（素数判定～next_turn）をすべてスキップ
    if number == 1729:
        # フラグをトグル
        room.reverse_order = not room.reverse_order
        # カードを場に出す
        push_to_reserve(room, played_cards)
        for c in played_cards:
            player.remove_card(c)
        room.field = played_cards
        room.last_number = number

        # 手札更新 & ゲーム状態通知
        await player.send_hand_update()
        await room.update_game_state()
        # ログ
        await room.log_chat(f"{player.name}が1729を出しました、ラマヌジャン革命！")
        play_text = score_cards_text(played_cards) + score_joker_suffix(played_cards, assigned_numbers)
        record_score_line(room, f"{player.name}:{score_prefix}{play_text}[RR]{score_win_suffix(player)}")

        # 通常の素数出しと同じく次のターンへ
        await next_turn(room)
        return
    # 素数判定
    if not is_valid_prime_for_player(number, player, room.rule):
        # ペナルティ
        # 出そうとしたカードを引き直すことはしない(そもそも出されていないため)
        penalty_cards = get_penalty_card_count(
            room.rule.penalty_rule,
            field_card_count=len(played_cards),
            normal_card_count=len(played_cards),
        )
        drawn_penalties = []
        for _ in range(penalty_cards):
            if room.deck:
                drawn = room.deck.pop(0)
                player.add_card(drawn)
                drawn_penalties.append(drawn)

        # フィールドをリセット（場のカードを消す）2人対戦想定であることに注意
        flow_field(room)

        await player.send_hand_update()
        await room.update_game_state()
        await room.broadcast( {
            "type": "penalty",
            "player_id": player.id,
            "played_cards": played_cards,
            "number": number
        })

        # チャットにペナルティのログを流す
        rule_name = rule_display_name(room.rule.prime_rule)
        await room.log_chat(f"{player.name}が{number}を出そうとしましたが、{number}は{rule_name}ではありません")
        play_text = score_cards_text(played_cards) + score_joker_suffix(played_cards, assigned_numbers)
        record_score_line(
            room,
            f"{player.name}:{score_prefix}{play_text},P({score_cards_text(drawn_penalties, sort_cards=True)})"
        )

        await next_turn(room)
        return

    # 素数なら場に出す
    push_to_reserve(room, played_cards)
    for c in played_cards:
        player.remove_card(c)
    room.field = played_cards
    room.last_number = number

    await player.send_hand_update()

    await room.update_game_state()
    await room.broadcast({
        "type": "action_result",
        "action": "play_card",
        "player_id": player.id,
        "played_cards": played_cards,
        "number": number
    })

    # チャットに「素数を出した」ログを流す
    await room.log_chat(f"{player.name}が{number}を出しました")
    play_text = score_cards_text(played_cards) + score_joker_suffix(played_cards, assigned_numbers)
    record_score_line(room, f"{player.name}:{score_prefix}{play_text}{score_win_suffix(player)}")
    await next_turn(room)

# 現行ルールでは指数が122を超える合法手が存在しないため、
# 計算量を抑える実用上の上限として122を採用する。
MAX_EXP = 122

# エラーメッセージ & 分類
class CompositeError(Exception):
    def __init__(self, msg: str):
        self.msg = msg
        super().__init__(msg)
# 文法エラー（やり直し）
class CompositeSyntaxError(CompositeError):
    pass

# 計算誤り（ペナルティ）
class CompositeMathError(CompositeError):
    pass

def map_joker_values_in_cards(cards: List[dict], assigned: List[str], allow_inf_singleton: bool) -> List[int]:
    """
    cards の並びを整数列(ランク)にする。Jokerは assigned で置換。
    allow_inf_singleton が True のときのみ、Joker1枚・単独・"inf" を許す（場流し扱いへ）。
    """
    jokers = [c for c in cards if c["suit"] == "X"]
    if len(jokers) != len(assigned):
        raise CompositeError("ジョーカーの数字指定が不足しています。")

    # 単独 Joker ∧ allow_inf_singleton のみ "inf" を許す
    if any(v == "inf" for v in assigned):
        if not (allow_inf_singleton and len(cards) == 1 and len(jokers) == 1):
            raise CompositeError("この状況で∞は使用できません。")

    out = []
    ji = 0
    for c in cards:
        if c["suit"] == "X":
            v = assigned[ji]
            ji += 1
            if v == "inf":
                out.append("inf")  # 単独流しだけこのまま返す
            else:
                out.append(int(v))
        else:
            out.append(c["rank"])
    return out

def build_int_from_cards(seq: List[int]) -> int:
    s = "".join(str(x) for x in seq)
    if s.startswith("0"):
        raise CompositeError("最上位桁が0の数は作れません。")
    return int(s)

def parse_and_eval_composite(
    tokens: List[dict],
    token_card_ranks: Dict[str, int],
    rule: RulePreset,
) -> Tuple[int, List[str]]:
    """
    tokens: [{kind:'card', card_id:...} | {kind:'op', op:'×'|'^'}]
    token_card_ranks: card_id -> ランク（Jokerは割当後）
    joker_values: （未使用、説明簡略化）
    return: (value, used_card_ids)

    許可する構文:
      card+ ( (×|^) card+ )*
    つまり
      - カードは連続して整数を作ってよい
      - 演算子は連続不可
      - 先頭末尾はカード
    """
    if not tokens:
        raise CompositeSyntaxError("合成数の式が空です。")

    if tokens[0]["kind"] != "card" or tokens[-1]["kind"] != "card":
        raise CompositeSyntaxError("式の先頭と末尾はカードである必要があります。")

    # 1) 演算子の基本構文チェック
    prev_kind = None
    for i, t in enumerate(tokens):
        kind = t.get("kind")

        if kind not in ("card", "op"):
            raise CompositeSyntaxError("不正なトークン種別があります。")

        if kind == "op":
            op = t.get("op")
            if op not in ("×", "^"):
                raise CompositeSyntaxError(f"不正な演算子 {op} です。")

            # 演算子が先頭末尾に来るのは不可
            if i == 0 or i == len(tokens) - 1:
                raise CompositeSyntaxError("演算子を式の先頭・末尾には置けません。")

            # 演算子の連続は禁止
            if prev_kind == "op":
                raise CompositeSyntaxError("演算子を連続して置くことはできません。")

        prev_kind = kind

    # 2) “×” で分割
    chunks: List[List[dict]] = []
    cur: List[dict] = []
    for t in tokens:
        if t["kind"] == "op" and t["op"] == "×":
            if not cur:
                raise CompositeSyntaxError("× の前後が不正です。")
            chunks.append(cur)
            cur = []
        else:
            cur.append(t)
    if not cur:
        raise CompositeSyntaxError("× の後に数字が必要です。")
    chunks.append(cur)

    used_card_ids: List[str] = []
    total_value = 1

    # 3) 各 chunk を「card+ (^ card+)*」として解釈
    for ch in chunks:
        seqs: List[List[int]] = []
        temp_cards: List[str] = []

        cur_cards: List[int] = []
        cur_ids: List[str] = []

        for t in ch:
            if t["kind"] == "card":
                cid = t["card_id"]
                if cid not in token_card_ranks:
                    raise CompositeSyntaxError("未知のカードが指定されました。")
                cur_cards.append(token_card_ranks[cid])
                cur_ids.append(cid)

            else:
                # chunk 内に残ってよい演算子は ^ のみ
                if t["op"] != "^":
                    raise CompositeSyntaxError("× は分割済みのはずです。")
                if not cur_cards:
                    raise CompositeSyntaxError("^ の前後に数字が必要です。")

                seqs.append(cur_cards)
                temp_cards.extend(cur_ids)
                cur_cards, cur_ids = [], []

        # 末尾の整数を追加
        if not cur_cards:
            raise CompositeSyntaxError("式の末尾が不正です。")
        seqs.append(cur_cards)
        temp_cards.extend(cur_ids)

        # 4) 各 card 列を整数化
        ints = [build_int_from_cards(s) for s in seqs]

        # 5) 底の条件
        base = ints[0]
        if base < 2:
            raise CompositeSyntaxError("底が0または1は不可です。")
        if not is_valid_prime_by_rule(base, rule):
            kind = rule_display_name(rule.prime_rule)
            raise CompositeMathError(f"底 {base} が{kind}ではありません。")

        # 6) 指数連鎖を右結合で評価
        if len(ints) == 1:
            exp = 1
        else:
            exp = ints[-1]
            if exp > MAX_EXP:
                raise CompositeMathError(f"指数 {exp} が上限 {MAX_EXP} を超えています。")

            for e in reversed(ints[1:-1]):
                if e > MAX_EXP:
                    raise CompositeMathError(f"指数 {e} が上限 {MAX_EXP} を超えています。")
                exp = pow(e, exp)
                if exp > MAX_EXP:
                    raise CompositeMathError(f"合成された指数 {exp} が上限 {MAX_EXP} を超えています。")

        value = pow(base, exp)
        total_value *= value
        used_card_ids.extend(temp_cards)

    return total_value, used_card_ids

async def handle_composite_play(player: Player, room: Room, data: dict) -> None:
    # 0) 手番 & 手札 所有チェック（共通）
    selected = data.get("selected", {}) or {}
    consume  = data.get("consume", {}) or {}
    comp     = data.get("composite", {}) or {}
    sel_cards: List[dict] = selected.get("cards", [])
    con_cards: List[dict] = consume.get("cards", [])
    comp_tokens: List[dict] = comp.get("tokens", [])
    sel_assigned: List[str] = selected.get("assigned_numbers", [])
    comp_assigned: List[str] = comp.get("assigned_numbers", [])
    score_prefix = score_state_prefix(room)

    # composite.tokens から材料札を再構成（見せ札と材料札は常に別）
    token_card_ids = [t.get("card_id") for t in comp_tokens if t.get("kind") == "card"]
    token_card_ids = [cid for cid in token_card_ids if cid is not None]
    if token_card_ids:
        hand_by_id = {c["card_id"]: c for c in player.hand}
        con_cards = [hand_by_id[cid] for cid in token_card_ids if cid in hand_by_id]
    score_cards_by_id = {c["card_id"]: c for c in (sel_cards + con_cards)}
    score_composite_text = (
        f"{score_cards_text(sel_cards)}={score_tokens_text(comp_tokens, score_cards_by_id)}"
        f"{score_joker_suffix(sel_cards + con_cards, sel_assigned + comp_assigned)}"
    )

    # 手札に全部あるか
    all_consume = list({c["card_id"]:c for c in (sel_cards + con_cards)}.values())
    if not player.has_cards(all_consume):
        await player.ws.send_json({"type": "error", "message": "そのカードは手札にありません。"})
        return

    # 1) Joker 検証（選択側）: 合成数モードでは∞は常に禁止（単独流しも不可）
    try:
        # 値の割当チェックのみ行い、∞は許可しない
        map_joker_values_in_cards(sel_cards, sel_assigned, allow_inf_singleton=False)
    except CompositeError as e:
        await player.ws.send_json({"type":"error","message":e.msg});
        return

    # 2) 合成数場 Joker 割当
    #   comp_tokens 上に Joker が m 枚出現していることを数え、その m と comp_assigned の長さが一致、かつ inf を含まないことを要求
    comp_joker_count = 0
    card_by_id = { c["card_id"]: c for c in player.hand }
    for t in comp_tokens:
        if t.get("kind") == "card":
            c = card_by_id.get(t["card_id"])
            if c and c.get("is_joker"): comp_joker_count += 1
    if comp_joker_count != len(comp_assigned) or any(v=="inf" for v in comp_assigned):
        await player.ws.send_json({"type":"error","message":"合成数内のジョーカー指定が不正です。"})
        return

    # 3) token_card_ranks を作る（合成数トークンの “card_id → ランク”）
    #    Joker は comp_assigned を登場順に置換
    token_card_ranks: Dict[str,int] = {}
    jidx = 0
    for t in comp_tokens:
        if t.get("kind") == "card":
            cid = t["card_id"]
            c   = card_by_id.get(cid)
            if not c:
                await player.ws.send_json({"type":"error","message":"未知のカードが式に含まれています。"}); return
            if c.get("is_joker"):
                token_card_ranks[cid] = int(comp_assigned[jidx]); jidx += 1
            else:
                token_card_ranks[cid] = int(c["rank"])

    # 4) 早期チェック：枚数・大小は selected のみで判定（合成数のパース前）
    # 4-1) 枚数（場があるときは selected の枚数と一致必須）
    if room.field:
        if len(sel_cards) != len(room.field):
            await player.ws.send_json({"type":"error","message":"枚数が違います。"})
            return

    # 4-2) 大小（selected を連結して得た sel_number で比較）
    #      ※ 合成数モードでは ∞ 不可／先頭0不可
    try:
        sel_ranks = map_joker_values_in_cards(sel_cards, sel_assigned, allow_inf_singleton=False)
    except CompositeError as e:
        await player.ws.send_json({"type":"error","message":e.msg})
        return

    sel_str = "".join(str(x) for x in sel_ranks)
    if sel_str.startswith("0"):
        await player.ws.send_json({"type":"error","message":"最上位桁が0の数字は出せません。"})
        return
    sel_number = int(sel_str) if sel_str else -1

    if room.field:
        field_number = room.last_number if room.last_number is not None else -1
        if (not room.reverse_order and sel_number <= field_number) or (room.reverse_order and sel_number >= field_number):
            await player.ws.send_json({
                "type":"error",
                "message": ("場より大きい数字を出してください。" if not room.reverse_order else "場より小さい数字を出してください。(ラマヌジャン革命中)")
            })
            return

    # 5) 合成数の構文・評価（con 側）。構文はエラー返し、計算はペナルティ。
    try:
        number, used_ids = parse_and_eval_composite(comp_tokens, token_card_ranks, room.rule)
        # con を全て掛け合わせた number と sel_number は一致必須（不一致は MathError → ペナルティ）
        if number != sel_number:
            raise CompositeMathError("選択カードの数と合成数の値が一致しません。")
        if (
            room.rule.prime_rule is PrimeRule.REGISTERED
            and not player.can_use_registered_composite(sel_number)
        ):
            raise CompositeMathError(f"{sel_number}は本人の登録済み合成数に含まれていません。")
    except CompositeSyntaxError as e:
        await player.ws.send_json({"type":"error","message":e.msg})
        return
    except CompositeMathError as e:
        penalty_cards = get_penalty_card_count(
            room.rule.penalty_rule,
            field_card_count=len(sel_cards),
            normal_card_count=len(all_consume),
        )
        drawn_penalties = []
        for _ in range(penalty_cards):
            if room.deck:
                drawn = room.deck.pop(0)
                player.add_card(drawn)
                drawn_penalties.append(drawn)
        flow_field(room)
        await player.send_hand_update()
        await room.update_game_state()
        await room.broadcast({
            "type": "penalty",
            "player_id": player.id,
            "played_cards": sel_cards,
            "number": sel_number
        })
        await room.log_chat(f"{player.name}の合成数は不正でした（{e.msg}）。ペナルティ。")
        record_score_line(
            room,
            f"{player.name}:{score_prefix}{score_composite_text},P({score_cards_text(drawn_penalties, sort_cards=True)})"
        )
        await next_turn(room)
        return

    # 7) すべてOK → 札を「出した順」でreserveに積む → 手札から除去
    #    出した順は UI から渡す順序（selected→consume）で良ければそのまま。必要なら tokens から順序を決める。
    push_to_reserve(room, sel_cards)

    # selected と重複するカードは deck に戻さない
    sel_ids = {c["card_id"] for c in sel_cards}
    con_only = [c for c in con_cards if c["card_id"] not in sel_ids]
    return_cards_to_deck_bottom(room, con_only)


    # 手札からは selected/consume 全部を除去（all_consume はユニーク化済み想定）
    for c in all_consume:
        player.remove_card(c)

    # field には sel 側が残る仕様。大小・一致は sel_number 基準。
    room.field = sel_cards # 合成数は流すのでカウントされない
    room.last_number = sel_number

    await player.send_hand_update()
    await room.update_game_state()
    await room.broadcast({
        "type":"action_result",
        "action":"play_card",
        "player_id": player.id,
        "played_cards": room.field,
        "number": sel_number,
        "mode": "composite"
    })
    await room.log_chat(f"{player.name}が合成数 {sel_number} を出しました")
    record_score_line(room, f"{player.name}:{score_prefix}{score_composite_text}{score_win_suffix(player)}")
    await next_turn(room)


################################################
# 部屋からの退出
################################################
async def leave_room(player):
    # デバッグ用にplayer.roomの状態を出力
    print(f"DEBUG: player.room before leave_room: {player.room}")

    # もしNoneの場合は注意喚起のログも出す
    if player.room is None:
        print("DEBUG: player.room is None; cannot proceed with leave_room processing")
        return

    room_id = player.room.room_id
    if room_id and player in rooms[room_id].players:
        room = player.room
        rooms[room_id].players.remove(player)
        player.room = None

        # 退出通知
        await room.log_chat(f"{player.name}が退室しました")
        if room.state == "playing" and player.status == "waiting":
            record_score_line(room, f"{player.name}:退出")
        player.clear_hand()

        # ゲーム中の特別処理
        if room.state == "playing":
            active_players = get_active_players(room)
            if len(active_players) == 1:
                winner_name = active_players[0].name
                room.state = "waiting"
                room.current_turn_id = None
                await room.broadcast({"type": "game_over", "winner": winner_name, "state": room.state})
                await room.log_chat(f"{winner_name}が勝利しました")
                await publish_score_log(room, winner_name)
            elif len(active_players) == 0:
                room.state = "waiting"
                room.current_turn_id = None
                await room.broadcast({"type": "game_over", "winner": None, "state": room.state})
                await room.log_chat("対戦者がいなくなったためゲームを終了しました")
                await publish_score_log(room, None)
            elif room.current_turn_id == player.id: # 現在ターンのプレイヤーが切断した場合、次のターンに進める
                await next_turn(room)


        # 改めて抜けたプレイヤーには各roomの人数を送る機能を追加
        await room.update_room_status()


################################################
# ゲーム開始処理
################################################
async def start_game(room):
    room.reverse_order = room.rule.start_revolution     # 革命はルールごとの開始時コンディションに戻す
    room.has_drawn = False         # ドロー済みフラグもクリア

    # 1) 待機中の2人を確定
    waiting_players = get_active_players(room)
    if len(waiting_players) != 2:
        return
    p1, p2 = waiting_players
    for p in room.players:
        if p not in waiting_players:
            p.clear_hand()
            await p.send_hand_update()

    # 2) デッキ生成→配布（プリセット準拠）
    deck = build_deck(room.rule)
    hands, remaining = shuffle_and_deal(deck, room.rule.hand_size, num_players=2)
    p1.hand, p2.hand = hands[0], hands[1]
    room.deck = remaining

    room.reserve = []
    room.field = []  # 場のカードは空
    room.last_number = None
    room.score_log = []
    p1.sort_hand()
    p2.sort_hand()
    record_score_line(room, f"{p1.name}:({score_cards_text(p1.hand, sort_cards=True)})")
    record_score_line(room, f"{p2.name}:({score_cards_text(p2.hand, sort_cards=True)})")
    room.state = "playing"

    # ランダムに先攻プレイヤー決定
    room.current_turn_id = random.choice([p1.id, p2.id])

    # プレイヤーそれぞれに手札情報を送信
    await p1.ws.send_json({"type": "deal","your_hand": p1.hand})
    await p2.ws.send_json({"type": "deal","your_hand": p2.hand})

    # 全体にゲーム開始 & 現在のターン情報
    await room.broadcast({
        "type": "game_start",
        "allow_composite": room.rule.allow_composite,
        "prime_rule": room.rule.prime_rule.name.lower(),
    })
    await room.update_game_state()
    # チャットにログを流す
    await room.log_chat("ゲーム開始！")


################################################
# 次のターンに移る
################################################
async def next_turn(room):
    # ターンが変わるので、ドロー済みフラグをリセットする
    room.has_drawn = False

    # 対戦に参加している（statusが"waiting"の）プレイヤーだけを対象とする
    active_players = get_active_players(room)
    if len(active_players) < 2:
        return

    if await room.try_end_game():
        await room.update_room_status()
        return

    current_turn_id = room.current_turn_id
    # 現在の手番プレイヤーが active_players の中にいるかを確認
    idx = [i for i, p in enumerate(active_players) if p.id == current_turn_id]
    if not idx:
        # もし現在の手番プレイヤーが active でなければ、先頭のプレイヤーに設定
        room.current_turn_id = active_players[0].id
    else:
        # 元の順番を無視しているようだが2人対戦の間は大丈夫か？
        current_idx = idx[0]
        next_idx = (current_idx + 1) % len(active_players)
        room.current_turn_id = active_players[next_idx].id

    # await room.update_game_state() それぞれのアクションで既に呼び出されているので省略
    # 次のプレイヤー名を取得して送信
    next_player = next((p for p in room.players if p.id == room.current_turn_id), None)
    await room.broadcast({
        "type": "next_turn",
        "current_turn": next_player.name if next_player else None,
    })
