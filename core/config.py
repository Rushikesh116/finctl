"""Load `.env` into the process environment.

**This existed as a gap for five phases.** `.env.example` documented every variable, `.gitignore`
excluded `.env`, and `scripts/check_secrets.py` told anyone who tripped it to "move the value into
.env and reference it from os.environ" — while **nothing in the codebase ever read `.env`**. Every
lookup was a bare `os.environ.get`, so a key sitting in `.env` was invisible and the failure mode
was silent: no error, just `mode=offline` and a stub proposing instead of a model.

Stdlib only. `python-dotenv` would do this properly and is one import away, but it is a new
dependency for fifteen lines of parsing, and the pinned stack is not worth widening for that.

**Real environment variables always win.** A value already in `os.environ` is never overwritten,
so an explicit `DEMO_MODE=0 make eval` overrides the file, and a deployed environment's injected
secrets are never shadowed by a stale file left in the image.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["DOTENV_PATH", "load_dotenv"]

DOTENV_PATH = Path(__file__).resolve().parents[1] / ".env"

_loaded = False


def load_dotenv(path: Path | None = None, *, override: bool = False) -> int:
    """Read `.env` into `os.environ`. Returns how many names it set.

    Idempotent: safe to call from every entry point, which is what makes it reliable — a loader
    that has to be called in exactly one place is a loader that will be missed.

    Never logs a value. Names only, if anything.
    """
    global _loaded
    target = path or DOTENV_PATH
    if path is None and _loaded:
        return 0
    if not target.exists():
        if path is None:
            _loaded = True
        return 0

    applied = 0
    for raw in target.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if not name:
            continue
        # Strip one layer of matching quotes, the way a shell would.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if not value:
            # An empty assignment means "deliberately unset" in .env.example — for instance
            # FINCTL_USD_INR, which must stay unset so the harness prints `Rs TBD` rather than a
            # figure derived from a guessed rate. Setting it to "" would defeat that.
            continue
        if not override and name in os.environ:
            continue
        os.environ[name] = value
        applied += 1

    if path is None:
        _loaded = True
    return applied
