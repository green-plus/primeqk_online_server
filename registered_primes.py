from __future__ import annotations

import re
import csv
import json
from dataclasses import dataclass
from functools import lru_cache
from itertools import combinations, product
from io import StringIO
from collections import defaultdict


FACE_VALUES = {"t": 10, "j": 11, "q": 12, "k": 13}
VALUE_SYMBOLS = {10: "t", 11: "j", 12: "q", 13: "k"}
TOKEN_RE = re.compile(r"^[0-9tjqk]+$", re.IGNORECASE)
TOKEN_SPLIT_RE = re.compile(r"[\s,、，]+")
HEADING_RE = re.compile(r"^\s*(\d+)(?:\s*[~～]\s*(\d+))?\s*枚(.*)$")

MAX_REGISTERED_PRIME_TEXT_LENGTH = 200_000
MAX_REGISTERED_PRIMES = 20_000
MAX_REGISTERED_PRIME_DIGITS = 72
MAX_ONE_CARDS_IN_PRIME_ENCODING = 4
MAX_COMPOSITE_AUTO_RANK_COPIES = 4
MAX_COMPOSITE_AUTO_EXPONENT = 122
MAX_COMPOSITE_AUTO_FACTOR_TRIAL = 1_000_000


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


@dataclass(frozen=True)
class RegisteredCompositeParseResult:
    entries: tuple["RegisteredCompositeEntry", ...]
    composite_values: tuple[int, ...]
    errors: tuple[RegisteredPrimeError, ...]
    duplicate_count: int
    truncated: bool = False


@dataclass(frozen=True)
class RegisteredCompositeExpressionToken:
    kind: str
    ranks: tuple[int, ...] = ()
    op: str | None = None
    text: str = ""


@dataclass(frozen=True)
class RegisteredCompositeEntry:
    source_line: int
    pattern: str
    value: int
    expression: str
    expression_tokens: tuple[RegisteredCompositeExpressionToken, ...]


@dataclass(frozen=True)
class RegisteredPrimeTemplateIndex:
    """Reusable lookup for matching registered numbers by physical-card ranks."""
    exact_by_signature: dict[tuple[int, ...], tuple[tuple[int, tuple[int, ...]], ...]]
    by_visible_signature: dict[tuple[int, tuple[int, ...]], tuple[tuple[int, tuple[int, ...]], ...]]
    templates_by_card_count: dict[int, tuple[tuple[int, tuple[int, ...]], ...]]


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


def registered_number_pattern_value(pattern: str) -> int:
    pattern = pattern.strip().lower()
    if not TOKEN_RE.fullmatch(pattern):
        raise ValueError("invalid token")
    return registered_prime_pattern_value(pattern)


def registered_cards_value(cards: tuple[int, ...]) -> int:
    return int("".join(str(rank) for rank in cards))


def registered_cards_label(cards: tuple[int, ...]) -> str:
    return "".join(VALUE_SYMBOLS.get(rank, str(rank)) for rank in cards)


def registered_prime_encoding_allowed(cards: tuple[int, ...]) -> bool:
    return cards.count(1) <= MAX_ONE_CARDS_IN_PRIME_ENCODING


@lru_cache(maxsize=32)
def registered_prime_template_index(
    values: tuple[int, ...],
    max_cards: int = 9,
    max_jokers: int = 2,
) -> RegisteredPrimeTemplateIndex:
    """Build a cached physical-card index for one registered-prime knowledge set.

    The exact signature handles ordinary hands.  The visible signature removes
    one or two ranks from a template, allowing the same index to answer joker
    substitutions without scanning every registered number again.
    """
    exact = defaultdict(list)
    visible = defaultdict(list)
    by_count = defaultdict(list)
    for value in values:
        for cards in registered_value_encodings(value, max_cards=max_cards):
            signature = tuple(sorted(cards))
            template = (value, cards)
            exact[signature].append(template)
            by_count[len(cards)].append(template)
            for joker_count in range(1, min(max_jokers, len(cards)) + 1):
                seen_visible = set()
                for omitted_indices in combinations(range(len(cards)), joker_count):
                    omitted = set(omitted_indices)
                    visible_signature = tuple(
                        rank for index, rank in enumerate(signature)
                        if index not in omitted
                    )
                    if visible_signature in seen_visible:
                        continue
                    seen_visible.add(visible_signature)
                    visible[(joker_count, visible_signature)].append(template)

    def freeze(mapping):
        return {
            key: tuple(dict.fromkeys(templates))
            for key, templates in mapping.items()
        }

    return RegisteredPrimeTemplateIndex(
        exact_by_signature=freeze(exact),
        by_visible_signature=freeze(visible),
        templates_by_card_count=freeze(by_count),
    )


