from __future__ import annotations

from dataclasses import dataclass, field
from itertools import permutations
import json
from pathlib import Path
import secrets
from typing import Callable, Iterable, List, Optional

from registered_primes import registered_value_encodings
from rules import PrimeRule


Card = dict
NumberValidator = Callable[[int, "CpuPlayer", object], bool]
CpuActionSelector = Callable[["CpuPlayer", object, Optional[NumberValidator]], "CpuAction"]
GOLD_PLAN_MAX_LAST_CANDIDATES = 30
GOLD_PLAN_MAX_BRANCH_CANDIDATES = 24
GOLD_PLAN_MAX_RESULTS_PER_COUNT = 3
GOLD_PLAN_MAX_ALTERNATIVES = 8
GOLD_PLAN_EVALUATION_JSON = Path(__file__).resolve().parent / "gold_plan_evaluation.json"


@dataclass(frozen=True)
class CpuAction:
    kind: str
    payload: dict = field(default_factory=dict)


@dataclass(frozen=True)
class CpuKnowledgeSpec:
    source: str = "none"  # "none" | "sample" | "gold" | "inline"
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
        self.gold_active_plan: Optional[dict] = None
        self.gold_plan_step_index = 0

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


def choose_gold_planning_cpu_action(
    cpu: CpuPlayer,
    room,
    validator: Optional[NumberValidator] = None,
) -> CpuAction:
    validator = gold_knowledge_number_validator
    field = getattr(room, "field", []) or []

    if field:
        action = choose_gold_response_action(cpu, room, validator)
    else:
        action = choose_gold_lead_action(cpu, room, validator)
    if action is not None:
        return action

    cut = choose_57_cut(cpu.hand, room)
    if cut is not None:
        return CpuAction("play_prime", cut)

    if not getattr(room, "has_drawn", False) and getattr(room, "deck", []):
        return CpuAction("draw")

    joker = single_joker(cpu.hand)
    field_count = len(getattr(room, "field", []) or [])
    if joker is not None and field_count <= 1:
        return CpuAction("play_prime", {"cards": [joker], "assigned_numbers": []})

    return CpuAction("pass")


def choose_gold_lead_action(
    cpu: CpuPlayer,
    room,
    validator: NumberValidator,
) -> Optional[CpuAction]:
    action = play_next_gold_plan_step(cpu, room, validator)
    if action is not None:
        return action

    plan = build_gold_plan(cpu, room_without_field(room), max_steps=20, validator=validator)
    if is_executable_gold_plan(plan, cpu):
        set_gold_active_plan(cpu, plan)
        return play_next_gold_plan_step(cpu, room, validator)

    return choose_gold_all_out_or_draw(cpu, room)


def choose_gold_response_action(
    cpu: CpuPlayer,
    room,
    validator: NumberValidator,
) -> Optional[CpuAction]:
    field_count = len(getattr(room, "field", []) or [])
    if not active_gold_plan_matches_field(cpu, field_count):
        clear_gold_active_plan(cpu)
        return choose_gold_plan_for_field_action(cpu, room, validator)

    action = play_next_gold_plan_step(cpu, room, validator)
    if action is not None:
        return action

    later = playable_later_gold_plan_steps(cpu, room)
    if later:
        step_index, candidate = later[0]
        trump_index = gold_plan_trump_step_index(cpu.gold_active_plan)
        if step_index == trump_index:
            return choose_gold_trump_or_saved_pass(cpu, room, candidate, validator)
        return play_gold_deviation_with_replan(cpu, room, candidate, validator)

    return choose_gold_correction_action(cpu, room, validator)


def choose_gold_plan_for_field_action(
    cpu: CpuPlayer,
    room,
    validator: NumberValidator,
) -> Optional[CpuAction]:
    field_count = len(getattr(room, "field", []) or [])
    if not field_count:
        return choose_gold_lead_action(cpu, room, validator)

    plan = build_same_count_gold_plan(cpu, room, field_count, max_steps=20, validator=validator)
    if is_executable_gold_plan(plan, cpu):
        set_gold_active_plan(cpu, plan)
        return play_next_gold_plan_step(cpu, room, validator)

    return choose_gold_correction_action(cpu, room, validator)


def choose_gold_correction_action(
    cpu: CpuPlayer,
    room,
    validator: NumberValidator,
) -> Optional[CpuAction]:
    field_count = len(getattr(room, "field", []) or [])
    if not field_count:
        return choose_gold_lead_action(cpu, room, validator)

    candidates = dedupe_candidates(gold_plan_candidates(cpu, room, [field_count], validator))
    candidates = [candidate for candidate in candidates if candidate_is_playable(candidate, cpu, room)]
    if not candidates:
        clear_gold_active_plan(cpu)
        return None

    rng = secrets.SystemRandom()
    sampled = rng.sample(candidates, min(3, len(candidates)))
    best = None
    for candidate in sampled:
        remaining = remaining_cards(cpu.hand, candidate_consumed_cards(candidate))
        temp_cpu = temporary_cpu_with_hand(cpu, remaining)
        plan = build_gold_plan(temp_cpu, room_without_field(room), max_steps=20, validator=validator)
        if not is_executable_gold_plan(plan, temp_cpu):
            continue
        key = gold_plan_score(plan)
        if best is None or key > best[0]:
            best = (key, candidate, plan)

    if best is None:
        clear_gold_active_plan(cpu)
        return None

    set_gold_active_plan(cpu, best[2])
    return candidate_to_action(best[1])


