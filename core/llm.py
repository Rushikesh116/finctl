"""The proposer boundary: prompts out, structured proposals in, everything cached and counted.

**Read this before trusting any LLM number in this project.**

No call in this repository has ever reached the Anthropic API. There is no `ANTHROPIC_API_KEY`
in the environment it was built in and no `ant` credential either, so the live path is written
against the SDK, type-checked and unit-tested, and **has never been executed against a real
model**. Every fixture in `fixtures/llm/` was produced by `OfflineProposer` below and is tagged
`"source": "offline_stub"`. The harness detects that tag and prints a warning in the metrics
block. Any run reporting LLM figures is reporting the stub.

What that does and does not invalidate, precisely:

* **Unaffected** — the regex promotion machinery. Validation, negative-example rejection,
  caching, persistence and deterministic re-extraction are all real code doing real work, and
  the falling call curve is a genuine property of that machinery. Only the *author* of a
  candidate regex is stubbed.
* **Unaffected** — the verifier boundary. A proposal is arithmetic-checked whatever produced it,
  so "a hallucinated match cannot enter the ledger" holds by construction rather than by trust.
* **Unverified** — whether a real model would propose usable regexes at a useful rate, and what
  it would actually cost. Those are the two things the stub cannot tell us.

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

DEFAULT_MODEL = "claude-opus-5"

# USD per million tokens, verified 2026-08-26 from
# https://platform.claude.com/docs/en/about-claude/models/overview.md
USD_PER_MTOK_IN = 5.0
USD_PER_MTOK_OUT = 25.0

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


class AnthropicProposer:
    """The live path. Written against the installed SDK; never executed against the API.

    No `temperature`: it was removed on the current models and returns HTTP 400 (D-0004).
    Determinism comes from the fixture cache, which was always the real mechanism.
    """

    name = "anthropic"

    def __init__(self, model: str = DEFAULT_MODEL) -> None:
        import anthropic  # imported lazily so the module loads with no SDK credential

        self._client = anthropic.Anthropic()
        self._model = model

    def propose(self, *, schema: str, system: str, user: str) -> dict[str, Any]:
        model_type = SCHEMAS[schema]
        response = self._client.messages.parse(
            model=self._model,
            max_tokens=2048,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"format": model_type},
            thinking={"type": "adaptive"},
        )
        parsed = response.parsed_output
        payload = parsed.model_dump() if parsed is not None else {}
        payload["_usage"] = {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }
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
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
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
            inner=AnthropicProposer(resolved_model),
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