@lru_cache(maxsize=8192)
def _registered_prime_templates_for_hand(
    values: tuple[int, ...],
    signature: tuple[int, ...],
    joker_count: int,
    max_cards: int,
) -> tuple[tuple[int, tuple[int, ...]], ...]:
    total_count = len(signature) + joker_count
    if total_count > max_cards:
        return ()
    index = registered_prime_template_index(values, max_cards=max_cards)
    if joker_count == 0:
        return index.exact_by_signature.get(signature, ())
    if joker_count <= 2:
        return index.by_visible_signature.get((joker_count, signature), ())

    return tuple(
        template
        for template in index.templates_by_card_count.get(total_count, ())
        if all(signature.count(rank) <= template[1].count(rank) for rank in set(signature))
    )


def registered_prime_templates_for_hand(
    values,
    ranks,
    joker_count: int = 0,
    max_cards: int = 9,
) -> tuple[tuple[int, tuple[int, ...]], ...]:
    """Return registered templates matching a hand's non-joker rank multiset.

    Calls with an identical knowledge set and rank combination reuse the cached
    lookup result.  Consumers still realize the returned template against
    concrete cards, so card IDs and suits remain outside this shared cache.
    """
    return _registered_prime_templates_for_hand(
        tuple(sorted(set(values))),
        tuple(sorted(ranks)),
        joker_count,
        max_cards,
    )


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


def parse_registered_composite_expression(expression: str) -> tuple[RegisteredCompositeExpressionToken, ...]:
    tokens: list[RegisteredCompositeExpressionToken] = []
    current = []

    def flush_current() -> None:
        if not current:
            return
        text = "".join(current)
        ranks = tokenize_registered_prime_pattern(text)
        tokens.append(RegisteredCompositeExpressionToken(
            kind="cards",
            ranks=ranks,
            text=text,
        ))
        current.clear()

    for char in expression.strip().lower():
        if char.isspace():
            continue
        if char in "*^":
            flush_current()
            tokens.append(RegisteredCompositeExpressionToken(kind="op", op=char, text=char))
            continue
        if TOKEN_RE.fullmatch(char):
            current.append(char)
            continue
        raise ValueError("invalid expression token")

    flush_current()
    if not tokens:
        raise ValueError("empty expression")
    if tokens[0].kind != "cards" or tokens[-1].kind != "cards":
        raise ValueError("expression must start and end with cards")
    previous = None
    for token in tokens:
        if previous == token.kind:
            raise ValueError("invalid expression order")
        previous = token.kind
    return tuple(tokens)


def generate_composite_expression_entries(
    value: int,
    pattern: str,
    source_line: int,
) -> tuple[RegisteredCompositeEntry, ...]:
    factorization = prime_factorization_for_composite_expression(value)
    if not factorization:
        return ()

    grouped_options = [
        grouped_prime_power_terms(prime, exponent)
        for prime, exponent in factorization
    ]
    expressions = set()
    for grouped in product(*grouped_options):
        terms = [term for group in grouped for term in group]
        terms.sort(key=lambda term: (-pow(term[0], term[1]), -term[0], -term[1]))
        variants = [term_expression_variants(base, exponent) for base, exponent in terms]
        if not variants or any(not option for option in variants):
            continue
        for parts in product(*variants):
            expressions.add("*".join(parts))

    entries = []
    for expression in sorted(expressions, key=lambda text: (text.count("*"), len(text), text)):
        try:
            expression_tokens = parse_registered_composite_expression(expression)
        except ValueError:
            continue
        if not composite_expression_rank_counts_allowed(expression_tokens):
            continue
        entries.append(RegisteredCompositeEntry(
            source_line=source_line,
            pattern=pattern,
            value=value,
            expression=expression,
            expression_tokens=expression_tokens,
        ))
    return tuple(entries)


def composite_expression_rank_counts_allowed(
    expression_tokens: tuple[RegisteredCompositeExpressionToken, ...],
) -> bool:
    counts: dict[int, int] = {}
    for token in expression_tokens:
        if token.kind != "cards":
            continue
        for rank in token.ranks:
            counts[rank] = counts.get(rank, 0) + 1
            if counts[rank] > MAX_COMPOSITE_AUTO_RANK_COPIES:
                return False
    return True


