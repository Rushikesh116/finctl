"""The proposer boundary: prompts out, structured proposals in, everything cached and counted.

**Read this before trusting any LLM number in this project.**

The provider is **Google Gemini** (`google-genai`, model `gemini-3.7-flash`), swapped from
Anthropic for API access rather than capability — see D-0025. The swap touched this one class,
which is the verifier boundary working as designed rather than a lucky refactor.

**No call in this repository has ever reached any model API.** There was no `ANTHROPIC_API_KEY`
before the swap and there is no `GEMINI_API_KEY`, `GOOGLE_API_KEY`, ADC file or `gcloud` after
it, so the swap did not achieve its stated purpose in this environment. The live path is written
against the installed SDK with every parameter name verified against the package, and it has
**still never been executed against a real model**. Every fixture in `fixtures/llm/` was
produced by `OfflineProposer` below and is tagged `"source": "offline_stub"`; the harness detects
that tag and prints a warning in the metrics block. Any run reporting LLM figures is reporting
the stub.

What that does and does not invalidate, precisely:

* **Unaffected** — the regex promotion machinery. Validation, negative-example rejection,
  caching, persistence and deterministic re-extraction are all real code doing real work, and
  the falling call curve is a genuine property of that machinery. Only the *author* of a
  candidate regex is stubbed.
* **Unaffected** — the verifier boundary. A proposal is arithmetic-checked whatever produced it,
  so "a hallucinated match cannot enter the ledger" holds by construction rather than by trust.
* **Unverified** — whether a real model would propose usable regexes at a useful rate, and what
  it would actually cost. Those are the two things the stub cannot tell us, and swapping the
  provider did not change that.

Three modes, chosen explicitly rather than by accident:

| mode | when | cache miss |
|---|---|---|
| `live` | key present, `DEMO_MODE=0` | real call, response cached |
| `replay` | `DEMO_MODE=1` | **hard error** — never falls through to the network |
| `offline` | no key, `DEMO_MODE=0` | stub proposes, response cached and tagged |

`replay` failing loudly is invariant 4: a run that silently reached the network would not be the
run whose audit log was recorded.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

__all__ = [
    "DEFAULT_MODEL",
    "PROVIDER",
    "USD_PER_MTOK_IN",
    "USD_PER_MTOK_OUT",
    "CallBudgetExceeded",
    "CacheMiss",
    "ExceptionExplanation",
    "NarrationParse",
    "Proposer",
    "ProposerStats",
    "build_proposer",
]

PROVIDER = "google-gemini"
DEFAULT_MODEL = "gemini-3.7-flash"

# USD per million tokens, verified 2026-08-26 from
# https://ai.google.dev/gemini-api/docs/pricing
# Flash tier, and the tier is the right shape for JSON extraction from a short string.
# NOTE the documented step change: "$0.75 through December 31, 2026. $1.50 starting January 1,
# 2027" for input, and $3.75 -> $7.50 for output. These constants are the 2026 rates; any cost
# figure produced after that date is understated and must be re-read from the pricing page.
USD_PER_MTOK_IN = 0.75
USD_PER_MTOK_OUT = 3.75

Mode = Literal["live", "replay", "offline"]

STUB_SOURCE = "offline_stub"


class CallBudgetExceeded(RuntimeError):
    """The per-run call ceiling was hit. Fails the run rather than degrading it quietly."""


class CacheMiss(RuntimeError):
    """A replay run needed a response that is not on disk."""


# --- structured output schemas -------------------------------------------------------------


class NarrationParse(BaseModel):
    """A proposal about one bank narration. Every field is a claim to be checked, not a result.

    `reference` is checked against the set of known settlement UTRs before use, and `regex`
    against positive *and* negative examples before caching. So an injected instruction in the
    narration cannot do better than produce a proposal that fails validation.
    """

    reference: str | None = Field(
        default=None, description="the settlement reference found in the narration, or null"
    )
    regex: str | None = Field(
        default=None,
        description="a Python regex with exactly one capture group that extracts it, or null",
    )
    confidence: int = Field(ge=0, le=100)
    reasoning: str = Field(default="", max_length=2000)


class ExceptionExplanation(BaseModel):
    """A drafted explanation for one exception. Presentation only — it moves no money."""

    summary: str = Field(max_length=400)
    suggested_resolution: str = Field(max_length=400)
    confidence: int = Field(ge=0, le=100)


SCHEMAS: dict[str, type[BaseModel]] = {
    "narration_parse": NarrationParse,
    "exception_explanation": ExceptionExplanation,
}


@dataclass
class ProposerStats:
    calls: int = 0
    cache_hits: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    stub_responses: int = 0
    modes_used: set[str] = field(default_factory=set)
    provider: str = "-"
    # The version the provider reported serving. Recorded per run because "which model produced
    # this" is provenance, not trivia, and an alias can move under you between runs.
    model_versions: set[str] = field(default_factory=set)
    # Split by schema, because the two kinds of call behave completely differently: narration
    # parses fall to zero as regexes are promoted, while explanation drafts are a fixed cost per
    # distinct exception type and fall only via the fixture cache. One combined number hides the
    # thing the regex cache actually does.
    calls_by_schema: dict[str, int] = field(default_factory=dict)

    @property
    def cost_micros_usd(self) -> int:
        """Integer micro-USD. Money is never a float, and cost is money."""
        micros = (
            self.input_tokens * USD_PER_MTOK_IN + self.output_tokens * USD_PER_MTOK_OUT
        ) / 1_000_000 * 1_000_000
        return int(micros)

    @property
    def is_stubbed(self) -> bool:
        return self.stub_responses > 0


# --- prompt hashing -------------------------------------------------------------------------


def prompt_key(*, schema: str, model: str, system: str, user: str) -> str:
    """The fixture key. Covers everything that could change a response.

    `sort_keys` and a fixed separator so the digest cannot depend on dict ordering — the same
    reason the audit ledger hashes canonical JSON.
    """
    payload = json.dumps(
        {"schema": schema, "model": model, "system": system, "user": user},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --- proposers ------------------------------------------------------------------------------


class OfflineProposer:
    """Stands in for the model when there is no credential. **Not a model.**

    For narration parsing it applies a deliberately simple heuristic — find the longest
    alphanumeric token that looks like a reference, then anchor a regex on the literal text
    either side of it. That is a plausible thing a model might propose, which is all a test
    double needs to be: everything downstream (validation, negative-example rejection, promotion,
    deterministic re-extraction) is real code and is exercised for real.

    It is not trying to be good. If it proposes something unusable, promotion rejects it, which
    is the path worth testing anyway.
    """

    name = STUB_SOURCE

    def propose(self, *, schema: str, system: str, user: str) -> dict[str, Any]:
        if schema == "narration_parse":
            return self._parse_narration(user)
        if schema == "exception_explanation":
            return self._explain(user)
        raise ValueError(f"offline proposer has no handler for schema {schema!r}")

    @staticmethod
    def _parse_narration(user: str) -> dict[str, Any]:
        narration = user.rsplit("NARRATION:", 1)[-1].strip().splitlines()[0].strip()
        tokens = re.findall(r"[A-Za-z0-9]{8,40}", narration)
        # A reference mixes letters and digits; pure words like SETTLEMENT do not.
        candidates = [
            t for t in tokens if re.search(r"\d", t) and re.search(r"[A-Za-z]", t)
        ]
        if not candidates:
            return {
                "reference": None,
                "regex": None,
                "confidence": 90,
                "reasoning": "no token in this narration looks like a settlement reference",
            }
        reference = max(candidates, key=len)
        start = narration.index(reference)
        prefix, suffix = narration[:start], narration[start + len(reference) :]
        pattern = (
            re.escape(prefix[-12:]) + r"([A-Za-z0-9]{8,40})" + re.escape(suffix[:4])
        )
        return {
            "reference": reference,
            "regex": pattern,
            "confidence": 75,
            "reasoning": f"anchored on the literal text surrounding {reference!r}",
        }

    @staticmethod
    def _explain(user: str) -> dict[str, Any]:
        kind = "this exception"
        for line in user.splitlines():
            if line.startswith("TYPE:"):
                kind = line.removeprefix("TYPE:").strip()
                break
        return {
            "summary": f"Could not match: {kind.lower().replace('_', ' ')}.",
            "suggested_resolution": "Review the evidence recorded on this exception and decide manually.",
            "confidence": 50,
        }


class GeminiProposer:
    """The live path. Written against `google-genai` 2.20.0, never executed against the API.

    Every parameter name here was verified against the installed package rather than taken from
    a documentation page — the structured-output docs describe a second surface
    (`client.interactions.create`, `response_format`, `output_text`) which also exists in 2.20.0,
    and mixing the two would fail at runtime in a way no test here could catch.

    No `temperature` is set. It is available on this provider, unlike the previous one, but
    determinism has never come from it: the fixture cache is the mechanism (D-0004), and adding
    a sampling parameter would imply a guarantee the cache already provides more strongly.
    """

    name = PROVIDER

    def __init__(self, model: str = DEFAULT_MODEL) -> None:
        from google import genai  # imported lazily so this module loads with no credential

        # Reads GEMINI_API_KEY or GOOGLE_API_KEY from the environment.
        self._client = genai.Client()
        self._model = model

    def propose(self, *, schema: str, system: str, user: str) -> dict[str, Any]:
        from google.genai import types

        model_type = SCHEMAS[schema]
        response = self._client.models.generate_content(
            model=self._model,
            contents=user,
            config=types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                # The annotation accepts a `type`, so the existing Pydantic models go through
                # unchanged. Same models the verifier and promotion gate already validate
                # against, so the swap cannot alter what counts as a well-formed proposal.
                response_schema=model_type,
            ),
        )

        parsed = response.parsed
        payload = parsed.model_dump() if parsed is not None else {}
        usage = response.usage_metadata
        payload["_usage"] = {
            "input_tokens": getattr(usage, "prompt_token_count", 0) or 0,
            "output_tokens": getattr(usage, "candidates_token_count", 0) or 0,
        }
        # The version that actually served the request, not the string that was asked for. If a
        # provider silently routes an alias to a new build, this is what records it.
        payload["_model_version"] = response.model_version or self._model
        return payload


class Proposer:
    """Caching wrapper: budget, cost accounting, and the fixture cache."""

    def __init__(
        self,
        *,
        mode: Mode,
        inner: Any | None,
        fixture_dir: Path,
        call_budget: int,
        model: str = DEFAULT_MODEL,
    ) -> None:
        self.mode = mode
        self.model = model
        self._inner = inner
        self._fixture_dir = fixture_dir
        self._call_budget = call_budget
        self.stats = ProposerStats()
        self.stats.modes_used.add(mode)

    def _fixture_path(self, key: str) -> Path:
        return self._fixture_dir / f"{key}.json"

    def propose(self, schema: str, *, system: str, user: str) -> BaseModel:
        key = prompt_key(schema=schema, model=self.model, system=system, user=user)
        path = self._fixture_path(key)

        if path.exists():
            record = json.loads(path.read_text(encoding="utf-8"))
            self.stats.cache_hits += 1
            self.stats.provider = record.get("provider", record.get("source", "-"))
            self.stats.model_versions.add(record.get("model_version", record.get("model", "-")))
            if record.get("source") == STUB_SOURCE:
                self.stats.stub_responses += 1
            return SCHEMAS[schema].model_validate(record["response"])

        if self.mode == "replay":
            raise CacheMiss(
                f"replay run needs fixture {key[:12]} for schema {schema!r} and it is not on "
                "disk. A replay never falls through to the network, because a run that "
                "silently did so would not be the run whose audit log was recorded."
            )
        if self._inner is None:  # pragma: no cover - defensive
            raise RuntimeError("no proposer available and no fixture on disk")
        if self.stats.calls >= self._call_budget:
            raise CallBudgetExceeded(
                f"per-run call budget of {self._call_budget} reached. Failing the run rather "
                "than continuing with partial adjudication, because a run that quietly stopped "
                "asking would report a coverage number it did not earn."
            )

        payload = self._inner.propose(schema=schema, system=system, user=user)
        usage = payload.pop("_usage", {"input_tokens": 0, "output_tokens": 0})
        served = payload.pop("_model_version", self.model)
        self.stats.model_versions.add(served)
        self.stats.provider = self._inner.name
        self.stats.calls += 1
        self.stats.calls_by_schema[schema] = (
            self.stats.calls_by_schema.get(schema, 0) + 1
        )
        self.stats.input_tokens += int(usage.get("input_tokens", 0))
        self.stats.output_tokens += int(usage.get("output_tokens", 0))
        if self._inner.name == STUB_SOURCE:
            self.stats.stub_responses += 1

        validated = SCHEMAS[schema].model_validate(payload)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema": schema,
                    "model": self.model,
                    "model_version": served,
                    "provider": self._inner.name,
                    "source": self._inner.name,
                    "usage": usage,
                    "response": validated.model_dump(),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return validated


def build_proposer(
    *,
    fixture_dir: Path | None = None,
    call_budget: int | None = None,
    model: str | None = None,
) -> Proposer:
    """Pick a mode from the environment, explicitly. Never guesses its way onto the network."""
    demo = os.environ.get("DEMO_MODE", "0") == "1"
    has_key = bool(
        os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    )
    resolved_model = model or os.environ.get("FINCTL_LLM_MODEL", DEFAULT_MODEL)
    directory = fixture_dir or Path(
        os.environ.get("FINCTL_LLM_FIXTURE_DIR", "fixtures/llm")
    )
    budget = call_budget or int(os.environ.get("FINCTL_LLM_CALL_BUDGET", "25"))

    if demo:
        return Proposer(
            mode="replay", inner=None, fixture_dir=directory, call_budget=0, model=resolved_model
        )
    if has_key:
        return Proposer(
            mode="live",
            inner=GeminiProposer(resolved_model),
            fixture_dir=directory,
            call_budget=budget,
            model=resolved_model,
        )
    return Proposer(
        mode="offline",
        inner=OfflineProposer(),
        fixture_dir=directory,
        call_budget=budget,
        model=resolved_model,
    )
