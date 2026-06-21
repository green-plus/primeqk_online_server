from __future__ import annotations

import re
import csv
import json
from dataclasses import dataclass
from functools import lru_cache
from io import StringIO


FACE_VALUES = {"t": 10, "j": 11, "q": 12, "k": 13}
VALUE_SYMBOLS = {10: "t", 11: "j", 12: "q", 13: "k"}
TOKEN_RE = re.compile(r"^[0-9tjqk]+$", re.IGNORECASE)
TOKEN_SPLIT_RE = re.compile(r"[\s,、，]+")
HEADING_RE = re.compile(r"^\s*(\d+)(?:\s*[~～]\s*(\d+))?\s*枚(.*)$")

MAX_REGISTERED_PRIME_TEXT_LENGTH = 200_000
MAX_REGISTERED_PRIMES = 20_000
MAX_REGISTERED_PRIME_DIGITS = 72
MAX_ONE_CARDS_IN_PRIME_ENCODING = 4


@dataclass(frozen=True)
class RegisteredPrimeEntry:
    source_line: int
    pattern: str
    value: int
    cards: tuple[int, ...]
    card_count: int
    section: str | None = None
    stated_card_count_min: int | None = None
    stated_card_count_max: int | None = None
    count_matches_section: bool | None = None


@dataclass(frozen=True)
class RegisteredPrimeError:
    source_line: int
    token: str
    message: str


@dataclass(frozen=True)
class RegisteredPrimeParseResult:
    entries: tuple[RegisteredPrimeEntry, ...]
    errors: tuple[RegisteredPrimeError, ...]
    prime_values: tuple[int, ...]
    duplicate_count: int
    truncated: bool = False


def tokenize_registered_prime_pattern(pattern: str) -> tuple[int, ...]:
    """Convert pasted physical-card notation to ranks.

    t/j/q/k mean 10/11/12/13. A literal 0 is accepted as a ten card for
    compatibility with the source data format; it is not a joker.
    """
    pattern = pattern.strip().lower()
    if not TOKEN_RE.fullmatch(pattern):
        raise ValueError("invalid token")
    return tuple(
        10 if char == "0" else FACE_VALUES[char] if char in FACE_VALUES else int(char)
        for char in pattern
    )


def registered_prime_pattern_value(pattern: str) -> int:
    text = "".join(str(FACE_VALUES.get(char, char)) for char in pattern.strip().lower())
    return int(text)


def registered_cards_value(cards: tuple[int, ...]) -> int:
    return int("".join(str(rank) for rank in cards))


def registered_cards_label(cards: tuple[int, ...]) -> str:
    return "".join(VALUE_SYMBOLS.get(rank, str(rank)) for rank in cards)


def registered_prime_encoding_allowed(cards: tuple[int, ...]) -> bool:
    return cards.count(1) <= MAX_ONE_CARDS_IN_PRIME_ENCODING


@lru_cache(maxsize=None)
def registered_value_encodings(value: int, max_cards: int = 13) -> tuple[tuple[int, ...], ...]:
    """Return physical-card encodings of a value, allowing 10-13 as face cards."""
    text = str(value)
    results: set[tuple[int, ...]] = set()

    def visit(index: int, cards: tuple[int, ...]) -> None:
        if len(cards) > max_cards:
            return
        if index == len(text):
            if registered_cards_value(cards) == value and registered_prime_encoding_allowed(cards):
                results.add(cards)
            return

        digit = int(text[index])
        if digit:
            visit(index + 1, cards + (digit,))

        if index + 1 < len(text):
            pair = int(text[index : index + 2])
            if 10 <= pair <= 13:
                visit(index + 2, cards + (pair,))

    visit(0, ())
    return tuple(sorted(results, key=lambda cards: (len(cards), cards)))