def prime_factorization_for_composite_expression(value: int) -> tuple[tuple[int, int], ...]:
    remaining = value
    factors = []
    divisor = 2
    while divisor * divisor <= remaining:
        if divisor > MAX_COMPOSITE_AUTO_FACTOR_TRIAL:
            return ()
        if remaining % divisor:
            divisor = 3 if divisor == 2 else divisor + 2
            continue
        exponent = 0
        while remaining % divisor == 0:
            remaining //= divisor
            exponent += 1
        if not is_probable_prime_for_registration(divisor):
            return ()
        factors.append((divisor, exponent))
        divisor = 3 if divisor == 2 else divisor + 2
    if remaining > 1:
        if not is_probable_prime_for_registration(remaining):
            return ()
        factors.append((remaining, 1))
    return tuple(factors)


def grouped_prime_power_terms(prime: int, exponent: int) -> tuple[tuple[tuple[int, int], ...], ...]:
    return tuple(
        tuple((prime, part) for part in partition)
        for partition in integer_partitions(exponent)
        if all(part <= MAX_COMPOSITE_AUTO_EXPONENT for part in partition)
    )


def integer_partitions(n: int, max_part: int | None = None) -> tuple[tuple[int, ...], ...]:
    if n == 0:
        return ((),)
    if max_part is None or max_part > n:
        max_part = n
    out = []
    for first in range(max_part, 0, -1):
        for rest in integer_partitions(n - first, first):
            out.append((first,) + rest)
    return tuple(out)


def term_expression_variants(base: int, exponent: int) -> tuple[str, ...]:
    base_texts = tuple(
        registered_cards_label(cards)
        for cards in registered_value_encodings(base)
    )
    if exponent == 1:
        return base_texts
    exponent_texts = tuple(
        registered_cards_label(cards)
        for cards in registered_value_encodings(exponent)
    )
    return tuple(
        f"{base_text}^{exponent_text}"
        for base_text in base_texts
        for exponent_text in exponent_texts
    )


def parse_registered_composite_text(text: str) -> RegisteredCompositeParseResult:
    if len(text) > MAX_REGISTERED_PRIME_TEXT_LENGTH:
        return RegisteredCompositeParseResult(
            entries=(),
            composite_values=(),
            errors=(RegisteredPrimeError(0, "", "input too long"),),
            duplicate_count=0,
            truncated=True,
        )

    errors: list[RegisteredPrimeError] = []
    entries: list[RegisteredCompositeEntry] = []
    seen_values: set[int] = set()
    duplicate_count = 0

    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        left = stripped.split("=", 1)[0].split("|", 1)[0].strip()
        token = left.split()[0] if left.split() else ""
        if not token:
            continue
        if len(token) > MAX_REGISTERED_PRIME_DIGITS:
            errors.append(RegisteredPrimeError(line_number, token, "token too long"))
            continue
        try:
            value = registered_number_pattern_value(token)
        except ValueError:
            errors.append(RegisteredPrimeError(line_number, token, "invalid token"))
            continue
        if value < 4 or is_probable_prime_for_registration(value):
            errors.append(RegisteredPrimeError(line_number, token, "not composite"))
            continue
        if value in seen_values:
            duplicate_count += 1
        seen_values.add(value)

        explicit_entry_added = False
        if "=" in stripped:
            expression = stripped.split("=", 1)[1].split("|", 1)[0].strip()
            if expression:
                try:
                    expression_tokens = parse_registered_composite_expression(expression)
                except ValueError:
                    errors.append(RegisteredPrimeError(line_number, expression, "invalid expression"))
                else:
                    entries.append(RegisteredCompositeEntry(
                        source_line=line_number,
                        pattern=token,
                        value=value,
                        expression=expression,
                        expression_tokens=expression_tokens,
                    ))
                    explicit_entry_added = True
        if not explicit_entry_added:
            entries.extend(generate_composite_expression_entries(value, token, line_number))

        if len(seen_values) > MAX_REGISTERED_PRIMES:
            errors.append(RegisteredPrimeError(line_number, token, "too many composites"))
            return RegisteredCompositeParseResult(
                entries=tuple(entries),
                composite_values=tuple(sorted(seen_values)),
                errors=tuple(errors),
                duplicate_count=duplicate_count,
                truncated=True,
            )

    return RegisteredCompositeParseResult(
        entries=tuple(entries),
        composite_values=tuple(sorted(seen_values)),
        errors=tuple(errors),
        duplicate_count=duplicate_count,
    )