def play_next_gold_plan_step(
    cpu: CpuPlayer,
    room,
    validator: NumberValidator,
) -> Optional[CpuAction]:
    plan = getattr(cpu, "gold_active_plan", None)
    if not plan:
        return None

    steps = plan.get("steps", [])
    index = getattr(cpu, "gold_plan_step_index", 0)
    while index < len(steps):
        candidate = steps[index]
        if not candidate_cards_available(candidate, cpu):
            clear_gold_active_plan(cpu)
            return None
        if candidate_is_playable(candidate, cpu, room):
            cpu.gold_plan_step_index = index + 1
            return candidate_to_action(candidate)
        break

    if index >= len(steps):
        clear_gold_active_plan(cpu)
    return None


def playable_later_gold_plan_steps(cpu: CpuPlayer, room) -> list[tuple[int, dict]]:
    plan = getattr(cpu, "gold_active_plan", None)
    if not plan:
        return []
    steps = plan.get("steps", [])
    start = getattr(cpu, "gold_plan_step_index", 0) + 1
    later = [
        (index, step)
        for index, step in enumerate(steps[start:], start=start)
        if candidate_cards_available(step, cpu)
        and candidate_is_playable(step, cpu, room)
        and len(step.get("cards", [])) == len(getattr(room, "field", []) or [])
    ]
    return sorted(later, key=lambda item: gold_plan_candidate_score(item[1], room))


def choose_gold_trump_or_saved_pass(
    cpu: CpuPlayer,
    room,
    trump: dict,
    validator: NumberValidator,
) -> CpuAction:
    remaining = remaining_cards(cpu.hand, candidate_consumed_cards(trump))
    temp_cpu = temporary_cpu_with_hand(cpu, remaining)
    tail = choose_gold_finish_tail(temp_cpu, room_without_field(room), validator)
    if tail:
        plan = finalize_gold_plan(temp_cpu, room_without_field(room), tail, len(trump.get("cards", [])))
        set_gold_active_plan(cpu, plan)
        return candidate_to_action(trump)
    return CpuAction("pass")


def play_gold_deviation_with_replan(
    cpu: CpuPlayer,
    room,
    candidate: dict,
    validator: NumberValidator,
) -> CpuAction:
    remaining = remaining_cards(cpu.hand, candidate_consumed_cards(candidate))
    temp_cpu = temporary_cpu_with_hand(cpu, remaining)
    plan = build_gold_plan(temp_cpu, room_without_field(room), max_steps=20, validator=validator)
    if is_executable_gold_plan(plan, temp_cpu):
        set_gold_active_plan(cpu, plan)
    else:
        clear_gold_active_plan(cpu)
    return candidate_to_action(candidate)


def set_gold_active_plan(cpu: CpuPlayer, plan: dict) -> None:
    cpu.gold_active_plan = plan
    cpu.gold_plan_step_index = 0


def clear_gold_active_plan(cpu: CpuPlayer) -> None:
    cpu.gold_active_plan = None
    cpu.gold_plan_step_index = 0


def is_executable_gold_plan(plan: dict, cpu: CpuPlayer) -> bool:
    return bool(plan.get("steps")) and bool(plan.get("completed")) and all(
        candidate_cards_available(step, cpu)
        for step in plan.get("steps", [])
    )


def active_gold_plan_matches_field(cpu: CpuPlayer, field_count: int) -> bool:
    plan = getattr(cpu, "gold_active_plan", None)
    if not plan or not field_count:
        return bool(plan)
    steps = plan.get("steps", [])
    index = getattr(cpu, "gold_plan_step_index", 0)
    if index >= len(steps):
        return False
    return len(steps[index].get("cards", [])) == field_count


def candidate_cards_available(candidate: dict, cpu: CpuPlayer) -> bool:
    return cpu.has_cards(candidate_consumed_cards(candidate))


def candidate_is_playable(candidate: dict, cpu: CpuPlayer, room) -> bool:
    if not candidate_cards_available(candidate, cpu):
        return False
    number = candidate.get("number")
    if number == "X":
        return len(getattr(room, "field", []) or []) <= 1
    try:
        value = int(number)
    except (TypeError, ValueError):
        return False
    return beats_field(value, len(candidate.get("cards", [])), room)


def gold_plan_trump_step_index(plan: Optional[dict]) -> Optional[int]:
    if not plan:
        return None
    return max(
        (
            index
            for index, step in enumerate(plan.get("steps", []))
            if str(step.get("role", "")).startswith("rally-")
        ),
        default=None,
    )


def choose_gold_all_out_or_draw(cpu: CpuPlayer, room) -> Optional[CpuAction]:
    if not getattr(room, "has_drawn", False) and getattr(room, "deck", []):
        clear_gold_active_plan(cpu)
        return CpuAction("draw")
    forced = build_gold_all_out_payload(cpu.hand, force_random=True)
    if forced is None:
        return None
    clear_gold_active_plan(cpu)
    return CpuAction("play_prime", forced)


