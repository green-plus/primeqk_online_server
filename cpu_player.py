from __future__ import annotations

from dataclasses import dataclass, field
from itertools import permutations
import secrets
from typing import Callable, Iterable, List, Optional

from rules import PrimeRule


Card = dict
NumberValidator = Callable[[int, "CpuPlayer", object], bool]
CpuActionSelector = Callable[["CpuPlayer", object, Optional[NumberValidator]], "CpuAction"]


@dataclass(frozen=True)
class CpuAction:
    kind: str
    payload: dict = field(default_factory=dict)


@dataclass(frozen=True)
class CpuKnowledgeSpec:
    source: str = "none"  # "none" | "sample" | "inline"
    load_timing: str = "never"  # "never" | "registration" | "always"
    prime_text: str = ""
    composite_text: str = ""


@dataclass(frozen=True)
class CpuProfile:
    key: str
    label: str
    description: str
    rule_keys: tuple[str, ...] = ()
    prime_rules: tuple[PrimeRule, ...] = ()
    knowledge: CpuKnowledgeSpec = field(default_factory=CpuKnowledgeSpec)
    action_selector: Optional[CpuActionSelector] = None

    def supports_rule(self, rule) -> bool:
        if self.rule_keys and getattr(rule, "key", None) not in self.rule_keys:
            return False
        if self.prime_rules and getattr(rule, "prime_rule", None) not in self.prime_rules:
            return False
        return True

    def to_payload(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "description": self.description,
        }


class CpuPlayer:
    def __init__(self, name: str = "CPU", player_id: Optional[str] = None, cpu_key: str = "basic"):
        self.id = player_id or f"cpu-{secrets.token_hex(8)}"
        self.name = name
        self.ws = self
        self.room = None
        self.status = "watching"
        self.hand: List[Card] = []
        self.is_cpu = True
        self.cpu_key = cpu_key
        self.registered_primes: set[int] = set()
        self.registered_composites: set[int] = set()
        self.registered_composite_entries = ()

    async def send_json(self, message: dict):
        return None

    async def send_hand_update(self):
        return None

    def sort_hand(self):
        self.hand.sort(key=lambda card: card.get("rank", 0))

    def add_card(self, card: Card):
        self.hand.append(card)
        self.sort_hand()

    def remove_card(self, card: Card) -> bool:
        if card in self.hand:
            self.hand.remove(card)
            return True
        return False

    def has_cards(self, cards: List[Card]) -> bool:
        temp = self.hand[:]
        for card in cards:
            if card in temp:
                temp.remove(card)
            else:
                return False
        return True

    def remove_cards(self, cards: List[Card]) -> bool:
        if not self.has_cards(cards):
            return False
        for card in cards:
            self.remove_card(card)
        return True

    def clear_hand(self):
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


def is_cpu_player(player) -> bool:
    return bool(getattr(player, "is_cpu", False))


def get_cpu_profile(cpu_key: str) -> Optional[CpuProfile]:
    return CPU_PROFILES.get(cpu_key)


def available_cpu_profiles_for_rule(rule) -> List[CpuProfile]:
    return [profile for profile in CPU_PROFILES.values() if profile.supports_rule(rule)]


def available_cpu_profile_payloads(rule) -> List[dict]:
    return [profile.to_payload() for profile in available_cpu_profiles_for_rule(rule)]


def choose_profile_cpu_action(
    cpu: CpuPlayer,
    room,
    validator: Optional[NumberValidator] = None,
) -> CpuAction:
    profile = get_cpu_profile(getattr(cpu, "cpu_key", "basic"))
    if profile and profile.action_selector:
        return profile.action_selector(cpu, room, validator)
    return choose_cpu_action(cpu, room, validator=validator)


def choose_cpu_action(
    cpu: CpuPlayer,
    room,
    validator: Optional[NumberValidator] = None,
    max_cards: int = 3,
) -> CpuAction:
    candidate = choose_prime_play(cpu, room, validator=validator, max_cards=max_cards)
    if candidate is not None:
        return CpuAction("play_prime", candidate)

    if not getattr(room, "has_drawn", False) and getattr(room, "deck", []):
        return CpuAction("draw")

    return CpuAction("pass")


