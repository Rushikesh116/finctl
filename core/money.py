"""Integer money arithmetic — the only place in FinCtl where money math is allowed to live.

Money is an `int` count of minor units, end to end: paise for INR, sen for JPY, fils for
KWD. Never a float. Never a Decimal-to-float round trip. Parsing, arithmetic, storage and
comparison all happen in integers; rendering to a rupee string happens at the presentation
edge only, and the string never flows back into a computation.

Two rules this module is held to, both enforced by tests rather than by discipline:

* **No `float`, no `round()`.** `tests/test_invariants.py` parses this file and fails on
  either, in a signature or in a body.
* **Guards `raise`; they are never `assert`.** `python -O` strips assert statements, so an
  assert guard silently disappears under an optimisation flag — leaving the branch that was
  supposed to be impossible wide open, in production, raising nothing.

Rounding is half-up (D-0005). The rule that catches people: **any aggregate of a rounded
per-row quantity must be a sum of the stored per-row values**, never a recomputation from
the aggregated base — half-up does not distribute over addition. See
`docs/skills/money-invariants/SKILL.md`.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import TypeAlias

__all__ = [
    "INR_SCALE",
    "MINOR_UNIT_SCALE",
    "Paise",
    "convert_to_paise",
    "format_rupees",
    "minor_unit_scale",
    "parse_minor_units",
    "parse_rupees",
    "pct_half_up",
    "split_with_remainder",
]

Paise: TypeAlias = int
"""An amount as an integer count of minor units.

A plain alias rather than a `NewType`, deliberately: no type checker is in the pinned stack,
so a `NewType` would advertise enforcement that does not exist. The alias documents intent
in signatures; the real enforcement is the AST tests in `tests/test_invariants.py`.
"""

INR_SCALE = 100

MINOR_UNIT_SCALE: dict[str, int] = {
    # Two-decimal currencies (the overwhelming majority).
    "INR": 100,
    "USD": 100,
    "EUR": 100,
    "GBP": 100,
    "AED": 100,
    "SGD": 100,
    "AUD": 100,
    "CAD": 100,
    # Zero-decimal. The gateway docs document JPY passing `295` for ¥295.
    "JPY": 1,
    "KRW": 1,
    # Three-decimal. The gateway docs document these passing `295990` for 295.990.
    "KWD": 1000,
    "BHD": 1000,
    "OMR": 1000,
}
"""Minor units per major unit, by ISO 4217 code.

