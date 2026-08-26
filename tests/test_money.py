"""Behavioural tests for `core/money.py`.

Separate from `test_invariants.py`, which holds the *structural* guards — those test what the
module may not contain, these test what its arithmetic does.

Bare `assert` is used freely here and that is not a contradiction of the no-assert rule for
`core/money.py`: pytest rewrites test asserts for better failure output, and test runs are
never under `python -O`. The rule is about production guards, which must survive `-O`.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MONEY_MODULE = REPO_ROOT / "core" / "money.py"

pytestmark = pytest.mark.skipif(
    not MONEY_MODULE.exists(),
    reason="core/money.py lands in Phase 1 — see docs/PROGRESS.md",
)

if MONEY_MODULE.exists():
    # Imported at module scope, not guarded by try/except: if money.py exists but is broken,
    # collection should fail loudly rather than skip and look green.
    from core.money import (
        convert_to_paise,
        format_rupees,
        minor_unit_scale,
        parse_minor_units,
        parse_rupees,
        pct_half_up,
        split_with_remainder,
    )


GST_NUMERATOR, GST_DENOMINATOR = 18, 100


def test_gst_is_summed_not_recomputed() -> None:
    """Half-up rounding does not distribute over addition, so GST must be summed per row.

    Two rows with `fee_base = 25` paise each: per-row GST is 5p, summing to 10p. Recomputing
    from the batch total of 50p gives 9p. That 1p gap is the whole reason `Σ gst_on_fee` is
    defined as a sum of stored values — a batch-level "verify the fee" check written the
    other way fails to balance while looking exactly like a rounding bug in the *data*.

    Pathology 6 puts half-paisa fees in both datasets so this stays live, not theoretical.
    """
    per_row = pct_half_up(25, GST_NUMERATOR, GST_DENOMINATOR)
    assert per_row == 5, "(25*18 + 50) // 100 == 5"

    summed = per_row + per_row
    recomputed = pct_half_up(50, GST_NUMERATOR, GST_DENOMINATOR)

    assert summed == 10, "summing stored per-row GST is the correct answer"
    assert recomputed == 9, "(50*18 + 50) // 100 == 9 — recomputation loses a paisa"
    assert summed != recomputed, (
        "the divergence this test exists to pin has disappeared. If pct_half_up changed, "
        "re-derive from scratch whether the settlement identity still requires Sigma "
        "gst_on_fee to be a sum of stored values before relaxing anything."
    )


def test_gst_summation_holds_across_a_realistic_batch() -> None:
    """The same divergence, at batch scale, so the failure mode is visible not incidental."""
    fee_bases = [25, 25, 25, 25, 75, 125, 3, 7]

    summed = sum(pct_half_up(f, GST_NUMERATOR, GST_DENOMINATOR) for f in fee_bases)
    recomputed = pct_half_up(sum(fee_bases), GST_NUMERATOR, GST_DENOMINATOR)

    assert summed != recomputed, (
        f"expected a rounding divergence over {fee_bases}; got {summed} both ways. "
        "Pick fee bases whose GST lands on a half-paisa (see SPEC pathology 6)."
    )


def test_pct_half_up_rejects_a_negative_base_by_raising() -> None:
    """Not by asserting: `python -O` would strip that and let the skew through silently."""
    with pytest.raises(ValueError, match="non-negative"):
        pct_half_up(-1, GST_NUMERATOR, GST_DENOMINATOR)


def test_pct_half_up_rejects_a_non_positive_denominator() -> None:
    with pytest.raises(ValueError, match="positive denominator"):
        pct_half_up(100, GST_NUMERATOR, 0)


def test_pct_half_up_rounds_half_away_from_zero() -> None:
    """Exactly .5 rounds up, never to-even. Half-up is the documented convention (D-0005)."""
    # 50 * 1/2 = 25 exactly, no rounding needed.
    assert pct_half_up(50, 1, 2) == 25
    # 25 * 1/2 = 12.5 -> 13 under half-up; banker's rounding would give 12.
    assert pct_half_up(25, 1, 2) == 13
    # 75 * 1/2 = 37.5 -> 38 under half-up; banker's rounding would also give 38, so the
    # 25-case above is the one that actually discriminates between the two conventions.
    assert pct_half_up(75, 1, 2) == 38


def test_split_with_remainder_conserves_every_paisa() -> None:
    """Property test: paise are neither lost nor invented, for any total and any weights.

    Three hand-picked cases would pass with a floor-only implementation. Random inputs are
    what actually catch the lost remainder.
    """
    rng = random.Random(11)  # Seeded: a failure must be reproducible.

    for _ in range(2000):
        total = rng.randint(0, 10_000_000)
        weights = [rng.randint(1, 1000) for _ in range(rng.randint(1, 12))]

        parts = split_with_remainder(total, weights)

        assert len(parts) == len(weights), "one part per weight"
        assert all(p >= 0 for p in parts), f"negative part in {parts}"
        assert sum(parts) == total, (
            f"split lost or invented paise: total={total} weights={weights} "
            f"sum(parts)={sum(parts)}"
        )


def test_split_with_remainder_is_deterministic() -> None:
    """Same inputs, same output — ties broken by lowest index, not by set iteration order."""
    total, weights = 100, [1, 1, 1]
    first = split_with_remainder(total, weights)

    assert first == split_with_remainder(total, weights)
    assert sum(first) == total
    # 100 across three equal weights: floors are 33 each (99), one paisa left over, which
    # goes to the lowest index.
    assert first == [34, 33, 33], f"expected largest-remainder with index tie-break, got {first}"


def test_split_with_remainder_rejects_zero_total_weight() -> None:
    with pytest.raises(ValueError):
        split_with_remainder(100, [0, 0])


# --- parsing and formatting -------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["4,83,271.44", "483271.44", "₹4,83,271.44", "Rs 4,83,271.44", "Rs. 483,271.44"],
)
def test_parse_rupees_accepts_the_grouping_variants_banks_actually_emit(text: str) -> None:
    """The brief's own headline figure, however a feed chooses to punctuate it.

    Comma placement is not validated: Indian grouping (4,83,271.44) and Western
    (483,271.44) both appear in real exports and the grouping carries no meaning.
    """
    assert parse_rupees(text) == 48327144


def test_rupee_round_trip_is_lossless_and_uses_indian_grouping() -> None:
    paise = parse_rupees("4,83,271.44")
    assert format_rupees(paise) == "4,83,271.44"
    assert format_rupees(paise, prefix="Rs ") == "Rs 4,83,271.44"


@pytest.mark.parametrize(
    ("paise", "expected"),
    [
        (0, "0.00"),
        (1, "0.01"),
        (10000, "100.00"),
        (100000, "1,000.00"),
        (18422000, "1,84,220.00"),
        (48327144, "4,83,271.44"),
        (123456789, "12,34,567.89"),
        (-1250, "-12.50"),
    ],
)
def test_format_rupees_groups_and_signs_correctly(paise: int, expected: str) -> None:
    """Negative values render because a settlement delta can be negative even though
    stored amounts never are (D-0008)."""
    assert format_rupees(paise) == expected


def test_parse_rupees_refuses_sub_paise_precision() -> None:
    """Truncating a third decimal would silently disagree with the upstream feed."""
    with pytest.raises(ValueError, match="fractional digits"):
        parse_rupees("1.234")


def test_parse_rupees_rejects_junk() -> None:
    for text in ["", "   ", "abc", "1.2.3", "1,2,3.4.5"]:
        with pytest.raises(ValueError):
            parse_rupees(text)


# --- non-INR currencies ----------------------------------------------------------------


def test_documented_minor_unit_scales() -> None:
    """The gateway documents JPY passing 295 for 295 yen and three-decimal currencies
    passing 295990 — so 'paise' is shorthand for 'integer minor units' (SPEC §3.6)."""
    assert minor_unit_scale("INR") == 100
    assert minor_unit_scale("JPY") == 1
    assert minor_unit_scale("KWD") == 1000
    assert minor_unit_scale("inr") == 100, "currency codes are case-insensitive"

    assert parse_minor_units("295", scale=1) == 295
    assert parse_minor_units("295.990", scale=1000) == 295990


def test_unknown_currency_raises_rather_than_defaulting_to_100() -> None:
    """Defaulting would make a zero-decimal amount 100x wrong, silently."""
    with pytest.raises(ValueError, match="unknown currency"):
        minor_unit_scale("XYZ")


def test_zero_decimal_currency_rejects_a_fraction() -> None:
    with pytest.raises(ValueError, match="fractional digits"):
        parse_minor_units("1.5", scale=1)


def test_convert_to_paise_uses_an_integer_rate() -> None:
    """USD 10.00 at 83.5 INR/USD is Rs 835.00, with the rate carried as an integer."""
    assert convert_to_paise(1000, scale=100, fx_rate_micros=83_500_000) == 83500
    assert format_rupees(83500) == "835.00"

    # A zero-decimal source currency: JPY 295 at 0.56 INR/JPY.
    assert convert_to_paise(295, scale=1, fx_rate_micros=560_000) == 16520


def test_convert_to_paise_rejects_a_non_power_of_ten_scale() -> None:
    with pytest.raises(ValueError, match="power of ten"):
        convert_to_paise(100, scale=60, fx_rate_micros=1_000_000)
