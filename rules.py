# rules.py
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum, auto
from typing import Dict

class DeckRule(Enum):
    DEFAULT = auto()       # 通常の54枚デッキ
    EVEN_HALVED = auto()   # 偶数カードを半分に間引く

class PenaltyRule(Enum):
    ALWAYS_1 = auto()      # 必ず1枚
    FIELD_COUNT = auto()   # 場の枚数
    NORMAL = auto()        # 通常（合成数では材料札も含む）

class PrimeRule(Enum):
    NORMAL = auto()      # 通常の素数
    TETRAD = auto()      # 四つ子素数

@dataclass(frozen=True)
class RulePreset:
    key: str
    label: str
    deck_rule: DeckRule
    hand_size: int
    penalty_rule: PenaltyRule
    allow_composite: bool = False
    prime_rule: PrimeRule = PrimeRule.NORMAL

PRESETS: Dict[str, RulePreset] = {
    "std-5-1": RulePreset(
        key="std-5-1",
        label="標準: 5枚 / ペナ1",
        deck_rule=DeckRule.DEFAULT,
        hand_size=5,
        penalty_rule=PenaltyRule.ALWAYS_1,
        allow_composite=False,
    ),
    "std-7-1": RulePreset(
        key="std-7-1",
        label="標準: 7枚 / ペナ1",
        deck_rule=DeckRule.DEFAULT,
        hand_size=7,
        penalty_rule=PenaltyRule.ALWAYS_1,
        allow_composite=False,
    ),
    "std-11-f": RulePreset(
        key="std-11-f",
        label="標準: 11枚 / 場の枚数",
        deck_rule=DeckRule.DEFAULT,
        hand_size=11,
        penalty_rule=PenaltyRule.FIELD_COUNT,
        allow_composite=False,
    ),
    "std-11-f-c": RulePreset(
        key="std-11-f-c",
        label="標準: 11枚 / 場の枚数 / 合成数あり",
        deck_rule=DeckRule.DEFAULT,
        hand_size=11,
        penalty_rule=PenaltyRule.FIELD_COUNT,
        allow_composite=True,
    ),
    "std-11-n-c": RulePreset(
        key="std-11-n-c",
        label="標準: 11枚 / 通常 / 合成数あり",
        deck_rule=DeckRule.DEFAULT,
        hand_size=11,
        penalty_rule=PenaltyRule.NORMAL,
        allow_composite=True,
    ),
    "half-5-f": RulePreset(
        key="half-5-f",
        label="偶数半減: 5枚 / 場の枚数",
        deck_rule=DeckRule.EVEN_HALVED,
        hand_size=5,
        penalty_rule=PenaltyRule.FIELD_COUNT,
        allow_composite=False,
    ),
    "half-7-1": RulePreset(
        key="half-7-1",
        label="偶数半減: 7枚 / ペナ1 / 合成数あり",
        deck_rule=DeckRule.EVEN_HALVED,
        hand_size=7,
        penalty_rule=PenaltyRule.ALWAYS_1,
        allow_composite=True,
    ),
    "tetrad-11-n": RulePreset(
        key="tetrad-11-f",
        label="四つ子素数: 11枚 / 通常",
        deck_rule=DeckRule.DEFAULT,
        hand_size=11,
        penalty_rule=PenaltyRule.NORMAL,
        allow_composite=False,
        prime_rule=PrimeRule.TETRAD,
    ),
    "tetrad-11-n-c": RulePreset(
        key="tetrad-11-f-c",
        label="四つ子素数: 11枚 / 通常 / 合成数あり",
        deck_rule=DeckRule.DEFAULT,
        hand_size=11,
        penalty_rule=PenaltyRule.NORMAL,
        allow_composite=True,
        prime_rule=PrimeRule.TETRAD,
    ),
}