Scales are grounded in the gateway's own documented examples (zero-decimal JPY,
three-decimal currencies); the specific currency list beyond those is conventional. Any
currency absent here raises rather than defaulting to 100 — a silent default is how a JPY
amount becomes 100× wrong.
"""

# Anything that is decoration rather than digits: currency symbols, codes, whitespace,
# thousands separators. Comma placement is not validated because bank exports disagree
# about it (Indian 4,83,271.44 vs Western 483,271.44) and the grouping carries no meaning.
_DECORATION = re.compile(r"[\s,  ]|₹|Rs\.?|INR|USD|EUR|GBP|JPY|\$|€|£|¥", re.IGNORECASE)
_SIGNED_DECIMAL = re.compile(r"^(?P<sign>[-+]?)(?P<whole>\d+)(?:\.(?P<frac>\d*))?$")


def minor_unit_scale(currency: str) -> int:
    """Minor units per major unit for `currency`, e.g. 100 for INR.

    Raises on an unknown currency rather than assuming 100: defaulting would make a JPY
    amount 100x wrong and a KWD amount 10x wrong, silently.
    """
    code = currency.strip().upper()
    try:
        return MINOR_UNIT_SCALE[code]
    except KeyError:
        raise ValueError(
            f"unknown currency {currency!r}: add its minor-unit scale to MINOR_UNIT_SCALE "
            "rather than letting it default, or the amount will be silently mis-scaled"
        ) from None


def _decimal_places(scale: int) -> int:
    """How many fractional digits `scale` admits. 100 -> 2, 1 -> 0, 1000 -> 3."""
    if scale < 1:
        raise ValueError(f"minor unit scale must be at least 1, got {scale}")
    places, remaining = 0, scale
    while remaining > 1:
        if remaining % 10 != 0:
            raise ValueError(f"minor unit scale must be a power of ten, got {scale}")
        remaining //= 10
        places += 1
    return places


def parse_minor_units(text: str, *, scale: int = INR_SCALE) -> Paise:
    """Parse a decimal money string into integer minor units, without touching a float.

    Accepts a leading sign, a currency symbol or code, and any thousands grouping:
    `"4,83,271.44"`, `"483271.44"`, `"₹4,83,271.44"` and `"Rs 483,271.44"` all give
    48327144 at scale 100.

    More fractional digits than the scale admits is an error, not something to truncate.
    Sub-paise precision in an input means the upstream feed disagrees with the money model,
    and silently dropping it is how a reconciliation goes quietly wrong.
    """
    places = _decimal_places(scale)
    cleaned = _DECORATION.sub("", text)
    if not cleaned:
        raise ValueError(f"no numeric content in {text!r}")

    match = _SIGNED_DECIMAL.match(cleaned)
    if match is None:
        raise ValueError(f"cannot parse {text!r} as a decimal amount (cleaned: {cleaned!r})")

    fraction = match.group("frac") or ""
    if len(fraction) > places:
        raise ValueError(
            f"{text!r} carries {len(fraction)} fractional digits but this currency admits "
            f"{places}; sub-unit precision must be resolved upstream, not truncated here"
        )

    magnitude = int(match.group("whole")) * scale + int(fraction.ljust(places, "0") or "0")
    return -magnitude if match.group("sign") == "-" else magnitude


def parse_rupees(text: str) -> Paise:
    """Parse an INR amount into integer paise. `"4,83,271.44"` -> `48327144`."""
    return parse_minor_units(text, scale=INR_SCALE)


def _group_indian(digits: str) -> str:
    """Indian digit grouping: last three, then pairs. `"483271"` -> `"4,83,271"`."""
    if len(digits) <= 3:
        return digits
    head, tail = digits[:-3], digits[-3:]
    groups: list[str] = []
    while len(head) > 2:
        groups.append(head[-2:])
        head = head[:-2]
    if head:
        groups.append(head)
    return ",".join(reversed(groups)) + "," + tail


def format_rupees(paise: Paise, *, prefix: str = "") -> str:
    """Render integer paise as a rupee string with Indian grouping and two decimals.

    `48327144` -> `"4,83,271.44"`. This is the presentation edge: the result is for a human
    or a report, and it never flows back into a computation.

    Negative values are rendered with a leading `-` because a settlement delta can be
    negative even though stored amounts never are (D-0008).
    """
    if not isinstance(paise, int) or isinstance(paise, bool):
        raise TypeError(f"format_rupees takes integer paise, got {type(paise).__name__}")

    sign = "-" if paise < 0 else ""
    magnitude = -paise if paise < 0 else paise
    whole, fraction = divmod(magnitude, INR_SCALE)
    return f"{sign}{prefix}{_group_indian(str(whole))}.{fraction:02d}"


def pct_half_up(base_paise: Paise, numerator: int, denominator: int) -> Paise:
    """`base * numerator / denominator`, rounded half-up, never leaving the integers.

    GST on a fee is `pct_half_up(fee_base_paise, 18, 100)`.

    The base must be non-negative: floor division skews negatives away from half-up, so the
    sign belongs at the call site where its meaning is known. Raises rather than asserts, so
    the guard survives `python -O`.
    """
    if base_paise < 0:
        raise ValueError(
            f"pct_half_up needs a non-negative base, got {base_paise}; apply the sign at "
            "the call site so floor division cannot skew the rounding"
        )
    if denominator <= 0:
        raise ValueError(f"pct_half_up needs a positive denominator, got {denominator}")
    if numerator < 0:
        raise ValueError(f"pct_half_up needs a non-negative numerator, got {numerator}")
    return (base_paise * numerator + denominator // 2) // denominator


def split_with_remainder(total_paise: Paise, weights: Sequence[int]) -> list[Paise]:
    """Split `total_paise` across `weights` so that no paisa is lost or invented.

    Largest-remainder method: floor every part, then hand the leftover paise out one each to
    the parts with the largest true remainder, ties broken by lowest index so the result is
    deterministic rather than dependent on iteration order.

    Guarantees `sum(result) == total_paise` exactly, for every input. That post-condition is
    the entire purpose of the function, and it is covered by a property test over random
    inputs — three hand-picked cases would pass a floor-only implementation that quietly
    drops the remainder.
    """
    if total_paise < 0:
        raise ValueError(
            f"split_with_remainder needs a non-negative total, got {total_paise}; apply the "
            "sign at the call site"
        )
    if not weights:
        raise ValueError("split_with_remainder needs at least one weight")
    if any(weight < 0 for weight in weights):
        raise ValueError(f"weights must be non-negative, got {list(weights)}")

    total_weight = sum(weights)
    if total_weight == 0:
        raise ValueError(
            "weights sum to zero, so there is no defined way to distribute the total"
        )

    scaled = [total_paise * weight for weight in weights]
    parts = [value // total_weight for value in scaled]
    remainders = [value % total_weight for value in scaled]

    leftover = total_paise - sum(parts)
    # Largest remainder first; index ascending breaks ties, keeping the split deterministic.
    order = sorted(range(len(weights)), key=lambda i: (-remainders[i], i))
    for index in order[:leftover]:
        parts[index] += 1

    return parts


def convert_to_paise(amount_minor: int, *, scale: int, fx_rate_micros: int) -> Paise:
    """Convert a foreign amount to INR paise using an integer rate, rounded half-up.

    `fx_rate_micros` is INR per one major unit of the source currency, times 10**6 — an
    integer, so the rate itself never introduces float drift.

        value_in_paise = amount_minor * fx_rate_micros / (scale * 10**4)

    USD 10.00 at 83.5 INR/USD is `convert_to_paise(1000, scale=100,
    fx_rate_micros=83_500_000)` -> `83500` paise, i.e. Rs 835.00.

    The result is **stored**, never re-derived later. Re-deriving a conversion on each read
    is how an FX line drifts (SPEC §3.6).
    """
    if amount_minor < 0:
        raise ValueError(f"convert_to_paise needs a non-negative amount, got {amount_minor}")
    if fx_rate_micros <= 0:
        raise ValueError(f"fx_rate_micros must be positive, got {fx_rate_micros}")
    _decimal_places(scale)  # Rejects a non-power-of-ten scale before it corrupts the maths.
    return pct_half_up(amount_minor * fx_rate_micros, 1, scale * 10_000)
