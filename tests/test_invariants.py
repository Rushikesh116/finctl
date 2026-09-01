"""Structural guards for the two invariants that code review reliably misses.

Invariant 1 — money is integer paise: ``float`` must not appear in a ``core/money.py``
signature, nor be called anywhere in that module.

Invariant 2 — the matcher must never read ground truth: nothing under ``core/`` may
import the generator, the scenario config, or the harness.

These run from Phase 0 so a violation fails the commit that introduces it rather than
surfacing at review time, or worse, as a suspiciously good metric. See docs/ENGINEERING_RULES.md.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CORE_DIR = REPO_ROOT / "core"
MONEY_MODULE = CORE_DIR / "money.py"

# Top-level packages that hold, or can reach, ground-truth labels.
FORBIDDEN_ROOTS_IN_CORE = frozenset({"data", "eval"})

_FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)


def _core_modules() -> list[Path]:
    return sorted(p for p in CORE_DIR.rglob("*.py") if p.name != "__init__.py")


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _annotations_of(func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.expr]:
    """Every annotation expression in a signature: params, *args, **kwargs, return."""
    args = func.args
    found = [
        arg.annotation
        for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs)
        if arg.annotation is not None
    ]
    for variadic in (args.vararg, args.kwarg):
        if variadic is not None and variadic.annotation is not None:
            found.append(variadic.annotation)
    if func.returns is not None:
        found.append(func.returns)
    return found


def _mentions_float(expression: ast.expr) -> bool:
    """True if `float` appears anywhere in the expression, including inside generics."""
    for node in ast.walk(expression):
        if isinstance(node, ast.Name) and node.id == "float":
            return True
        if isinstance(node, ast.Attribute) and node.attr == "float":
            return True
        # String annotations, e.g. `def f(x: "float") -> None`.
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            try:
                inner = ast.parse(node.value, mode="eval").body
            except SyntaxError:
                continue
            if inner is not expression and _mentions_float(inner):
                return True
    return False


def test_no_float_in_money_signatures() -> None:
    """Invariant 1: money is integer paise, so no signature in money.py may touch float."""
    if not MONEY_MODULE.exists():
        pytest.skip("core/money.py lands in Phase 1 — see docs/PROGRESS.md")

    offenders = [
        f"core/money.py:{node.lineno} {node.name}()"
        for node in ast.walk(_parse(MONEY_MODULE))
        if isinstance(node, _FUNCTION_NODES)
        and any(_mentions_float(a) for a in _annotations_of(node))
    ]

    assert not offenders, (
        "`float` appears in a core/money.py signature. Money is integer paise end to "
        "end; formatting to rupees happens only in the presentation layer.\n  "
        + "\n  ".join(offenders)
    )


def test_money_module_never_calls_float() -> None:
    """Stricter companion to the above: a float() round trip in the body is just as fatal.

    The brief only mandates the signature check. A `float()` call inside a helper would
    pass that check and still lose paise, so the module is held to the stronger rule.
    """
    if not MONEY_MODULE.exists():
        pytest.skip("core/money.py lands in Phase 1 — see docs/PROGRESS.md")

    offenders = [
        f"core/money.py:{node.lineno}"
        for node in ast.walk(_parse(MONEY_MODULE))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"float", "round"}
    ]

    assert not offenders, (
        "core/money.py calls float() or round(). Use integer arithmetic; rounding "
        "direction is defined in docs/skills/money-invariants/SKILL.md.\n  "
        + "\n  ".join(offenders)
    )


def test_money_module_uses_exceptions_not_asserts() -> None:
    """A money guard must not be strippable.

    `python -O` removes every assert statement, so an `assert` guard silently vanishes under
    an optimisation flag — leaving the code path that was supposed to be impossible wide
    open, in production, raising nothing. Money guards `raise`.
    """
    if not MONEY_MODULE.exists():
        pytest.skip("core/money.py lands in Phase 1 — see docs/PROGRESS.md")

    offenders = [
        f"core/money.py:{node.lineno}"
        for node in ast.walk(_parse(MONEY_MODULE))
        if isinstance(node, ast.Assert)
    ]

    assert not offenders, (
        "core/money.py uses `assert` as a guard. `python -O` strips asserts; raise a "
        "ValueError instead so the guard survives.\n  " + "\n  ".join(offenders)
    )


def test_core_never_imports_ground_truth() -> None:
    """Invariant 2: ground-truth leakage is the likeliest way to fake a good result.

    The dependency arrow points one way only: ``data`` and ``eval`` import record
    schemas from ``core``; ``core`` imports neither. Passing today with an empty
    ``core/`` is expected — the guard exists so it keeps passing later.
    """
    violations: list[str] = []

    for path in _core_modules():
        for node in ast.walk(_parse(path)):
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                # A relative import (level > 0) cannot reach a sibling top-level package.
                imported = [node.module] if node.level == 0 and node.module else []
            else:
                continue

            for name in imported:
                if name.split(".")[0] in FORBIDDEN_ROOTS_IN_CORE:
                    rel = path.relative_to(REPO_ROOT)
                    violations.append(f"{rel}:{node.lineno} imports {name}")

    assert not violations, (
        "A module under core/ imports a package that can reach ground-truth labels. "
        "The matcher must not be able to see the answers.\n  " + "\n  ".join(violations)
    )
