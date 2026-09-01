"""The Phase 0 gate, as a test.

Every file listed here is load-bearing for a future session or a judge: the memory
files that let a fresh context resume without re-deriving anything, the three local
skills, and the entry points `make demo` walks. If one goes missing, this fails rather
than the next session quietly re-inventing a fact that was already verified.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

MEMORY_FILES = [
    "docs/ENGINEERING_RULES.md",
    "README.md",
    "docs/SPEC.md",
    "docs/DECISIONS.md",
    "docs/PROGRESS.md",
    "docs/OPEN_QUESTIONS.md",
    "docs/METRICS.md",
    "docs/WHAT_BROKE.md",
]

SKILL_FILES = [
    "docs/skills/razorpay-domain/SKILL.md",
    "docs/skills/money-invariants/SKILL.md",
    "docs/skills/eval-protocol/SKILL.md",
]

PROJECT_FILES = [
    "Makefile",
    "requirements.txt",
    "requirements.lock.txt",
    ".gitignore",
    ".env.example",
    "pytest.ini",
    ".githooks/pre-commit",
    "scripts/check_secrets.py",
]

MAKE_TARGETS = ["setup", "seed", "run", "eval", "report", "serve", "test", "demo"]


@pytest.mark.parametrize("relative_path", MEMORY_FILES + SKILL_FILES + PROJECT_FILES)
def test_required_file_exists_and_is_not_empty(relative_path: str) -> None:
    path = REPO_ROOT / relative_path
    assert path.is_file(), f"{relative_path} is missing"
    assert path.stat().st_size > 0, f"{relative_path} is empty"


@pytest.mark.parametrize("skill_path", SKILL_FILES)
def test_skill_has_yaml_frontmatter(skill_path: str) -> None:
    """A skill without `name` and `description` frontmatter will not be discovered."""
    lines = (REPO_ROOT / skill_path).read_text(encoding="utf-8").splitlines()
    assert lines and lines[0].strip() == "---", f"{skill_path} has no frontmatter opener"

    closing = next((i for i, line in enumerate(lines[1:], start=1) if line.strip() == "---"), None)
    assert closing is not None, f"{skill_path} has an unterminated frontmatter block"

    keys = {line.split(":", 1)[0].strip() for line in lines[1:closing] if ":" in line}
    assert {"name", "description"} <= keys, f"{skill_path} frontmatter needs name and description"


@pytest.mark.parametrize("target", MAKE_TARGETS)
def test_makefile_declares_target(target: str) -> None:
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    assert f"\n{target}:" in makefile, f"Makefile has no `{target}` target"


def test_engineering_rules_records_every_make_target() -> None:
    """docs/ENGINEERING_RULES.md is the contract for future sessions; an undocumented target rots it."""
    engineering_rules = (REPO_ROOT / "docs/ENGINEERING_RULES.md").read_text(encoding="utf-8")
    undocumented = [t for t in MAKE_TARGETS if f"`make {t}`" not in engineering_rules]
    assert not undocumented, f"docs/ENGINEERING_RULES.md does not document: {', '.join(undocumented)}"