def build_gold_all_out_payload(hand: List[Card], force_random: bool) -> Optional[dict]:
    if not hand:
        return None

    rng = secrets.SystemRandom()
    cards = hand[:]
    joker = single_joker(cards)
    assigned_by_id = {}

    if joker is not None:
        choices = [1, 3, 7, 9]
        valid = [
            value for value in choices
            if hand_rank_sum(cards, joker_value=value) % 3 != 0
        ]
        if valid:
            assigned_by_id[joker.get("card_id")] = str(rng.choice(valid))
        elif not force_random:
            return None
        else:
            assigned_by_id[joker.get("card_id")] = str(rng.choice(choices))
    elif hand_rank_sum(cards, joker_value=None) % 3 == 0 and not force_random:
        return None

    bottom = choose_gold_all_out_bottom_card(cards, assigned_by_id, rng)
    if bottom is None and not force_random:
        return None

    remaining = cards[:]
    if bottom is not None:
        remaining.remove(bottom)
    rng.shuffle(remaining)
    ordered = remaining + ([bottom] if bottom is not None else [])
    return {
        "cards": ordered,
        "assigned_numbers": [
            assigned_by_id[card.get("card_id")]
            for card in ordered
            if card.get("card_id") in assigned_by_id
        ],
    }


def hand_rank_sum(hand: List[Card], joker_value: Optional[int]) -> int:
    total = 0
    for card in hand:
        if is_joker(card):
            total += joker_value or 0
        else:
            total += int(card.get("rank", 0))
    return total


def choose_gold_all_out_bottom_card(cards: List[Card], assigned_by_id: dict, rng) -> Optional[Card]:
    odd_ranks = {1, 3, 7, 9, 11, 13}
    candidates = [
        card for card in cards
        if (
            int(assigned_by_id.get(card.get("card_id"), card.get("rank", 0))) in odd_ranks
            if is_joker(card)
            else int(card.get("rank", 0)) in odd_ranks
        )
    ]
    if not candidates:
        return None
    return rng.choice(candidates)


def choose_gold_play(
    cpu: CpuPlayer,
    room,
    validator: Optional[NumberValidator] = None,
) -> Optional[dict]:
    validator = validator or gold_knowledge_number_validator
    if not (getattr(room, "field", []) or []):
        plan = build_gold_plan(cpu, room, max_steps=20, validator=validator)
        if plan["steps"]:
            return plan["steps"][0]

    field_count = len(getattr(room, "field", []) or [])
    max_cards = min(9, len([card for card in cpu.hand if not is_joker(card)]))
    counts = [field_count] if field_count else list(range(1, max_cards + 1))
    candidates = knowledge_prime_candidates(cpu, room, validator, counts)
    candidates.extend(knowledge_composite_candidates(cpu, room, counts))
    if not candidates:
        return None

    trumps = strongest_trumps_by_count(cpu, room, validator)
    best = max(
        candidates,
        key=lambda candidate: gold_candidate_score(cpu, room, candidate, trumps, validator),
    )
    return best


def choose_gold_prime_play(
    cpu: CpuPlayer,
    room,
    validator: Optional[NumberValidator] = None,
) -> Optional[dict]:
    candidate = choose_gold_play(cpu, room, validator=validator)
    if candidate is None or candidate.get("kind") != "prime":
        return None
    return {
        "cards": candidate["cards"],
        "assigned_numbers": candidate["assigned_numbers"],
    }


def gold_knowledge_number_validator(number: int, cpu: CpuPlayer, rule) -> bool:
    prime_rule = getattr(rule, "prime_rule", PrimeRule.NORMAL)
    if prime_rule in (PrimeRule.NORMAL, PrimeRule.REGISTERED):
        return cpu.can_use_registered_prime(number)
    return default_number_validator(number, cpu, rule)


def build_gold_plan(
    cpu: CpuPlayer,
    room,
    max_steps: int = 20,
    validator: Optional[NumberValidator] = None,
) -> dict:
    validator = validator or gold_knowledge_number_validator
    plans = build_gold_plans(cpu, room, max_steps=max_steps, validator=validator)
    if plans:
        best = plans[0]
        best["alternatives"] = plans[1:GOLD_PLAN_MAX_ALTERNATIVES]
        return best

    fallback = build_same_count_gold_plan(cpu, room, 1, max_steps, validator)
    fallback["alternatives"] = []
    return fallback


def build_gold_plans(
    cpu: CpuPlayer,
    room,
    max_steps: int = 20,
    validator: Optional[NumberValidator] = None,
) -> list[dict]:
    validator = validator or gold_knowledge_number_validator
    non_joker_count = len([card for card in cpu.hand if not is_joker(card)])
    rally_counts = range(1, min(9, non_joker_count) + 1)
    plans = [
        plan
        for rally_count in rally_counts
        for plan in search_same_count_gold_plans(cpu, room, rally_count, max_steps, validator)
    ]
    plans.sort(key=gold_plan_score, reverse=True)
    return plans[:GOLD_PLAN_MAX_ALTERNATIVES]


def build_same_count_gold_plan(
    cpu: CpuPlayer,
    room,
    rally_count: int,
    max_steps: int,
    validator: NumberValidator,
) -> dict:
    searched = search_same_count_gold_plans(cpu, room, rally_count, max_steps, validator)
    if searched:
        return searched[0]

    temp_cpu = temporary_cpu_with_hand(cpu, cpu.hand[:])
    steps = []
    for _ in range(max_steps):
        candidate = choose_gold_rally_candidate(temp_cpu, room, rally_count, validator)
        if candidate is None:
            break
        append_gold_plan_step(steps, temp_cpu, candidate, role=f"rally-{rally_count}")
        temp_cpu.hand = remaining_cards(temp_cpu.hand, candidate_consumed_cards(candidate))

    plan = {
        "steps": steps,
        "remaining": temp_cpu.hand,
        "completed": not temp_cpu.hand,
        "rally_count": rally_count,
        "last_rally_strength": gold_plan_last_rally_strength(steps, room),
    }
    plan["evaluation"] = evaluate_gold_plan(plan)
    return plan


