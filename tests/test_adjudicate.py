"""Tests for Layer 4 — narration parsing, the exception split, and the drafted explanation.

The properties worth having are that a bad rule cannot get cached, that an injected reference
cannot be used, and that a replay never quietly reaches the network. All three are tested by
doing the thing rather than by reading the code.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core import llm
from core.rules_cache import (
    NEGATIVE_EXAMPLES,
    SEEDED_RULES,
    PromotionRejected,
    RulesCache,
)

DEV = "dev_seed_11"


# --- the rules cache -------------------------------------------------------------------------


def test_the_seeded_rule_handles_the_shape_it_ships_for() -> None:
    cache = RulesCache()
    hit = cache.extract("NEFT-RAZORPAYSOFTWARE-UTR1688304862ounii5-STL")
    assert hit is not None
    assert hit[0] == "1688304862ounii5"


def test_a_valid_regex_is_promoted_and_then_used() -> None:
    """Promotion is the whole mechanism: after it, the shape costs no call."""
    cache = RulesCache()
    narration = "IMPS/1888481283mjoasu/RAZORPAY SOFTWARE"
    assert cache.extract(narration) is None, "precondition: the seeded rule misses this shape"

    cache.promote(
        r"IMPS/([A-Za-z0-9]{8,40})/RAZ",
        example=narration,
        expected="1888481283mjoasu",
        name="promoted_test",
    )
    hit = cache.extract(narration)
    assert hit is not None and hit[0] == "1888481283mjoasu"


def test_an_overbroad_regex_is_refused() -> None:
    """The check that matters. `(.+)` passes "matches the example" and would then attach a
    wrong reference to every unparsed credit forever."""
    cache = RulesCache()
    # The example and expected value are a plausible reference, so the plausibility and
    # capture checks both pass and the NEGATIVE-example check is what has to catch this.
    with pytest.raises(PromotionRejected, match="no reference"):
        cache.promote(
            r"(.+)", example="1234567890abcdef", expected="1234567890abcdef", name="bad"
        )


@pytest.mark.parametrize("negative", NEGATIVE_EXAMPLES)
def test_no_promoted_regex_may_match_a_reference_free_narration(negative: str) -> None:
    cache = RulesCache()
    with pytest.raises(PromotionRejected):
        cache.promote(
            r"([A-Za-z0-9 /]{8,40})", example=negative, expected=negative[:20], name="bad"
        )


def test_a_regex_without_exactly_one_group_is_refused() -> None:
    cache = RulesCache()
    for pattern in (r"UTR[A-Za-z0-9]{8,}", r"UTR([A-Za-z0-9]{4,})([A-Za-z0-9]{4,})"):
        with pytest.raises(PromotionRejected, match="capture group"):
            cache.promote(pattern, example="UTR12345678ab", expected="12345678ab", name="bad")


def test_a_regex_that_captures_the_wrong_span_is_refused() -> None:
    cache = RulesCache()
    with pytest.raises(PromotionRejected, match="captures"):
        cache.promote(
            r"(NEFT)-RAZORPAYSOFTWARE-UTR[A-Za-z0-9]+",
            example="NEFT-RAZORPAYSOFTWARE-UTR1688304862ounii5-STL",
            expected="1688304862ounii5",
            name="bad",
        )


def test_the_cache_round_trips_and_preserves_order(tmp_path: Path) -> None:
    """Order is preserved so extraction resolves through the same rule on every run."""
    cache = RulesCache()
    cache.promote(
        r"IMPS/([A-Za-z0-9]{8,40})/RAZ",
        example="IMPS/1888481283mjoasu/RAZORPAY SOFTWARE",
        expected="1888481283mjoasu",
        name="promoted_1",
    )
    path = tmp_path / "rules.json"
    cache.save(path)

    reloaded = RulesCache.load(path)
    assert [r.pattern for r in reloaded.rules] == [r.pattern for r in cache.rules]
    assert len(reloaded.promoted) == 1


def test_an_absent_cache_file_starts_from_the_seeded_rules(tmp_path: Path) -> None:
    cache = RulesCache.load(tmp_path / "nothing.json")
    assert len(cache) == len(SEEDED_RULES)


# --- the proposer boundary --------------------------------------------------------------------


def test_replay_mode_fails_loudly_on_a_cache_miss(tmp_path: Path) -> None:
    """Invariant 4. A replay that reached the network would not be the run that was recorded."""
    proposer = llm.Proposer(
        mode="replay", inner=None, fixture_dir=tmp_path, call_budget=0
    )
    with pytest.raises(llm.CacheMiss, match="never falls through to the network"):
        proposer.propose("narration_parse", system="s", user="NARRATION: anything")


def test_the_call_budget_fails_the_run_rather_than_degrading_it(tmp_path: Path) -> None:
    proposer = llm.Proposer(
        mode="offline", inner=llm.OfflineProposer(), fixture_dir=tmp_path, call_budget=1
    )
    proposer.propose("narration_parse", system="s", user="NARRATION: IMPS/1234567890ab/X")
    with pytest.raises(llm.CallBudgetExceeded, match="rather than continuing"):
        proposer.propose("narration_parse", system="s", user="NARRATION: RTGS REF 9876543210cd Y")


def test_a_cached_response_is_not_recounted_as_a_call(tmp_path: Path) -> None:
    proposer = llm.Proposer(
        mode="offline", inner=llm.OfflineProposer(), fixture_dir=tmp_path, call_budget=5
    )
    args = {"system": "s", "user": "NARRATION: IMPS/1234567890ab/X"}
    proposer.propose("narration_parse", **args)
    proposer.propose("narration_parse", **args)

    assert proposer.stats.calls == 1
    assert proposer.stats.cache_hits == 1


def test_every_stub_fixture_is_tagged_as_a_stub(tmp_path: Path) -> None:
    """No number may be presentable as model output when a stub produced it."""
    proposer = llm.Proposer(
        mode="offline", inner=llm.OfflineProposer(), fixture_dir=tmp_path, call_budget=5
    )
    proposer.propose("narration_parse", system="s", user="NARRATION: IMPS/1234567890ab/X")

    written = list(tmp_path.glob("*.json"))
    assert written
    for path in written:
        assert json.loads(path.read_text())["source"] == llm.STUB_SOURCE
    assert proposer.stats.is_stubbed


def test_the_prompt_key_covers_everything_that_could_change_a_response() -> None:
    base = {"schema": "narration_parse", "model": "m", "system": "s", "user": "u"}
    key = llm.prompt_key(**base)
    for field in base:
        altered = dict(base) | {field: base[field] + "x"}
        assert llm.prompt_key(**altered) != key, f"{field} does not affect the fixture key"


def test_a_narration_with_no_reference_yields_no_regex() -> None:
    """The stub must not invent a rule for text containing nothing to extract."""
    result = llm.OfflineProposer().propose(
        schema="narration_parse", system="s", user="NARRATION: NEFT CR-RAZORPAY SOFTWARE-SETTLEMENT"
    )
    assert result["reference"] is None
    assert result["regex"] is None


# --- against the real datasets -----------------------------------------------------------------

pytestmark = pytest.mark.skipif(
    not __import__("data.generator", fromlist=["dataset_paths"])
    .dataset_paths(DEV)["gateway"]
    .exists(),
    reason="run `make seed` first — data/generated/ is gitignored",
)


@pytest.mark.parametrize("dataset_name", [DEV, "holdout_seed_97"])
def test_the_split_produces_both_types(dataset_name: str) -> None:
    """Item 2: MISSING_BANK_ROW and UNPARSEABLE_NARRATION must actually separate.

    Layer 1 cannot tell them apart — both look identical to an exact join — so if only one type
    ever appears, the split is nominal.
    """
    from eval import harness

    metrics = harness.evaluate(dataset_name, max_layer=4)
    assert metrics.by_type.get("UNPARSEABLE_NARRATION", 0) > 0, (
        f"{dataset_name}: no UNPARSEABLE_NARRATION, so the split is not being exercised"
    )
    assert metrics.by_type.get("MISSING_BANK_ROW", 0) > 0, (
        f"{dataset_name}: no MISSING_BANK_ROW left, so the split has swallowed the other side"
    )


@pytest.mark.parametrize("dataset_name", [DEV, "holdout_seed_97"])
def test_layer_4_introduces_no_false_matches(dataset_name: str) -> None:
    """Every recovered reference is checked against real settlement UTRs and every resulting
    group re-verified, so a parse error should not be able to become a match."""
    from eval import harness

    before = harness.evaluate(dataset_name, max_layer=3)
    after = harness.evaluate(dataset_name, max_layer=4)

    assert after.auto_matched >= before.auto_matched
    assert after.false_matches <= before.false_matches, (
        f"{dataset_name}: false matches rose {before.false_matches} -> {after.false_matches} "
        "when Layer 4 was enabled. Report both numbers as a trade; do not absorb it."
    )


def test_promoted_rules_survive_across_runs() -> None:
    """The falling curve depends on this: the cache has to persist to disk."""
    from eval import harness

    first = harness.evaluate(DEV, max_layer=4)
    second = harness.evaluate(DEV, max_layer=4)
    assert second.rules_promoted >= first.rules_promoted
    assert second.rules_total >= len(SEEDED_RULES)


# --- the provider adapter ----------------------------------------------------------------------


def test_the_provider_is_gemini_and_anthropic_is_gone() -> None:
    """D-0025. One adapter replaced another; no multi-provider abstraction was introduced."""
    assert llm.PROVIDER == "google-gemini"
    assert llm.DEFAULT_MODEL.startswith("gemini-")
    assert hasattr(llm, "GeminiProposer")
    assert not hasattr(llm, "AnthropicProposer")


def test_the_live_path_is_selected_only_by_a_google_key(monkeypatch) -> None:
    monkeypatch.delenv("DEMO_MODE", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    assert llm.build_proposer().mode == "offline"

    monkeypatch.setenv("DEMO_MODE", "1")
    assert llm.build_proposer().mode == "replay"


def test_changing_the_model_invalidates_every_fixture() -> None:
    """The prompt key covers the model, so a provider swap cannot silently replay another
    model's answers as if they were this one's."""
    args = {"schema": "narration_parse", "system": "s", "user": "u"}
    assert llm.prompt_key(model="claude-opus-5", **args) != llm.prompt_key(
        model="gemini-3.7-flash", **args
    )


def test_provider_and_model_version_reach_the_ledger() -> None:
    """"Which model produced this" is provenance, so it is hashed into the chain."""
    from audit.ledger import AuditLedger

    ledger = AuditLedger()
    entry = ledger.record(
        layer=4,
        decision="raise_exception",
        record_ids=["a"],
        outcome="UNPARSEABLE_NARRATION",
        confidence=100,
        provider="google-gemini",
        model="gemini-3.7-flash",
    )
    assert entry.provider == "google-gemini"
    assert "google-gemini" in entry.payload()["provider"]


def test_a_model_without_a_provider_is_refused() -> None:
    """Two providers can ship similarly-named models, so the model string alone is not enough."""
    from audit.ledger import AuditLedger

    with pytest.raises(ValueError, match="without a provider"):
        AuditLedger().record(
            layer=4,
            decision="d",
            record_ids=["a"],
            outcome="o",
            confidence=50,
            model="gemini-3.7-flash",
        )


# --- what the negative-example gate would do to a real model's proposals -----------------------
#
# The stub proposes regexes anchored on the literal text either side of the reference, and those
# pass the gate trivially: a literal anchor like `IMPS/` cannot match a reference-free narration.
# So the observed 0% rejection rate says almost nothing about the gate, because the gate exists
# for LOOSER proposals than the stub is capable of making.
#
# These are patterns a model plausibly returns for the same narrations. They are the closest
# thing available to an answer, and they are not a substitute for one.

IMPS_EXAMPLE = "IMPS/1888481283mjoasu/RAZORPAY SOFTWARE"

REALISTIC_PROPOSALS = [
    # Tightly anchored on BOTH sides - what the stub happens to produce, and what passes.
    (r"IMPS/([A-Za-z0-9]{8,40})/RAZ", IMPS_EXAMPLE, "1888481283mjoasu", True),
    (r"REF ([A-Za-z0-9]{8,40}) RAZ", "RTGS CR REF 1552002271luumnm RAZORPAY", "1552002271luumnm", True),
    # Anchored on one side only. This is the natural, obvious proposal for this narration -- and
    # the gate REJECTS it, because `IMPS/SETTLEMENT/CR` also matches and yields "SETTLEMENT".
    # Discovered by this test failing against an expectation of True.
    (r"IMPS/([A-Za-z0-9]{8,40})/", IMPS_EXAMPLE, "1888481283mjoasu", False),
    # "Just grab a long token" answers.
    (r"([A-Za-z0-9]{12,})", IMPS_EXAMPLE, "1888481283mjoasu", True),
    (r"([A-Za-z0-9]{8,})", IMPS_EXAMPLE, "1888481283mjoasu", False),
    # Catastrophic - matches anything.
    (r"(\S+)", "1888481283mjoasu", "1888481283mjoasu", False),
    (r"(.+)", "1888481283mjoasu", "1888481283mjoasu", False),
]


@pytest.mark.parametrize(("pattern", "example", "expected", "should_pass"), REALISTIC_PROPOSALS)
def test_the_gate_on_patterns_a_model_might_plausibly_propose(
    pattern: str, example: str, expected: str, should_pass: bool
) -> None:
    """Documents where the gate's line actually falls, since no model has been asked.

    The informative row is `IMPS/([A-Za-z0-9]{8,40})/` -- anchored on the left, which is the
    obvious thing to propose for this narration. The gate rejects it, because
    `IMPS/SETTLEMENT/CR` matches too and yields "SETTLEMENT". This row was written expecting
    True and the test failed; the expectation was wrong and the gate was right.

    That matters for reading the promotion numbers. The stub survives the gate only because it
    anchors on BOTH sides -- it happens to append the `RAZ` suffix. A model proposing the more
    natural one-sided anchor would be rejected, so the observed 0-rejections figure is a
    property of the stub's conservatism, not evidence that proposals generally pass.
    """
    cache = RulesCache()
    if should_pass:
        cache.promote(pattern, example=example, expected=expected, name="candidate")
        assert len(cache.promoted) == 1
    else:
        with pytest.raises(PromotionRejected):
            cache.promote(pattern, example=example, expected=expected, name="candidate")


# --- the two reporting-honesty rules -----------------------------------------------------------


def test_cold_calls_per_100_is_never_estimated() -> None:
    """A cold rate is only knowable from a run that made every call fresh.

    Anything else would be an estimate, and eval-protocol §1 forbids a number no command
    produced. `not measured` is the correct output, not 0.00.
    """
    from eval import harness

    replayed = harness.Metrics(
        dataset="x", provenance=harness.capture(DEV), n=500, llm_calls=0, llm_cache_hits=6
    )
    text = harness._cold_calls_per_100(replayed)
    assert "not measured" in text
    assert "0.00" not in text, "a replayed run reported a cold rate"
    assert "quota" in text, "the reason the cold run did not complete must be stated"

    cold = harness.Metrics(
        dataset="x", provenance=harness.capture(DEV), n=500, llm_calls=8, llm_cache_hits=0
    )
    assert "1.60" in harness._cold_calls_per_100(cold), "a genuinely cold run must report its rate"


def test_the_replay_banner_explains_where_the_models_contribution_went() -> None:
    """The banner counts this run's responses, which understates a promoted model's work.

    Once a proposal is promoted the narration resolves via the cached regex and the fixture is
    never read again, so the model vanishes from the response counts while its rule keeps
    working. The note has to sit next to the banner, not further down the block.
    """
    from eval import harness

    metrics = harness.evaluate(DEV)
    if not (metrics.llm_stubbed and harness._rules_authored_by_a_model(metrics)):
        pytest.skip("no real-model-authored rule alongside a stubbed run to disambiguate")

    lines = harness.render(metrics).splitlines()
    banner = next(i for i, line in enumerate(lines) if "Adjudicator:" in line)
    note = next((i for i, line in enumerate(lines) if "counts THIS RUN" in line), None)

    assert note is not None, "the banner is unexplained on the replay path"
    assert note == banner + 1, (
        f"the note is {note - banner} lines from the banner it explains; a clarification that "
        "far away is not a clarification"
    )
    assert "rules cache" in lines[note]
