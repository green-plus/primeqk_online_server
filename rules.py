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
    NORMAL = auto()       # 通常の素数
    TETRAD = auto()       # 四つ子素数
    SEMIPRIME = auto()    # 半素数
    REGISTERED = auto()   # プレイヤーごとの登録済み素数

@dataclass(frozen=True)
class RulePreset:
    key: str
    label: str
    deck_rule: DeckRule
    hand_size: int
    penalty_rule: PenaltyRule
    allow_composite: bool = False
    start_revolution: bool = False
    prime_rule: PrimeRule = PrimeRule.NORMAL
    assist_enabled: bool = False
    registration_enabled: bool = False

PRESETS: Dict[str, RulePreset] = {
    "std-5-1": RulePreset(
        key="std-5-1",
        label="5枚 / ペナ1",
        deck_rule=DeckRule.DEFAULT,
        hand_size=5,
        penalty_rule=PenaltyRule.ALWAYS_1,
        allow_composite=True,
    ),
    "std-7-1": RulePreset(
        key="std-7-1",
        label="7枚 / ペナ1",
        deck_rule=DeckRule.DEFAULT,
        hand_size=7,
        penalty_rule=PenaltyRule.ALWAYS_1,
        allow_composite=True,
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
        label="11枚 / ペナ場の枚数",
        deck_rule=DeckRule.DEFAULT,
        hand_size=11,
        penalty_rule=PenaltyRule.FIELD_COUNT,
        allow_composite=True,
    ),
    "std-11-n-c": RulePreset(
        key="std-11-n-c",
        label="11枚 / 通常",
        deck_rule=DeckRule.DEFAULT,
        hand_size=11,
        penalty_rule=PenaltyRule.NORMAL,
        allow_composite=True,
    ),
    "std-11-n-no-c": RulePreset(
        key="std-11-n-no-c",
        label="11枚 / 合成数なし",
        deck_rule=DeckRule.DEFAULT,
        hand_size=11,
        penalty_rule=PenaltyRule.NORMAL,
        allow_composite=False,
    ),
    "std-11-n-c-rev": RulePreset(
        key="std-11-n-c-rev",
        label="初期革命: 11枚 / 通常",
        deck_rule=DeckRule.DEFAULT,
        hand_size=11,
        penalty_rule=PenaltyRule.NORMAL,
        allow_composite=True,
        start_revolution=True,
    ),
    "half-5-f": RulePreset(
        key="half-5-f",
        label="偶数半減: 5枚 / 場の枚数",
        deck_rule=DeckRule.EVEN_HALVED,
        hand_size=5,
        penalty_rule=PenaltyRule.FIELD_COUNT,
        allow_composite=False,
    ),
    "half-7-1-c": RulePreset(
        key="half-7-1-c",
        label="7枚 / 偶数半減 / ペナ1",
        deck_rule=DeckRule.EVEN_HALVED,
        hand_size=7,
        penalty_rule=PenaltyRule.ALWAYS_1,
        allow_composite=True,
    ),
    "tetrad-11-n": RulePreset(
        key="tetrad-11-n",
        label="四つ子素数: 11枚 / 通常",
        deck_rule=DeckRule.DEFAULT,
        hand_size=11,
        penalty_rule=PenaltyRule.NORMAL,
        allow_composite=False,
        prime_rule=PrimeRule.TETRAD,
    ),
    "tetrad-11-n-c": RulePreset(
        key="tetrad-11-n-c",
        label="四つ子素数: 11枚 / 通常",
        deck_rule=DeckRule.DEFAULT,
        hand_size=11,
        penalty_rule=PenaltyRule.NORMAL,
        allow_composite=True,
        prime_rule=PrimeRule.TETRAD,
    ),
    "semiprime-11-n": RulePreset(
        key="semiprime-11-n",
        label="半素数: 11枚 / 通常",
        deck_rule=DeckRule.DEFAULT,
        hand_size=11,
        penalty_rule=PenaltyRule.NORMAL,
        allow_composite=False,
        prime_rule=PrimeRule.SEMIPRIME,
    ),
    "semiprime-11-n-c": RulePreset(
        key="semiprime-11-n-c",
        label="半素数: 11枚 / 通常",
        deck_rule=DeckRule.DEFAULT,
        hand_size=11,
        penalty_rule=PenaltyRule.NORMAL,
        allow_composite=True,
        prime_rule=PrimeRule.SEMIPRIME,
    ),
    "semiprime-11-1-c": RulePreset(
        key="semiprime-11-1-c",
        label="半素数: 11枚 / ペナルティ1枚",
        deck_rule=DeckRule.DEFAULT,
        hand_size=11,
        penalty_rule=PenaltyRule.ALWAYS_1,
        allow_composite=True,
        prime_rule=PrimeRule.SEMIPRIME,
    ),
    "registered-11-n": RulePreset(
        key="registered-11-n",
        label="登録制限: 11枚 / 通常 / アシストなし",
        deck_rule=DeckRule.DEFAULT,
        hand_size=11,
        penalty_rule=PenaltyRule.NORMAL,
        allow_composite=True,
        prime_rule=PrimeRule.REGISTERED,
        registration_enabled=True,
    ),
    "registered-11-n-assist": RulePreset(
        key="registered-11-n-assist",
        label="登録制限: 11枚 / 通常 / アシストあり",
        deck_rule=DeckRule.DEFAULT,
        hand_size=11,
        penalty_rule=PenaltyRule.NORMAL,
        allow_composite=True,
        prime_rule=PrimeRule.REGISTERED,
        assist_enabled=True,
        registration_enabled=True,
    ),
    "neo-assist-11-n-unlimited": RulePreset(
        key="neo-assist-11-n-unlimited",
        label="登録アシスト: 11枚 / 通常 / 制限なし",
        deck_rule=DeckRule.DEFAULT,
        hand_size=11,
        penalty_rule=PenaltyRule.NORMAL,
        allow_composite=True,
        assist_enabled=True,
        registration_enabled=True,
    ),
}