def search_same_count_gold_plans(
    cpu: CpuPlayer,
    room,
    rally_count: int,
    max_steps: int,
    validator: NumberValidator,
) -> list[dict]:
    direct_tail = choose_gold_finish_tail(cpu, room, validator)
    if direct_tail:
        return [
            finalize_gold_plan(cpu, room, direct_tail, rally_count)
        ]

    results = []
    seen_plans = set()
    for joker_trump in (False, True):
        last_candidates = gold_last_rally_candidates(
            cpu,
            room,
            rally_count,
            validator,
            joker_trump=joker_trump,
        )
        for last in last_candidates:
            last = dict(last)
            last["joker_trump"] = joker_trump
            reserved_hand = remaining_cards(cpu.hand, candidate_consumed_cards(last))
            reserved_cpu = temporary_cpu_with_hand(cpu, reserved_hand)
            last_strength = candidate_strength(last, room)

            def visit(current_cpu: CpuPlayer, bound_strength: int, selected_desc: list[dict]) -> None:
                if len(results) >= GOLD_PLAN_MAX_RESULTS_PER_COUNT * 2:
                    return
                tail = choose_gold_finish_tail(current_cpu, room, validator)
                if tail:
                    sequence = list(reversed(selected_desc)) + [last] + tail
                    plan_key = tuple(candidate_fingerprint(candidate) for candidate in sequence)
                    if plan_key not in seen_plans:
                        seen_plans.add(plan_key)
                        plan = finalize_gold_plan(cpu, room, sequence, rally_count)
                        plan["joker_trump"] = joker_trump
                        results.append(plan)
                    return
                if len(selected_desc) + 2 >= max_steps:
                    return

                branch_candidates = gold_plan_candidates(current_cpu, room, [rally_count], validator)
                branch_candidates = [
                    candidate for candidate in branch_candidates
                    if len(candidate.get("cards", [])) == rally_count
                    and len(candidate_consumed_cards(candidate)) < len(current_cpu.hand)
                    and candidate_strength(candidate, room) < bound_strength
                ]
                branch_candidates = sorted(
                    dedupe_candidates(branch_candidates),
                    key=lambda candidate: gold_plan_candidate_score(candidate, room),
                    reverse=True,
                )[:GOLD_PLAN_MAX_BRANCH_CANDIDATES]

                for candidate in branch_candidates:
                    next_hand = remaining_cards(current_cpu.hand, candidate_consumed_cards(candidate))
                    next_cpu = temporary_cpu_with_hand(current_cpu, next_hand)
                    visit(next_cpu, candidate_strength(candidate, room), selected_desc + [candidate])
                    if len(results) >= GOLD_PLAN_MAX_RESULTS_PER_COUNT * 2:
                        return

            visit(reserved_cpu, last_strength, [])
            if len(results) >= GOLD_PLAN_MAX_RESULTS_PER_COUNT * 2:
                break

    results.sort(key=gold_plan_score, reverse=True)
    return results[:GOLD_PLAN_MAX_RESULTS_PER_COUNT]


def gold_last_rally_candidates(
    cpu: CpuPlayer,
    room,
    rally_count: int,
    validator: NumberValidator,
    joker_trump: bool,
) -> list[dict]:
    if joker_trump:
        candidates = [
            candidate for candidate in joker_prime_candidates_for_count(cpu, room, rally_count, validator)
            if any(is_joker(card) for card in candidate.get("cards", []))
        ]
    else:
        candidates = gold_plan_candidates(cpu, room, [rally_count], validator)
    candidates = [
        candidate for candidate in candidates
        if len(candidate.get("cards", [])) == rally_count
        and len(candidate_consumed_cards(candidate)) < len(cpu.hand)
    ]
    return sorted(
        dedupe_candidates(candidates),
        key=lambda candidate: gold_plan_candidate_score(candidate, room),
        reverse=True,
    )[:GOLD_PLAN_MAX_LAST_CANDIDATES]


def choose_gold_finish_candidate(
    cpu: CpuPlayer,
    room,
    validator: NumberValidator,
) -> Optional[dict]:
    max_cards = min(9, len([card for card in cpu.hand if not is_joker(card)]))
    candidates = gold_plan_candidates(cpu, room, range(1, max_cards + 1), validator)
    hand_ids = {card.get("card_id") for card in cpu.hand}
    finish_candidates = [
        candidate for candidate in candidates
        if {card.get("card_id") for card in candidate_consumed_cards(candidate)} == hand_ids
    ]
    if len(cpu.hand) == 1 and is_joker(cpu.hand[0]):
        finish_candidates.append({
            "kind": "prime",
            "number": "X",
            "cards": cpu.hand[:],
            "assigned_numbers": [],
            "ranks": (),
        })
    finish_candidates.extend(joker_prime_finish_candidates(cpu, room, validator))
    if not finish_candidates:
        return None
    return max(finish_candidates, key=lambda candidate: gold_plan_candidate_score(candidate, room))


