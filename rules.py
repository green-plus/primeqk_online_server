# rules.py  ------------------------------------------------------
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum, auto
from typing import Callable, List, Dict

# ―― 軸ごとの単純な Enum ―――――――――――――――――――――――――――――――――
class DeckRule(Enum):
    DEFAULT      = auto()   # 52+J×2
    EVEN_HALVED  = auto()   # 偶数カードを半分に間引く

class PenaltyRule(Enum):
    ALWAYS_1         = auto()   # 必ず 1 枚
    SAME_AS_PLAYED   = auto()   # 出した枚数と同じ

# ―― ルールプリセット定義 ―――――――――――――――――――――――――――――――
@dataclass(frozen=True)
class RulePreset:
    key: str                # "std-5-1" など内部 ID
    label: str              # UI に表示する名前
    deck_rule: DeckRule
    hand_size: int
    penalty_rule: PenaltyRule

# 必要なプリセットだけ宣言（後でここを書き換えるだけで OK）
PRESETS: Dict[str, RulePreset] = {
    "std-5-1":  RulePreset("std-5-1",  "標準: 5枚 / ペナ1",  DeckRule.DEFAULT,     5,  PenaltyRule.ALWAYS_1),
    "std-7-1":  RulePreset("std-7-1",  "標準: 7枚 / ペナ1",  DeckRule.DEFAULT,     7,  PenaltyRule.ALWAYS_1),
    "std-11-n": RulePreset("std-11-n", "標準: 11枚 / 同数", DeckRule.DEFAULT,     11, PenaltyRule.SAME_AS_PLAYED),
    "half-5-n": RulePreset("half-5-n", "偶数半減: 5枚 / 同数", DeckRule.EVEN_HALVED, 5,  PenaltyRule.SAME_AS_PLAYED),
}
# ---------------------------------------------------------------