def is_probable_prime_for_registration(n: int) -> bool:
    if n < 2:
        return False

    small_primes = (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37)
    for prime in small_primes:
        if n == prime:
            return True
        if n % prime == 0:
            return False

    d, s = n - 1, 0
    while d % 2 == 0:
        s += 1
        d //= 2

    for base in small_primes:
        x = pow(base, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(s - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def parse_registered_prime_text(text: str) -> RegisteredPrimeParseResult:
    """Parse pasted player prime knowledge into unique prime values.

    Accepted tokens are decimal digits plus t/j/q/k face-card notation. Optional
    headings such as "3枚" and "4～6枚" are preserved for diagnostics.
    """
    if len(text) > MAX_REGISTERED_PRIME_TEXT_LENGTH:
        return RegisteredPrimeParseResult(
            entries=(),
            errors=(RegisteredPrimeError(0, "", "input too long"),),
            prime_values=(),
            duplicate_count=0,
            truncated=True,
        )

    if _looks_like_prime_memory_csv(text):
        return parse_registered_prime_csv(text)

    entries: list[RegisteredPrimeEntry] = []
    errors: list[RegisteredPrimeError] = []
    seen_values: set[int] = set()
    duplicate_count = 0
    section: str | None = None
    stated_min: int | None = None
    stated_max: int | None = None

    for line_number, line in enumerate(text.splitlines(), start=1):
        heading = HEADING_RE.match(line)
        if heading:
            stated_min = int(heading.group(1))
            stated_max = int(heading.group(2)) if heading.group(2) else stated_min
            section = line.strip()
            continue

        for token in (item for item in TOKEN_SPLIT_RE.split(line.strip().lower()) if item):
            if not TOKEN_RE.fullmatch(token):
                errors.append(RegisteredPrimeError(line_number, token, "invalid token"))
                continue

            if len(token) > MAX_REGISTERED_PRIME_DIGITS:
                errors.append(RegisteredPrimeError(line_number, token, "token too long"))
                continue

            try:
                cards = tokenize_registered_prime_pattern(token)
                value = registered_prime_pattern_value(token)
            except ValueError:
                errors.append(RegisteredPrimeError(line_number, token, "invalid token"))
                continue

            if not is_probable_prime_for_registration(value):
                errors.append(RegisteredPrimeError(line_number, token, "not prime"))
                continue

            count_matches = (
                stated_min <= len(cards) <= stated_max
                if stated_min is not None and stated_max is not None
                else None
            )
            entries.append(RegisteredPrimeEntry(
                source_line=line_number,
                pattern=token,
                value=value,
                cards=cards,
                card_count=len(cards),
                section=section,
                stated_card_count_min=stated_min,
                stated_card_count_max=stated_max,
                count_matches_section=count_matches,
            ))

            if value in seen_values:
                duplicate_count += 1
            seen_values.add(value)

            if len(seen_values) > MAX_REGISTERED_PRIMES:
                errors.append(RegisteredPrimeError(line_number, token, "too many primes"))
                return RegisteredPrimeParseResult(
                    entries=tuple(entries),
                    errors=tuple(errors),
                    prime_values=tuple(sorted(seen_values)),
                    duplicate_count=duplicate_count,
                    truncated=True,
                )

    return RegisteredPrimeParseResult(
        entries=tuple(entries),
        errors=tuple(errors),
        prime_values=tuple(sorted(seen_values)),
        duplicate_count=duplicate_count,
    )


def _looks_like_prime_memory_csv(text: str) -> bool:
    first_line = next((line for line in text.splitlines() if line.strip()), "")
    if not first_line:
        return False
    columns = [column.strip() for column in first_line.split(",")]
    return "prime_value" in columns


def parse_registered_prime_csv(text: str) -> RegisteredPrimeParseResult:
    entries: list[RegisteredPrimeEntry] = []
    errors: list[RegisteredPrimeError] = []
    seen_values: set[int] = set()
    duplicate_count = 0

    reader = csv.DictReader(StringIO(text))
    if not reader.fieldnames or "prime_value" not in reader.fieldnames:
        return RegisteredPrimeParseResult(
            entries=(),
            errors=(RegisteredPrimeError(1, "", "missing prime_value column"),),
            prime_values=(),
            duplicate_count=0,
        )

    for row_number, row in enumerate(reader, start=2):
        token = (row.get("prime_value") or "").strip()
        if not token:
            errors.append(RegisteredPrimeError(row_number, token, "empty prime_value"))
            continue
        if not token.isdigit():
            errors.append(RegisteredPrimeError(row_number, token, "invalid prime_value"))
            continue
        if len(token) > MAX_REGISTERED_PRIME_DIGITS:
            errors.append(RegisteredPrimeError(row_number, token, "token too long"))
            continue

        value = int(token)
        if not is_probable_prime_for_registration(value):
            errors.append(RegisteredPrimeError(row_number, token, "not prime"))
            continue

        cards = _csv_cards(row)
        card_count = _csv_int(row.get("card_count"), default=len(cards))
        stated_min = _csv_int(row.get("stated_card_count"), default=None)
        stated_max = _csv_int(row.get("stated_card_count_max"), default=stated_min)
        count_matches = (
            stated_min <= card_count <= stated_max
            if stated_min is not None and stated_max is not None
            else None
        )

        entries.append(RegisteredPrimeEntry(
            source_line=row_number,
            pattern=(row.get("pattern") or token).strip(),
            value=value,
            cards=cards,
            card_count=card_count,
            section=(row.get("section") or None),
            stated_card_count_min=stated_min,
            stated_card_count_max=stated_max,
            count_matches_section=count_matches,
        ))

        if value in seen_values:
            duplicate_count += 1
        seen_values.add(value)

        if len(seen_values) > MAX_REGISTERED_PRIMES:
            errors.append(RegisteredPrimeError(row_number, token, "too many primes"))
            return RegisteredPrimeParseResult(
                entries=tuple(entries),
                errors=tuple(errors),
                prime_values=tuple(sorted(seen_values)),
                duplicate_count=duplicate_count,
                truncated=True,
            )

    return RegisteredPrimeParseResult(
        entries=tuple(entries),
        errors=tuple(errors),
        prime_values=tuple(sorted(seen_values)),
        duplicate_count=duplicate_count,
    )


def _csv_cards(row: dict[str, str]) -> tuple[int, ...]:
    cards_json = (row.get("cards_json") or "").strip()
    if not cards_json:
        return ()
    try:
        values = json.loads(cards_json)
    except json.JSONDecodeError:
        return ()
    if not isinstance(values, list):
        return ()
    cards = []
    for value in values:
        try:
            cards.append(int(value))
        except (TypeError, ValueError):
            return ()
    return tuple(cards)


def _csv_int(value: str | None, default):
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default