def choose_gold_finish_tail(
    cpu: CpuPlayer,
    room,
    validator: NumberValidator,
) -> list[dict]:
    finish = choose_gold_finish_candidate(cpu, room, validator)
    if finish is not None:
        finish["role"] = "finish"
        return [finish]

    cut = choose_gold_cut_candidate(cpu, room)
    if cut is not None:
        after_cut = temporary_cpu_with_hand(cpu, remaining_cards(cpu.hand, candidate_consumed_cards(cut)))
        finish = choose_gold_finish_candidate(after_cut, room, validator)
        if finish is not None:
            cut["role"] = "cut"
            finish["role"] = "finish"
            return [cut, finish]
    return []


def choose_gold_cut_candidate(cpu: CpuPlayer, room) -> Optional[dict]:
    cut = choose_57_cut(cpu.hand, room)
    if cut is not None and len(cut["cards"]) < len(cpu.hand):
        return {
            "kind": "prime",
            "number": 57,
            "cards": cut["cards"],
            "assigned_numbers": cut.get("assigned_numbers", []),
            "ranks": (5, 7),
        }

    joker = single_joker(cpu.hand)
    field_count = len(getattr(room, "field", []) or [])
    if joker is not None and field_count <= 1 and len(cpu.hand) > 1:
        return {
            "kind": "prime",
            "number": "X",
            "cards": [joker],
            "assigned_numbers": [],
            "ranks": (),
        }
    return None


def joker_prime_finish_candidates(
    cpu: CpuPlayer,
    room,
    validator: NumberValidator,
) -> list[dict]:
    if not any(is_joker(card) for card in cpu.hand):
        return []
    if len(cpu.hand) > 9:
        return []

    candidates = []
    for number in sorted(cpu.registered_primes):
        if not validator(number, cpu, getattr(room, "rule", None)):
            continue
        for ranks in registered_value_encodings(number, max_cards=9):
            if len(ranks) != len(cpu.hand):
                continue
            realization = cards_for_ranks_with_jokers(cpu.hand, ranks)
            if realization is None:
                continue
            if {card.get("card_id") for card in realization["cards"]} != {card.get("card_id") for card in cpu.hand}:
                continue
            if not beats_field(number, len(realization["cards"]), room):
                continue
            candidates.append({
                "kind": "prime",
                "number": number,
                "cards": realization["cards"],
                "assigned_numbers": realization["assigned_numbers"],
                "ranks": ranks,
            })
            break
    return candidates


def joker_prime_candidates_for_count(
    cpu: CpuPlayer,
    room,
    count: int,
    validator: NumberValidator,
) -> list[dict]:
    if not any(is_joker(card) for card in cpu.hand):
        return []
    if count < 1 or count > 9:
        return []

    candidates = []
    for number in sorted(cpu.registered_primes):
        if not validator(number, cpu, getattr(room, "rule", None)):
            continue
        for ranks in registered_value_encodings(number, max_cards=9):
            if len(ranks) != count:
                continue
            realization = cards_for_ranks_with_jokers(cpu.hand, ranks)
            if realization is None:
                continue
            if not any(is_joker(card) for card in realization["cards"]):
                continue
            if not beats_field(number, len(realization["cards"]), room):
                continue
            candidates.append({
                "kind": "prime",
                "number": number,
                "cards": realization["cards"],
                "assigned_numbers": realization["assigned_numbers"],
                "ranks": ranks,
            })
            break
    return candidates


def choose_gold_rally_candidate(
    cpu: CpuPlayer,
    room,
    rally_count: int,
    validator: NumberValidator,
) -> Optional[dict]:
    candidates = gold_plan_candidates(cpu, room, [rally_count], validator)
    candidates = [
        candidate for candidate in candidates
        if len(candidate.get("cards", [])) == rally_count
        and len(candidate_consumed_cards(candidate)) < len(cpu.hand)
    ]
    if not candidates:
        return None

    finishable = []
    for candidate in candidates:
        temp_cpu = temporary_cpu_with_hand(cpu, remaining_cards(cpu.hand, candidate_consumed_cards(candidate)))
        if choose_gold_finish_candidate(temp_cpu, room, validator) is not None:
            finishable.append(candidate)
    pool = finishable or candidates
    return min(pool, key=lambda candidate: gold_plan_candidate_score(candidate, room))


def gold_plan_candidates(
    cpu: CpuPlayer,
    room,
    counts: Iterable[int],
    validator: NumberValidator,
) -> List[dict]:
    candidates = knowledge_prime_candidates(cpu, room, validator, counts)
    candidates.extend(knowledge_composite_candidates(cpu, room, counts))
    return candidates


def gold_plan_candidate_score(candidate: dict, room) -> tuple:
    if candidate.get("number") == "X":
        return (10**100, 1)
    return (
        candidate_strength(candidate, room),
        -len(candidate_consumed_cards(candidate)),
    )


def append_gold_plan_step(steps: list[dict], cpu: CpuPlayer, candidate: dict, role: str) -> None:
    consume_cards = candidate_consumed_cards(candidate)
    step = dict(candidate)
    step["role"] = role
    step["remaining_before"] = len(cpu.hand)
    step["remaining_after"] = len(cpu.hand) - len(consume_cards)
    step["visible_count"] = len(candidate.get("cards", []))
    steps.append(step)