def choose_prime_play(
    cpu: CpuPlayer,
    room,
    validator: Optional[NumberValidator] = None,
    max_cards: int = 3,
) -> Optional[dict]:
    validator = validator or default_number_validator
    best = None
    for cards in prime_play_candidates(cpu.hand, room, max_cards=max_cards):
        number = cards_number(cards)
        if number is None:
            continue
        if not beats_field(number, len(cards), room):
            continue
        if not validator(number, cpu, getattr(room, "rule", None)):
            continue
        payload = {"cards": cards, "assigned_numbers": [], "number": number}
        if best is None or cpu_candidate_sort_key(payload, room) < cpu_candidate_sort_key(best, room):
            best = payload

    if best is not None:
        return {
            "cards": best["cards"],
            "assigned_numbers": best["assigned_numbers"],
        }

    joker = single_joker(cpu.hand)
    field_count = len(getattr(room, "field", []) or [])
    if joker is not None and field_count <= 1:
        return {"cards": [joker], "assigned_numbers": []}

    return None


def prime_play_candidates(hand: List[Card], room, max_cards: int = 3) -> Iterable[List[Card]]:
    non_jokers = [card for card in hand if not is_joker(card)]
    required_count = len(getattr(room, "field", []) or [])
    if required_count:
        counts = [required_count]
    else:
        counts = range(1, min(max_cards, len(non_jokers)) + 1)

    for count in counts:
        if count < 1 or count > max_cards or count > len(non_jokers):
            continue
        seen_numbers = set()
        for cards_tuple in permutations(non_jokers, count):
            cards = list(cards_tuple)
            number = cards_number(cards)
            if number is None or number in seen_numbers:
                continue
            seen_numbers.add(number)
            yield cards


def cards_number(cards: List[Card]) -> Optional[int]:
    if not cards or any(is_joker(card) for card in cards):
        return None
    text = "".join(str(card.get("rank")) for card in cards)
    if text.startswith("0"):
        return None
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def beats_field(number: int, card_count: int, room) -> bool:
    field = getattr(room, "field", []) or []
    if not field:
        return True
    if card_count != len(field):
        return False

    field_number = getattr(room, "last_number", None)
    if field_number is None:
        return True

    if getattr(room, "reverse_order", False):
        return number < field_number
    return number > field_number


def default_number_validator(number: int, cpu: CpuPlayer, rule) -> bool:
    prime_rule = getattr(rule, "prime_rule", PrimeRule.NORMAL)
    if prime_rule is PrimeRule.REGISTERED:
        return cpu.can_use_registered_prime(number)
    if prime_rule is PrimeRule.TETRAD:
        return is_twin_quadruplet_prime(number)
    if prime_rule is PrimeRule.SEMIPRIME:
        return is_semiprime(number)
    return is_prime(number)


def cpu_candidate_sort_key(payload: dict, room) -> tuple:
    number = payload["number"]
    if getattr(room, "reverse_order", False):
        return (len(payload["cards"]), -number)
    return (len(payload["cards"]), number)


def is_joker(card: Card) -> bool:
    return bool(card.get("is_joker")) or card.get("suit") == "X"


def single_joker(hand: List[Card]) -> Optional[Card]:
    for card in hand:
        if is_joker(card):
            return card
    return None


def is_prime(n: int) -> bool:
    if n < 2:
        return False
    if n == 2:
        return True
    if n % 2 == 0:
        return False
    d = 3
    while d * d <= n:
        if n % d == 0:
            return False
        d += 2
    return True


def is_twin_quadruplet_prime(n: int) -> bool:
    if n in {5, 7, 11, 13}:
        return True
    if not is_prime(n):
        return False
    for start in (n, n - 2, n - 6, n - 8):
        if start >= 2 and n in {start, start + 2, start + 6, start + 8}:
            if all(is_prime(value) for value in (start, start + 2, start + 6, start + 8)):
                return True
    return False


def is_semiprime(n: int) -> bool:
    if n < 4 or is_prime(n):
        return False
    for divisor in range(2, int(n**0.5) + 1):
        if n % divisor == 0:
            return is_prime(divisor) and is_prime(n // divisor)
    return False


CPU_PROFILES = {
    "basic": CpuProfile(
        key="basic",
        label="汎用テストCPU",
        description="弱めですが、通常・四つ子・半素数・登録制限の各ルールで最低限の動作確認に使えるCPUです。",
        knowledge=CpuKnowledgeSpec(source="sample", load_timing="registration"),
    ),
}
