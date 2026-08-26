"""Promoted narration regexes — the LLM writes rules, and the rules do the work.

Layer 4's first job is parsing bank narration. Regex first; the model is asked only about a
shape no regex handles. When it proposes a regex for that shape, the regex is **validated and
then cached**, so the shape is free from then on. The call count falls as the cache fills, and
that curve is the point: the model is an author of rules, not a participant in every run.

**Promotion is validated, not trusted.** A proposed regex must:

1. compile, and expose exactly one capture group;
2. match the example it was proposed for, and capture *exactly* the expected reference;
3. match **none** of the negative examples — narrations known to carry no reference at all.

Rule 3 is the one that matters. Without it a proposal of `(.+)` would sail through rules 1 and
2, then match every narration forever and silently attach a wrong reference to every unparsed
credit. A cache that accepts a rule on the strength of one positive example is a cache that can
be poisoned by one bad suggestion — and the narration it was derived from is untrusted input.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "NEGATIVE_EXAMPLES",
    "SEEDED_RULES",
    "PromotionRejected",
    "RulesCache",
]

# The one shape FinCtl ships knowing. Everything else has to be learned and promoted, which is
# what makes the falling call curve observable rather than asserted.
SEEDED_RULES: tuple[tuple[str, str], ...] = (
    ("seed_neft_utr", r"UTR([A-Za-z0-9]{8,})"),
)

# Narrations that carry no reference. Any candidate regex matching one of these is refused: a
# rule that fires on a reference-free narration will invent references indefinitely.
NEGATIVE_EXAMPLES: tuple[str, ...] = (
    "NEFT CR-RAZORPAY SOFTWARE-SETTLEMENT",
    "IMPS/SETTLEMENT/CR",
    "RTGS CREDIT",
    "BY TRANSFER-SETTLEMENT",
    "NEFT INWARD RETURN",
)

# A reference is a long alphanumeric token. Used only to sanity-check what a rule extracts, not
# to extract anything itself.
_PLAUSIBLE_REFERENCE = re.compile(r"^[A-Za-z0-9]{8,40}$")


class PromotionRejected(Exception):
    """A candidate regex failed validation and was not cached."""


@dataclass(frozen=True, slots=True)
class Rule:
    name: str
    pattern: str
    promoted_from: str | None = None

    def extract(self, narration: str) -> str | None:
        match = re.search(self.pattern, narration)
        if match is None:
            return None
        try:
            captured = match.group(1)
        except IndexError:  # pragma: no cover - validation rejects group-less patterns
            return None
        return captured or None


class RulesCache:
    """An ordered, persistable set of narration rules.

    Order is insertion order and is preserved on disk, so extraction is deterministic: the same
    narration resolves through the same rule on every run and in every process.
    """

    def __init__(self, rules: list[Rule] | None = None) -> None:
        self._rules: list[Rule] = list(rules) if rules is not None else [
            Rule(name=name, pattern=pattern) for name, pattern in SEEDED_RULES
        ]

    def __len__(self) -> int:
        return len(self._rules)

    @property
    def rules(self) -> list[Rule]:
        return list(self._rules)

    @property
    def promoted(self) -> list[Rule]:
        return [rule for rule in self._rules if rule.promoted_from is not None]

    def extract(self, narration: str) -> tuple[str, str] | None:
        """First rule that fires wins. Returns `(reference, rule_name)`, or None."""
        for rule in self._rules:
            captured = rule.extract(narration)
            if captured is not None:
                return captured, rule.name
        return None

    def validate(self, pattern: str, *, example: str, expected: str) -> None:
        """Raise `PromotionRejected` unless the pattern is safe to cache."""
        try:
            compiled = re.compile(pattern)
        except re.error as error:
            raise PromotionRejected(f"pattern does not compile: {error}") from None

        if compiled.groups != 1:
            raise PromotionRejected(
                f"pattern must expose exactly one capture group, found {compiled.groups}"
            )
        if not _PLAUSIBLE_REFERENCE.match(expected):
            raise PromotionRejected(
                f"expected reference {expected!r} is not a plausible reference token"
            )

        match = compiled.search(example)
        if match is None:
            raise PromotionRejected("pattern does not match the example it was proposed for")
        if match.group(1) != expected:
            raise PromotionRejected(
                f"pattern captures {match.group(1)!r} from the example, expected {expected!r}"
            )

        for negative in NEGATIVE_EXAMPLES:
            hit = compiled.search(negative)
            if hit is not None:
                raise PromotionRejected(
                    f"pattern also matches a narration with no reference "
                    f"({negative!r} -> {hit.group(1)!r}); a rule this broad would invent "
                    "references on every unparsed credit"
                )

    def promote(self, pattern: str, *, example: str, expected: str, name: str) -> Rule:
        """Validate and cache. Raises `PromotionRejected` if it does not pass."""
        self.validate(pattern, example=example, expected=expected)
        if any(rule.pattern == pattern for rule in self._rules):
            raise PromotionRejected("pattern is already cached")

        rule = Rule(name=name, pattern=pattern, promoted_from=example)
        self._rules.append(rule)
        return rule

    # --- persistence ---------------------------------------------------------------------

    def to_json(self) -> str:
        return json.dumps(
            [
                {"name": r.name, "pattern": r.pattern, "promoted_from": r.promoted_from}
                for r in self._rules
            ],
            indent=2,
            sort_keys=True,
        ) + "\n"

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json(), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> RulesCache:
        """Load, or start from the seeded rules if there is nothing on disk yet."""
        if not path.exists():
            return cls()
        payload = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            [
                Rule(
                    name=entry["name"],
                    pattern=entry["pattern"],
                    promoted_from=entry.get("promoted_from"),
                )
                for entry in payload
            ]
        )