def finalize_gold_plan(
    cpu: CpuPlayer,
    room,
    sequence: list[dict],
    rally_count: int,
) -> dict:
    temp_cpu = temporary_cpu_with_hand(cpu, cpu.hand[:])
    steps = []
    for candidate in sequence:
        role = candidate.get("role", f"rally-{rally_count}")
        append_gold_plan_step(steps, temp_cpu, candidate, role=role)
        temp_cpu.hand = remaining_cards(temp_cpu.hand, candidate_consumed_cards(candidate))
    plan = {
        "steps": steps,
        "remaining": temp_cpu.hand,
        "completed": not temp_cpu.hand,
        "rally_count": rally_count,
        "last_rally_strength": gold_plan_last_rally_strength(steps, room),
    }
    plan["evaluation"] = evaluate_gold_plan(plan)
    return plan


def dedupe_candidates(candidates: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for candidate in candidates:
        key = candidate_fingerprint(candidate)
        if key in seen:
            continue
        seen.add(key)
        out.append(candidate)
    return out


def candidate_fingerprint(candidate: dict) -> tuple:
    return (
        candidate.get("kind"),
        candidate.get("number"),
        tuple(card.get("card_id") for card in candidate.get("cards", [])),
        tuple(card.get("card_id") for card in candidate.get("consume_cards", [])),
    )


def gold_plan_last_rally_strength(steps: list[dict], room) -> int:
    rally_steps = [step for step in steps if str(step.get("role", "")).startswith("rally-")]
    if not rally_steps:
        return -1
    return candidate_strength(rally_steps[-1], room)


def gold_plan_score(plan: dict) -> tuple:
    remaining_count = len(plan["remaining"])
    rally_steps = sum(1 for step in plan["steps"] if str(step.get("role", "")).startswith("rally-"))
    cut_steps = sum(1 for step in plan["steps"] if step.get("role") == "cut")
    evaluation_score = plan.get("evaluation", {}).get("score", 0)
    return (
        evaluation_score,
        1 if plan["completed"] else 0,
        -remaining_count,
        plan.get("last_rally_strength", -1),
        rally_steps,
        cut_steps,
        -len(plan["steps"]),
        plan["rally_count"],
    )


_GOLD_PLAN_EVALUATION_CONFIG = None


def gold_plan_evaluation_config() -> dict:
    global _GOLD_PLAN_EVALUATION_CONFIG
    if _GOLD_PLAN_EVALUATION_CONFIG is None:
        try:
            _GOLD_PLAN_EVALUATION_CONFIG = json.loads(GOLD_PLAN_EVALUATION_JSON.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            _GOLD_PLAN_EVALUATION_CONFIG = {
                "immediate_win_trump_strength": 100.0,
                "trump_strength": {},
                "resource_index": {},
            }
    return _GOLD_PLAN_EVALUATION_CONFIG


def evaluate_gold_plan(plan: dict) -> dict:
    config = gold_plan_evaluation_config()
    category = gold_plan_step_category(plan)
    x_role = gold_plan_x_role(plan)
    trump_strength = gold_plan_trump_strength_score(plan, config)
    resource_index = (
        config.get("resource_index", {})
        .get(category, {})
        .get(x_role)
    )
    if resource_index is None:
        resource_index = (
            config.get("resource_index", {})
            .get(category, {})
            .get("no_x", 1.0)
        )
    score = 100 - (100 - trump_strength) * float(resource_index)
    return {
        "score": round(score, 4),
        "trump_strength": trump_strength,
        "resource_index": resource_index,
        "step_category": category,
        "x_role": x_role,
    }


def gold_plan_step_category(plan: dict) -> str:
    steps = [
        step for step in plan.get("steps", [])
        if step.get("role") != "cut"
    ]
    if len(steps) == 1 and steps[0].get("role") == "finish":
        return "immediate"
    if len(steps) == 2 and steps[-1].get("role") == "finish":
        return "trump_finish"
    if len(steps) == 3:
        return "three_steps"
    if len(steps) == 4:
        return "four_steps"
    return "five_or_more"


def gold_plan_x_role(plan: dict) -> str:
    steps = plan.get("steps", [])
    last_rally_index = max(
        (index for index, step in enumerate(steps) if str(step.get("role", "")).startswith("rally-")),
        default=None,
    )
    x_step_indices = [
        index for index, step in enumerate(steps)
        if step_uses_joker(step)
    ]
    if not x_step_indices:
        return "x_single_saved" if any(is_joker(card) for card in plan.get("remaining", [])) else "no_x"

    index = max(x_step_indices)
    role = steps[index].get("role")
    if role == "finish":
        return "x_finish"
    if role == "cut":
        return "x_single_saved"
    if last_rally_index is not None and index == last_rally_index:
        return "x_trump"
    if last_rally_index is not None and index == last_rally_index - 1:
        return "x_before_trump"
    return "x_early"


def step_uses_joker(step: dict) -> bool:
    return any(
        is_joker(card)
        for card in step.get("cards", []) + step.get("consume_cards", [])
    )


def gold_plan_trump_strength_score(plan: dict, config: dict) -> float:
    steps = plan.get("steps", [])
    if gold_plan_step_category(plan) == "immediate":
        return float(config.get("immediate_win_trump_strength", 100.0))
    trump_step = next(
        (step for step in reversed(steps) if str(step.get("role", "")).startswith("rally-")),
        None,
    )
    if trump_step is None:
        trump_step = next((step for step in reversed(steps) if step.get("role") == "finish"), None)
    if trump_step is None:
        return 0.0
    number = trump_step.get("number")
    if number == "X":
        return 100.0
    try:
        value = int(number)
    except (TypeError, ValueError):
        return 0.0
    count = str(len(trump_step.get("cards", [])))
    table = config.get("trump_strength", {}).get(count)
    if not table:
        return 0.0
    score = float(table.get("default", 0.0))
    for threshold in table.get("thresholds", []):
        if value >= int(threshold.get("value", 0)):
            score = float(threshold.get("score", score))
    return score


def knowledge_prime_candidates(
    cpu: CpuPlayer,
    room,
    validator: NumberValidator,
    counts: Iterable[int],
) -> List[dict]:
    candidates = []
    count_set = {count for count in counts if count > 0}
    for number in sorted(cpu.registered_primes):
        if not validator(number, cpu, getattr(room, "rule", None)):
            continue
        for ranks in registered_value_encodings(number, max_cards=9):
            if len(ranks) not in count_set:
                continue
            cards = cards_for_ranks(cpu.hand, ranks)
            if cards is None:
                continue
            if not beats_field(number, len(cards), room):
                continue
            candidates.append({
                "kind": "prime",
                "number": number,
                "cards": cards,
                "assigned_numbers": [],
                "ranks": ranks,
            })
            break
    return candidates


def knowledge_composite_candidates(
    cpu: CpuPlayer,
    room,
    counts: Iterable[int],
) -> List[dict]:
    if not getattr(getattr(room, "rule", None), "allow_composite", False):
        return []
    count_set = {count for count in counts if 2 <= count <= 4}
    if not count_set:
        return []

    entries_by_value: dict[int, list] = {}
    for entry in cpu.registered_composite_entries:
        entries_by_value.setdefault(entry.value, []).append(entry)

    candidates = []
    for value in sorted(set(cpu.registered_composites) | set(entries_by_value)):
        for visible_ranks in registered_value_encodings(value, max_cards=4):
            if len(visible_ranks) not in count_set:
                continue
            visible_cards = cards_for_ranks(cpu.hand, visible_ranks)
            if visible_cards is None:
                continue
            material = material_for_composite_entries(
                cpu.hand,
                entries_by_value.get(value, []),
                visible_cards,
            )
            if material is None:
                continue
            if not beats_field(value, len(visible_cards), room):
                continue
            candidates.append({
                "kind": "composite",
                "number": value,
                "cards": visible_cards,
                "assigned_numbers": [],
                "consume_cards": material["cards"],
                "composite_tokens": material["tokens"],
                "composite_assigned_numbers": [],
                "expression": material.get("expression", ""),
                "expression_source": material.get("source", "registered"),
                "ranks": visible_ranks,
            })
            break
    return candidates


def strongest_candidates_by_count(candidates: List[dict], room) -> dict[int, dict]:
    trumps = {}
    for candidate in candidates:
        count = len(candidate["cards"])
        current = trumps.get(count)
        if current is None or candidate_strength(candidate, room) > candidate_strength(current, room):
            trumps[count] = candidate
    return trumps


def strongest_trumps_by_count(cpu: CpuPlayer, room, validator: NumberValidator) -> dict[int, dict]:
    max_cards = min(9, len([card for card in cpu.hand if not is_joker(card)]))
    counts = range(1, max_cards + 1)
    candidates = knowledge_prime_candidates(cpu, room_without_field(room), validator, counts)
    candidates.extend(knowledge_composite_candidates(cpu, room_without_field(room), range(2, 5)))
    joker = single_joker(cpu.hand)
    if joker is not None:
        candidates.append({
            "kind": "joker_cut",
            "number": float("inf"),
            "cards": [joker],
            "assigned_numbers": [],
            "ranks": (),
        })
    return strongest_candidates_by_count(candidates, room)


def gold_candidate_score(
    cpu: CpuPlayer,
    room,
    candidate: dict,
    trumps: dict[int, dict],
    validator: NumberValidator,
) -> tuple:
    count = len(candidate["cards"])
    is_trump = same_candidate(trumps.get(count), candidate)
    remaining = remaining_cards(cpu.hand, candidate_consumed_cards(candidate))
    has_followup = has_remaining_known_play(cpu, room, remaining, validator)
    return (
        1 if has_followup else 0,
        0 if is_trump else 1,
        1 if candidate.get("kind") == "prime" else 0,
        count,
        candidate_strength(candidate, room),
    )


def candidate_strength(candidate: dict, room) -> int:
    if candidate.get("kind") == "joker_cut":
        return 10**100
    if candidate.get("number") == "X":
        return 10**100
    number = int(candidate["number"])
    return -number if getattr(room, "reverse_order", False) else number


def same_candidate(left: Optional[dict], right: Optional[dict]) -> bool:
    if left is None or right is None:
        return False
    return (
        left.get("kind") == right.get("kind")
        and left.get("number") == right.get("number")
        and {card.get("card_id") for card in left.get("cards", [])}
        == {card.get("card_id") for card in right.get("cards", [])}
    )


def has_remaining_known_play(
    cpu: CpuPlayer,
    room,
    remaining: List[Card],
    validator: NumberValidator,
) -> bool:
    temp_cpu = temporary_cpu_with_hand(cpu, remaining)
    empty_room = room_without_field(room)
    max_cards = min(9, len([card for card in remaining if not is_joker(card)]))
    counts = range(1, max_cards + 1)
    return bool(
        knowledge_prime_candidates(temp_cpu, empty_room, validator, counts)
        or knowledge_composite_candidates(temp_cpu, empty_room, counts)
    )


def candidate_to_action(candidate: dict) -> CpuAction:
    if candidate.get("kind") == "composite":
        return CpuAction("play_composite", {
            "selected": {
                "cards": candidate["cards"],
                "assigned_numbers": candidate.get("assigned_numbers", []),
            },
            "consume": {
                "cards": candidate.get("consume_cards", []),
            },
            "composite": {
                "tokens": candidate.get("composite_tokens", []),
                "assigned_numbers": candidate.get("composite_assigned_numbers", []),
            },
        })
    return CpuAction("play_prime", {
        "cards": candidate["cards"],
        "assigned_numbers": candidate.get("assigned_numbers", []),
    })


def candidate_consumed_cards(candidate: dict) -> List[Card]:
    return list({
        card.get("card_id"): card
        for card in candidate.get("cards", []) + candidate.get("consume_cards", [])
    }.values())


def material_for_composite_entry(
    hand: List[Card],
    entry,
    visible_cards: List[Card],
) -> Optional[dict]:
    excluded_ids = {card.get("card_id") for card in visible_cards}
    used_ids = set(excluded_ids)
    cards = []
    tokens = []
    for expression_token in entry.expression_tokens:
        if expression_token.kind == "op":
            tokens.append({
                "kind": "op",
                "op": "\u00d7" if expression_token.op == "*" else expression_token.op,
            })
            continue
        if expression_token.kind != "cards":
            return None
        for rank in expression_token.ranks:
            card = next(
                (
                    card for card in hand
                    if not is_joker(card)
                    and card.get("rank") == rank
                    and card.get("card_id") not in used_ids
                ),
                None,
            )
            if card is None:
                return None
            used_ids.add(card.get("card_id"))
            cards.append(card)
            tokens.append({"kind": "card", "card_id": card.get("card_id")})
    return {
        "cards": cards,
        "tokens": tokens,
        "expression": getattr(entry, "expression", ""),
        "source": "registered",
    }


def material_for_composite_entries(
    hand: List[Card],
    entries: Iterable,
    visible_cards: List[Card],
) -> Optional[dict]:
    for entry in entries:
        material = material_for_composite_entry(hand, entry, visible_cards)
        if material is not None:
            return material
    return None


def cards_for_ranks(hand: List[Card], ranks: tuple[int, ...]) -> Optional[List[Card]]:
    available = [card for card in hand if not is_joker(card)]
    selected = []
    used_ids = set()
    for rank in ranks:
        card = next(
            (
                card for card in available
                if card.get("rank") == rank and card.get("card_id") not in used_ids
            ),
            None,
        )
        if card is None:
            return None
        selected.append(card)
        used_ids.add(card.get("card_id"))
    return selected


def cards_for_ranks_with_jokers(hand: List[Card], ranks: tuple[int, ...]) -> Optional[dict]:
    selected = []
    assigned_by_card_id = {}
    used_ids = set()
    jokers = [card for card in hand if is_joker(card)]

    for rank in ranks:
        card = next(
            (
                card for card in hand
                if not is_joker(card)
                and card.get("rank") == rank
                and card.get("card_id") not in used_ids
            ),
            None,
        )
        if card is None:
            card = next(
                (
                    joker for joker in jokers
                    if joker.get("card_id") not in used_ids
                ),
                None,
            )
            if card is None:
                return None
            assigned_by_card_id[card.get("card_id")] = str(rank)
        selected.append(card)
        used_ids.add(card.get("card_id"))

    return {
        "cards": selected,
        "assigned_numbers": [
            assigned_by_card_id[card.get("card_id")]
            for card in selected
            if is_joker(card)
        ],
    }


def remaining_cards(hand: List[Card], used_cards: List[Card]) -> List[Card]:
    remaining = hand[:]
    for card in used_cards:
        if card in remaining:
            remaining.remove(card)
    return remaining


def temporary_cpu_with_hand(cpu: CpuPlayer, hand: List[Card]) -> CpuPlayer:
    temp = CpuPlayer(name=cpu.name, player_id=cpu.id, cpu_key=cpu.cpu_key)
    temp.hand = hand
    temp.registered_primes = cpu.registered_primes
    temp.registered_composites = cpu.registered_composites
    temp.registered_composite_entries = cpu.registered_composite_entries
    return temp


def room_without_field(room):
    class EmptyFieldRoom:
        pass
    copy = EmptyFieldRoom()
    copy.rule = getattr(room, "rule", None)
    copy.field = []
    copy.last_number = None
    copy.reverse_order = getattr(room, "reverse_order", False)
    return copy


def choose_57_cut(hand: List[Card], room) -> Optional[dict]:
    field_count = len(getattr(room, "field", []) or [])
    if field_count not in (0, 2):
        return None
    cards = cards_for_ranks(hand, (5, 7))
    if cards is None:
        return None
    return {"cards": cards, "assigned_numbers": []}


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
    "gold_planner": CpuProfile(
        key="gold_planner",
        label="GOLD組み切りCPU",
        description="GOLD素数表だけを基本知識として参照し、切り札を温存しながら枚数別に候補を探す試作CPUです。",
        prime_rules=(PrimeRule.NORMAL, PrimeRule.REGISTERED),
        knowledge=CpuKnowledgeSpec(source="gold", load_timing="always"),
        action_selector=choose_gold_planning_cpu_action,
    ),
}
