from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from dataclasses import asdict
from pathlib import Path
from functools import lru_cache
from typing import Dict, List, Optional, Tuple
from rules import PRESETS, RulePreset, DeckRule, PenaltyRule, PrimeRule
from registered_primes import parse_registered_composite_text, parse_registered_prime_text
from cpu_player import CpuPlayer, choose_cpu_action, is_cpu_player
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

ASSIST_LIMITS = {
    "ten": 10,
    "fifty": 50,
    "many": 50,
}
ASSIST_SCAN_LIMITS = {
    "ten": 500,
    "fifty": 2000,
    "many": 2000,
}
ASSIST_REALIZATIONS_PER_NUMBER = 4

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
    def __init__(self, room_id: str, rule: RulePreset, category: str = "Classic"):
        self.room_id = room_id
        self.rule: RulePreset = rule
        self.category = category
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
        disconnected = []
        for p in list(self.players):
            try:
                await p.send_json(message)
            except Exception as exc:
                print(f"broadcast failed in {self.room_id}: {exc}")
                disconnected.append(p)
        for p in disconnected:
            if p in self.players:
                self.players.remove(p)
            if p.room is self:
                p.room = None
                p.status = "watching"
                p.clear_hand()

    async def update_room_status(self):
        message = {
            "type": "update_room_status",
            "room_id": self.room_id,
            "rule": self.rule.label,
            "category": self.category,
            "allow_composite": self.rule.allow_composite,
            "prime_rule": self.rule.prime_rule.name.lower(),
            "assist_enabled": self.rule.assist_enabled,
            "registration_enabled": self.rule.registration_enabled,
            "count": len(self.players),
            "player_list": [
                {
                    "id": p.id,
                    "name": p.name,
                    "status": p.status,
                    "is_cpu": is_cpu_player(p),
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
            "category": self.category,
            "current_turn": current_name,
            "revolution": self.reverse_order,
            "allow_composite": self.rule.allow_composite,
            "prime_rule": self.rule.prime_rule.name.lower(),
            "assist_enabled": self.rule.assist_enabled,
            "registration_enabled": self.rule.registration_enabled,
            "deck_count": len(self.deck),
            "field": self.field,
            "player_list": [
                {
                    "id": p.id,
                    "name": p.name,
                    "status": p.status,
                    "is_cpu": is_cpu_player(p),
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
    ("room_1", PRESETS["std-5-1"], "Classic"),
    ("room_2", PRESETS["half-7-1-c"], "Classic"),
    ("room_3", PRESETS["std-7-1"], "Classic"),
    ("room_4", PRESETS["std-11-f-c"], "Classic"),
    ("room_5", PRESETS["std-11-n-c"], "Classic"),
    ("room_6", PRESETS["std-11-n-no-c"], "Classic"),
    ("room_7", PRESETS["std-11-n-c-rev"], "Plus"),
    ("room_8", PRESETS["tetrad-11-n-c"], "Plus"),
    ("room_9", PRESETS["semiprime-11-n-c"], "Plus"),
    ("room_13", PRESETS["registered-11-n"], "Neo"),
    ("room_14", PRESETS["registered-11-n-assist"], "Neo"),
    ("room_15", PRESETS["neo-assist-11-n-unlimited"], "Neo"),
]
ROOM_DESCRIPTIONS = {
    "room_15": (
        "登録した素数・合成数をもとにアシスト候補を表示する部屋です。"
        "登録リストによる使用制限はないため、登録していない素数も通常通り出せます。\n"
        "素数候補欄では、検索対象を手札全体・選択中・未選択から切り替えられます。"
        "候補数、強い順/弱い順/効率順、出せる数/全枚数/枚数指定も変更できます。\n"
        "候補ボタンを押すと、出す予定のカードが自動で並びます。"
        "合成数候補では、式に使う材料札もあわせてセットされます。"
        "ジョーカーを含む候補は X69|X=2 のような数譜方式で表示されます。"
    ),
}
rooms = {rid: Room(rid, rule, category) for rid, rule, category in ROOM_CONFIG}

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
        self.registered_composite_entries = ()

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

    def replace_registered_composites(self, values: set[int], entries=()) -> None:
        self.registered_composites = set(values)
        self.registered_composite_entries = tuple(entries)

    def can_use_registered_composite(self, n: int) -> bool:
        return n in self.registered_composites

################################################
# 勝敗判定ロジック
################################################

def check_win_condition(room):
    active_players = get_active_players(room)

    if len(active_players) == 0:
        return None

    # 現在プレイヤーの手札0枚による通常勝利
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

def record_score_play_line(room: Room, player: "Player", notation: str) -> None:
    prefix = f"{player.name}:"
    if room.score_log:
        last = room.score_log[-1]
        line = last.get("line", "")
        if line.startswith(prefix):
            tail = line[len(prefix):]
            if "D(" in tail and tail.endswith(")") and ",P(" not in tail:
                draw_prefix = tail.split("D(", 1)[0]
                suffix = notation
                if draw_prefix and suffix.startswith(draw_prefix):
                    suffix = suffix[len(draw_prefix):]
                last["line"] = line + suffix
                return
    record_score_line(room, f"{player.name}:{notation}")

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
    if (
        room.rule.prime_rule is not PrimeRule.REGISTERED
        or not room.rule.registration_enabled
    ):
        return []
    return [
        p for p in get_active_players(room)
        if not p.registered_primes and not p.registered_composites
    ]

def registered_numbers_update_payload(prime_result, composite_result) -> dict:
    return {
        "type": "registered_numbers_updated",
        "prime_values": sorted(set(prime_result.prime_values)),
        "composite_values": sorted(set(composite_result.composite_values)),
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
    player.replace_registered_composites(
        set(composite_result.composite_values),
        composite_result.entries,
    )
    return registered_numbers_update_payload(prime_result, composite_result)

def load_sample_memory() -> tuple[tuple[int, ...], tuple[int, ...], tuple, str, str]:
    if not SAMPLE_MEMORY_JSON.exists():
        return (), (), (), "", ""
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
    composite_result = parse_registered_composite_text(composite_text)
    return (
        tuple(sorted(set(values))),
        composite_result.composite_values,
        composite_result.entries,
        prime_text,
        composite_text,
    )

(
    SAMPLE_REGISTERED_PRIMES,
    SAMPLE_REGISTERED_COMPOSITES,
    SAMPLE_REGISTERED_COMPOSITE_ENTRIES,
    SAMPLE_REGISTERED_PRIME_TEXT,
    SAMPLE_REGISTERED_COMPOSITE_TEXT,
) = load_sample_memory()

def load_sample_registered_prime_payload(player: "Player") -> dict:
    player.replace_registered_primes(set(SAMPLE_REGISTERED_PRIMES))
    player.replace_registered_composites(
        set(SAMPLE_REGISTERED_COMPOSITES),
        SAMPLE_REGISTERED_COMPOSITE_ENTRIES,
    )
    return {
        "type": "registered_numbers_updated",
        "prime_values": sorted(player.registered_primes),
        "composite_values": sorted(player.registered_composites),
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

def field_allows_number(room: Room, number: int, card_count: int) -> bool:
    if room.field and card_count != len(room.field):
        return False
    return field_allows_number_value(room, number)

def field_allows_number_value(room: Room, number: int) -> bool:
    if room.field:
        field_number = room.last_number if room.last_number is not None else -1
        if not room.reverse_order and number <= field_number:
            return False
        if room.reverse_order and number >= field_number:
            return False
    return True

def find_prime_realization(
    number: int,
    source_cards: List[dict],
    required_card_count: Optional[int] = None,
) -> Optional[dict]:
    realizations = find_prime_realizations(
        number,
        source_cards,
        required_card_count,
        limit=1,
    )
    return realizations[0] if realizations else None

def assist_card_text(cards: List[dict], assigned_numbers: List[str]) -> str:
    parts = []
    suffixes = []
    joker_index = 0
    for card in cards:
        if card.get("is_joker") or card.get("suit") == "X":
            assigned = assigned_numbers[joker_index] if joker_index < len(assigned_numbers) else "?"
            parts.append("X")
            if assigned != "inf":
                suffixes.append(f"|X={score_value_symbol(assigned)}")
            joker_index += 1
        else:
            parts.append(score_value_symbol(card.get("rank")))
    return "".join(parts) + "".join(suffixes)

def assist_joker_count(cards: List[dict]) -> int:
    return sum(1 for card in cards if card.get("is_joker") or card.get("suit") == "X")

def build_assist_number_text_filter(
    source_cards: List[dict],
    required_card_count: Optional[int],
):
    rank_counts = [0] * 14
    joker_count = 0
    for card in source_cards:
        if card.get("is_joker") or card.get("suit") == "X":
            joker_count += 1
            continue
        try:
            rank = int(card.get("rank"))
        except (TypeError, ValueError):
            continue
        if 0 <= rank <= 13:
            rank_counts[rank] += 1

    rank_options = tuple(
        (str(rank), rank)
        for rank, count in enumerate(rank_counts)
        if count > 0
    )
    joker_options = tuple(str(value) for value in range(14))
    total_cards = len(source_cards)

    @lru_cache(maxsize=None)
    def can_match(text: str, index: int, counts: tuple[int, ...], jokers_left: int, used_count: int) -> bool:
        if required_card_count is not None:
            if used_count > required_card_count:
                return False
            if used_count + (len(text) - index) < required_card_count:
                return False
            if used_count + (total_cards - used_count) < required_card_count:
                return False
        if index == len(text):
            return required_card_count is None or used_count == required_card_count

        for option, rank in rank_options:
            if counts[rank] <= 0 or not text.startswith(option, index):
                continue
            next_counts = list(counts)
            next_counts[rank] -= 1
            if can_match(text, index + len(option), tuple(next_counts), jokers_left, used_count + 1):
                return True

        if jokers_left > 0:
            for option in joker_options:
                if text.startswith(option, index) and can_match(
                    text,
                    index + len(option),
                    counts,
                    jokers_left - 1,
                    used_count + 1,
                ):
                    return True

        return False

    def can_realize(number: int) -> bool:
        text = str(number)
        return can_match(text, 0, tuple(rank_counts), joker_count, 0)

    return can_realize

def find_prime_realizations(
    number: int,
    source_cards: List[dict],
    required_card_count: Optional[int] = None,
    limit: int = ASSIST_REALIZATIONS_PER_NUMBER,
) -> List[dict]:
    text = str(number)
    used: list[dict] = []
    assigned_by_card_id: dict[str, str] = {}
    results: list[dict] = []
    seen_patterns: set[tuple[str, ...]] = set()

    def card_pattern() -> tuple[str, ...]:
        pattern = []
        for card in used:
            if card.get("is_joker") or card.get("suit") == "X":
                pattern.append(f"X={assigned_by_card_id.get(card['card_id'], '?')}")
            else:
                pattern.append(str(card.get("rank")))
        return tuple(pattern)

    def collect_result() -> None:
        pattern = card_pattern()
        if pattern in seen_patterns:
            return
        seen_patterns.add(pattern)
        assigned_numbers = [
            assigned_by_card_id[card["card_id"]]
            for card in used
            if card.get("is_joker") or card.get("suit") == "X"
        ]
        cards = used[:]
        results.append({
            "number": number,
            "cards": cards,
            "assigned_numbers": assigned_numbers,
            "visible_text": assist_card_text(cards, assigned_numbers),
            "joker_count": assist_joker_count(cards),
        })

    def visit(index: int, remaining: list[dict]) -> None:
        if len(results) >= limit:
            return
        if required_card_count is not None and len(used) > required_card_count:
            return
        if index == len(text):
            if required_card_count is None or len(used) == required_card_count:
                collect_result()
            return

        candidates = sorted(
            enumerate(remaining),
            key=lambda item: 1 if item[1].get("is_joker") or item[1].get("suit") == "X" else 0,
        )
        for i, card in candidates:
            options: list[str]
            if card.get("is_joker") or card.get("suit") == "X":
                options = [str(v) for v in range(14)]
            else:
                options = [str(card.get("rank"))]

            for option in options:
                if not text.startswith(option, index):
                    continue
                used.append(card)
                if card.get("is_joker") or card.get("suit") == "X":
                    assigned_by_card_id[card["card_id"]] = option
                next_remaining = remaining[:i] + remaining[i + 1:]
                visit(index + len(option), next_remaining)
                if card.get("is_joker") or card.get("suit") == "X":
                    assigned_by_card_id.pop(card["card_id"], None)
                used.pop()
                if len(results) >= limit:
                    return

    visit(0, source_cards[:])
    results.sort(key=lambda result: (
        len(result["cards"]),
        result.get("joker_count", 0),
        result["visible_text"],
    ))
    return results

def remove_cards_by_id(cards: List[dict], used_cards: List[dict]) -> List[dict]:
    used_ids = {card["card_id"] for card in used_cards}
    return [card for card in cards if card["card_id"] not in used_ids]

def find_rank_sequence_realization(
    ranks: tuple[int, ...],
    source_cards: List[dict],
) -> Optional[dict]:
    used: list[dict] = []
    assigned_by_card_id: dict[str, str] = {}

    def visit(index: int, remaining: list[dict]) -> bool:
        if index == len(ranks):
            return True
        rank = ranks[index]

        candidates = sorted(
            enumerate(remaining),
            key=lambda item: 1 if item[1].get("is_joker") or item[1].get("suit") == "X" else 0,
        )
        for i, card in candidates:
            is_joker = card.get("is_joker") or card.get("suit") == "X"
            if not is_joker and int(card.get("rank")) != rank:
                continue
            used.append(card)
            if is_joker:
                assigned_by_card_id[card["card_id"]] = str(rank)
            next_remaining = remaining[:i] + remaining[i + 1:]
            if visit(index + 1, next_remaining):
                return True
            if is_joker:
                assigned_by_card_id.pop(card["card_id"], None)
            used.pop()
        return False

    if not visit(0, source_cards[:]):
        return None

    return {
        "cards": used[:],
        "assigned_numbers": [
            assigned_by_card_id[card["card_id"]]
            for card in used
            if card.get("is_joker") or card.get("suit") == "X"
        ],
    }

def find_composite_expression_realization(entry, source_cards: List[dict]) -> Optional[dict]:
    remaining = source_cards[:]
    material_cards: list[dict] = []
    assigned_numbers: list[str] = []
    tokens: list[dict] = []

    for token in entry.expression_tokens:
        if token.kind == "op":
            tokens.append({
                "kind": "op",
                "op": "×" if token.op == "*" else token.op,
            })
            continue

        realization = find_rank_sequence_realization(token.ranks, remaining)
        if realization is None:
            return None
        for card in realization["cards"]:
            tokens.append({"kind": "card", "card_id": card["card_id"]})
        material_cards.extend(realization["cards"])
        assigned_numbers.extend(realization["assigned_numbers"])
        remaining = remove_cards_by_id(remaining, realization["cards"])

    return {
        "tokens": tokens,
        "cards": material_cards,
        "assigned_numbers": assigned_numbers,
    }

def assist_limit_from_filters(data: dict) -> int:
    filters = data.get("filters") or {}
    if not isinstance(filters, dict):
        filters = {}
    limit_mode = filters.get("limit_mode", "ten")
    if limit_mode in ASSIST_LIMITS:
        return ASSIST_LIMITS[limit_mode]
    limit = data.get("limit", 10)
    try:
        return max(1, min(int(limit), 50))
    except (TypeError, ValueError):
        return 10

def assist_scan_limit_from_filters(data: dict) -> int:
    filters = data.get("filters") or {}
    if not isinstance(filters, dict):
        filters = {}
    limit_mode = filters.get("limit_mode", "ten")
    return ASSIST_SCAN_LIMITS.get(limit_mode, ASSIST_SCAN_LIMITS["ten"])

def assist_filter_value(data: dict, key: str, default: str) -> str:
    filters = data.get("filters") or {}
    if not isinstance(filters, dict):
        return default
    value = filters.get(key)
    return value if isinstance(value, str) else default

def assist_card_count_from_filters(data: dict) -> Optional[int]:
    value = assist_filter_value(data, "card_count", "1")
    try:
        count = int(value)
    except (TypeError, ValueError):
        return 1
    return count if 1 <= count <= 11 else 1

def assist_number_sort_key(room: Room, order: str):
    strong_first = order == "strong"
    if room.reverse_order:
        return (lambda item: item[1]) if strong_first else (lambda item: -item[1])
    return (lambda item: -item[1]) if strong_first else (lambda item: item[1])

def assist_efficiency_score(candidate: dict) -> float:
    card_count = max(1, len(candidate.get("cards") or []))
    return candidate["number"] / (10 ** (card_count - 1))

def finalize_assist_candidates(
    candidates: list[dict],
    limit: int,
    source: str,
    truncated: bool,
    scan_limit: Optional[int] = None,
    order: str = "weak",
) -> dict:
    if order == "efficient":
        candidates = sorted(
            candidates,
            key=lambda candidate: (
                -assist_efficiency_score(candidate),
                -candidate["number"],
                len(candidate.get("cards") or []),
                candidate.get("joker_count", 0),
                candidate.get("visible_text", ""),
            ),
        )
        truncated = truncated or len(candidates) > limit
        candidates = candidates[:limit]
    payload = {"candidates": candidates, "truncated": truncated, "source": source}
    if scan_limit is not None:
        payload["scan_limit"] = scan_limit
    return payload

def build_prime_assist_candidates(player: "Player", room: Room, data: dict) -> dict:
    if (
        not room.rule.registration_enabled
        or not room.rule.assist_enabled
    ):
        return {"candidates": [], "truncated": False, "source": "hand"}

    selected_ids = data.get("selected_card_ids") or []
    if not isinstance(selected_ids, list):
        selected_ids = []

    hand_by_id = {card["card_id"]: card for card in player.hand}
    selected_id_set = {cid for cid in selected_ids if cid in hand_by_id}
    target_scope = assist_filter_value(data, "target_scope", "auto")
    if target_scope == "selected":
        source_cards = [hand_by_id[cid] for cid in selected_ids if cid in hand_by_id]
        source = "selected"
    elif target_scope == "unselected":
        source_cards = [card for card in player.hand if card["card_id"] not in selected_id_set]
        source = "unselected"
    elif target_scope == "all":
        source_cards = player.hand[:]
        source = "all"
    else:
        source_cards = [hand_by_id[cid] for cid in selected_ids if cid in hand_by_id]
        source = "selected" if source_cards else "unselected"
    if not source_cards and source != "selected":
        source_cards = player.hand[:]
        source = "unselected"

    count_scope = assist_filter_value(data, "count_scope", "field")
    order = assist_filter_value(data, "order", "weak")
    defer_limit_until_sorted = order == "efficient"
    specified_card_count = assist_card_count_from_filters(data) if count_scope == "specified" else None
    required_card_count = None
    if room.field and count_scope == "field":
        required_card_count = len(room.field)
    elif count_scope == "specified":
        required_card_count = specified_card_count
    apply_field_value_filter = count_scope == "field"
    limit = assist_limit_from_filters(data)
    scan_limit = assist_scan_limit_from_filters(data)
    can_realize_number_text = build_assist_number_text_filter(source_cards, required_card_count)

    candidates = []
    scanned = 0
    registered_numbers = [("prime", number, None) for number in player.registered_primes]
    if room.rule.allow_composite:
        registered_numbers.extend(
            ("composite", entry.value, entry)
            for entry in player.registered_composite_entries
            if entry.value in player.registered_composites
        )
    registered_numbers.sort(key=assist_number_sort_key(room, order))

    for kind, number, entry in registered_numbers:
        if apply_field_value_filter and not field_allows_number_value(room, number):
            continue
        if not can_realize_number_text(number):
            continue

        scanned += 1
        if scanned > scan_limit:
            return finalize_assist_candidates(
                candidates,
                limit,
                source,
                truncated=True,
                scan_limit=scan_limit,
                order=order,
            )
        realizations = find_prime_realizations(
            number,
            source_cards,
            required_card_count,
            limit=ASSIST_REALIZATIONS_PER_NUMBER,
        )
        for realization in realizations:
            if (
                apply_field_value_filter
                and not field_allows_number(room, number, len(realization["cards"]))
            ):
                continue
            realization["kind"] = kind
            realization["field_count_match"] = (
                count_scope == "specified"
                or not room.field
                or len(realization["cards"]) == len(room.field)
            )
            if kind != "composite":
                candidates.append(realization)
                if not defer_limit_until_sorted and len(candidates) >= limit:
                    return finalize_assist_candidates(
                        candidates,
                        limit,
                        source,
                        truncated=True,
                        order=order,
                    )
                continue

            material_pool = source_cards if source in ("selected", "unselected", "all") else player.hand
            material_source = remove_cards_by_id(material_pool, realization["cards"])
            expression = find_composite_expression_realization(entry, material_source)
            if expression is None:
                continue
            realization["expression"] = entry.expression
            realization["composite"] = expression
            realization["material_text"] = assist_card_text(
                expression["cards"],
                expression["assigned_numbers"],
            )
            candidates.append(realization)
            if not defer_limit_until_sorted and len(candidates) >= limit:
                return finalize_assist_candidates(
                    candidates,
                    limit,
                    source,
                    truncated=True,
                    order=order,
                )

    return finalize_assist_candidates(
        candidates,
        limit,
        source,
        truncated=False,
        order=order,
    )
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
# CPU処理
################################################

def current_turn_player(room: Room):
    return next((p for p in room.players if p.id == room.current_turn_id), None)


async def add_cpu_to_room(room: Room, name: str = "CPU") -> CpuPlayer:
    cpu_count = sum(1 for p in room.players if is_cpu_player(p))
    cpu = CpuPlayer(name=f"{name}{cpu_count + 1}" if cpu_count else name)
    cpu.room = room
    cpu.status = "waiting"
    if room.rule.registration_enabled and (SAMPLE_REGISTERED_PRIMES or SAMPLE_REGISTERED_COMPOSITES):
        load_sample_registered_prime_payload(cpu)
    room.players.append(cpu)
    await room.log_chat(f"{cpu.name}が入室しました")
    await room.update_room_status()
    return cpu


async def remove_cpu_from_room(room: Room) -> bool:
    cpu = next((p for p in room.players if is_cpu_player(p)), None)
    if cpu is None:
        return False
    room.players.remove(cpu)
    cpu.room = None
    cpu.status = "watching"
    cpu.clear_hand()
    await room.log_chat(f"{cpu.name}が退室しました")
    await room.update_room_status()
    return True


async def maybe_schedule_cpu_turn(room: Room) -> None:
    if room.state != "playing":
        return
    current = current_turn_player(room)
    if not is_cpu_player(current):
        return
    if getattr(room, "cpu_turn_running", False):
        return
    room.cpu_turn_running = True
    asyncio.create_task(run_cpu_turn(room, current))


async def run_cpu_turn(room: Room, cpu: CpuPlayer) -> None:
    try:
        await asyncio.sleep(0.8)
        if room.state != "playing" or room.current_turn_id != cpu.id or cpu not in room.players:
            return

        action = choose_cpu_action(cpu, room, validator=is_valid_prime_for_player)
        if action.kind == "play_prime":
            await handle_prime_play(cpu, room, action.payload)
        elif action.kind == "draw":
            await draw_card_for_player(cpu, room)
            followup = choose_cpu_action(cpu, room, validator=is_valid_prime_for_player)
            if followup.kind == "play_prime" and room.current_turn_id == cpu.id:
                await asyncio.sleep(0.4)
                await handle_prime_play(cpu, room, followup.payload)
            elif room.current_turn_id == cpu.id:
                await asyncio.sleep(0.4)
                await pass_turn_for_player(cpu, room)
        else:
            await pass_turn_for_player(cpu, room)
    finally:
        room.cpu_turn_running = False
        if room.state == "playing" and room.current_turn_id == cpu.id:
            await maybe_schedule_cpu_turn(room)


async def draw_card_for_player(player, room: Room) -> bool:
    if room.has_drawn:
        await player.send_json({"type": "error", "message": "このターンはすでにドロー済みです。"})
        return False
    if not room.deck:
        return False

    drawn = room.deck.pop(0)
    player.add_card(drawn)
    record_score_line(room, f"{player.name}:{score_state_prefix(room)}D({score_card_symbol(drawn)})")
    await player.send_hand_update()
    await room.update_game_state()
    room.has_drawn = True
    return True


async def pass_turn_for_player(player, room: Room) -> None:
    await player.send_hand_update()
    flow_field(room)
    await room.update_game_state()
    await room.broadcast({
        "type": "action_result",
        "action": "pass",
        "player_id": player.id
    })
    await room.log_chat(f"{player.name}がパスしました")
    record_score_play_line(room, player, f"{score_state_prefix(room)}%")
    await next_turn(room)

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
                room_categories = {rid: room.category for rid, room in rooms.items()}
                allow_composite = {rid: room.rule.allow_composite for rid, room in rooms.items()}
                prime_rules = {rid: room.rule.prime_rule.name.lower() for rid, room in rooms.items()}
                assist_enabled = {rid: room.rule.assist_enabled for rid, room in rooms.items()}
                registration_enabled = {rid: room.rule.registration_enabled for rid, room in rooms.items()}
                room_descriptions = {rid: ROOM_DESCRIPTIONS.get(rid, "") for rid in rooms}
                await websocket.send_json({
                    "type": "room_counts",
                    "counts": counts,
                    "rules": rules,
                    "room_categories": room_categories,
                    "allow_composite": allow_composite,
                    "prime_rules": prime_rules,
                    "assist_enabled": assist_enabled,
                    "registration_enabled": registration_enabled,
                    "room_descriptions": room_descriptions,
                })

            elif msg_type == "join_room":
                rid = data["room_id"]
                room = rooms.get(rid)
                if room is None:
                    await websocket.send_json({"type": "error", "message": "room not found"})
                    continue

                if player.room is room:
                    await room.update_room_status()
                    await player.send_json({
                        "type": "room_state_initialization",
                        "room_state": room.state,
                        "category": room.category,
                        "allow_composite": room.rule.allow_composite,
                        "prime_rule": room.rule.prime_rule.name.lower(),
                        "assist_enabled": room.rule.assist_enabled,
                        "registration_enabled": room.rule.registration_enabled,
                        "description": ROOM_DESCRIPTIONS.get(room.room_id, ""),
                    })
                    continue

                if player.room:
                    await leave_room(player)

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
                await player.send_json({
                    "type": "room_state_initialization",
                    "room_state": room.state,
                    "category": room.category,
                    "allow_composite": room.rule.allow_composite,
                    "prime_rule": room.rule.prime_rule.name.lower(),
                    "assist_enabled": room.rule.assist_enabled,
                    "registration_enabled": room.rule.registration_enabled,
                    "description": ROOM_DESCRIPTIONS.get(room.room_id, ""),
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

            elif msg_type == "add_cpu":
                if not player.room:
                    continue
                room = player.room
                if room.state == "playing":
                    await player.send_json({"type": "error", "message": "対戦中はCPUを追加できません。"})
                    continue
                if any(is_cpu_player(p) for p in room.players):
                    await player.send_json({"type": "error", "message": "この部屋にはすでにCPUがいます。"})
                    continue
                if len(room.players) >= 10:
                    await player.send_json({"type": "error", "message": "部屋が満員です。"})
                    continue
                await add_cpu_to_room(room)

            elif msg_type == "remove_cpu":
                if not player.room:
                    continue
                room = player.room
                if room.state == "playing":
                    await player.send_json({"type": "error", "message": "対戦中はCPUを退出させられません。"})
                    continue
                if not await remove_cpu_from_room(room):
                    await player.send_json({"type": "error", "message": "この部屋にCPUはいません。"})
                    continue

            elif msg_type == "start_game":
                if not player.room:
                    continue
                room = player.room

                # 対戦待ちプレイヤー確認
                waiting_players = get_active_players(room)
                if len(waiting_players) not in (1, 2):
                    await websocket.send_json({"type": "error", "message": "対戦待ちは1人または2人必要です。"})
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
                await maybe_schedule_cpu_turn(room)

            elif msg_type == "get_prime_assist":
                if not player.room:
                    continue
                room = player.room
                await player.send_json({
                    "type": "prime_assist_result",
                    **build_prime_assist_candidates(player, room, data),
                })
                continue

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

                await draw_card_for_player(player, room)

            elif msg_type == "pass":
                if not player.room:
                    continue
                room = player.room
                if player.id != room.current_turn_id:
                    await websocket.send_json({"type": "error", "message": "あなたのターンではありません。"})
                    continue

                await pass_turn_for_player(player, room)

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
    if not played_cards:
        await player.ws.send_json({"type": "error", "message": "出すカードを選んでください。"})
        return
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
        if room.field and len(room.field) != 1:
            await player.ws.send_json({"type": "error", "message": "ジョーカー1枚出しは、場が空か1枚のときだけ出せます。"})
            return
        push_to_reserve(room, played_cards)
        # ジョーカー1枚だけ → 場を流す
        player.remove_card(jokers[0])
        # 場を流して予備軍を山へ戻す
        flow_field(room)
        room.has_drawn = False
        await player.send_hand_update()
        await room.log_chat(f"{player.name}がジョーカーを出しました、インフィニティ！")
        record_score_play_line(room, player, f"{score_prefix}X[IN]{score_win_suffix(player)}")
        await room.update_game_state()
        if await room.try_end_game():
            await room.update_room_status()
        else:
            await broadcast_turn_update(room, player.name)
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
        record_score_play_line(room, player, f"{score_prefix}{play_text}[GC]{score_win_suffix(player)}")
        await room.update_game_state()
        if await room.try_end_game():
            await room.update_room_status()
            return
        await broadcast_turn_update(room, player.name)
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
        record_score_play_line(room, player, f"{score_prefix}{play_text}[RR]{score_win_suffix(player)}")

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
        record_score_play_line(
            room,
            player,
            f"{score_prefix}{play_text},P({score_cards_text(drawn_penalties, sort_cards=True)})"
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
    record_score_play_line(room, player, f"{score_prefix}{play_text}{score_win_suffix(player)}")
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
    if not sel_cards:
        await player.ws.send_json({"type": "error", "message": "見せ札を選んでください。"})
        return
    if not comp_tokens:
        await player.ws.send_json({"type": "error", "message": "材料札で合成数の式を作ってください。"})
        return

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
    composite_chat_text = (
        f"{score_cards_text(sel_cards)}={score_tokens_text(comp_tokens, score_cards_by_id).replace('*', '×')}"
        f"{score_joker_suffix(con_cards, comp_assigned)}"
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
        await room.log_chat(f"{player.name}の合成数 {composite_chat_text} は不正でした（{e.msg}）。ペナルティ。")
        record_score_play_line(
            room,
            player,
            f"{score_prefix}{score_composite_text},P({score_cards_text(drawn_penalties, sort_cards=True)})"
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
    await room.log_chat(f"{player.name}が{composite_chat_text}を出しました")
    record_score_play_line(room, player, f"{score_prefix}{score_composite_text}{score_win_suffix(player)}")
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
            record_score_play_line(room, player, "退出")
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

    # 1) 待機中のプレイヤーを確定（1人練習または2人対戦）
    waiting_players = get_active_players(room)
    if len(waiting_players) not in (1, 2):
        return
    for p in room.players:
        if p not in waiting_players:
            p.clear_hand()
            await p.send_hand_update()

    # 2) デッキ生成→配布（プリセット準拠）
    deck = build_deck(room.rule)
    hands, remaining = shuffle_and_deal(deck, room.rule.hand_size, num_players=len(waiting_players))
    for player, hand in zip(waiting_players, hands):
        player.hand = hand
    room.deck = remaining

    room.reserve = []
    room.field = []  # 場のカードは空
    room.last_number = None
    room.score_log = []
    for player in waiting_players:
        player.sort_hand()
        record_score_line(room, f"{player.name}:({score_cards_text(player.hand, sort_cards=True)})")
    room.state = "playing"

    # ランダムに先攻プレイヤー決定
    room.current_turn_id = random.choice([p.id for p in waiting_players])

    # プレイヤーそれぞれに手札情報を送信
    for player in waiting_players:
        await player.send_json({"type": "deal", "your_hand": player.hand})

    # 全体にゲーム開始 & 現在のターン情報
    await room.broadcast({
        "type": "game_start",
        "category": room.category,
        "allow_composite": room.rule.allow_composite,
        "prime_rule": room.rule.prime_rule.name.lower(),
        "assist_enabled": room.rule.assist_enabled,
        "registration_enabled": room.rule.registration_enabled,
    })
    await room.update_game_state()
    # チャットにログを流す
    await room.log_chat("ゲーム開始！")
    await maybe_schedule_cpu_turn(room)


################################################
# 次のターンに移る
################################################
async def broadcast_turn_update(room, current_turn_name: str | None, reset_timer: bool = True) -> None:
    await room.broadcast({
        "type": "turn_update",
        "current_turn": current_turn_name,
        "reset_timer": reset_timer,
    })

async def next_turn(room):
    # ターンが変わるので、ドロー済みフラグをリセットする
    room.has_drawn = False

    # 対戦に参加している（statusが"waiting"の）プレイヤーだけを対象とする
    active_players = get_active_players(room)
    if len(active_players) < 1:
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
    await broadcast_turn_update(room, next_player.name if next_player else None)
    await maybe_schedule_cpu_turn(room)
