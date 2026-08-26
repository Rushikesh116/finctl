#!/usr/bin/env python3
"""Refuse any commit whose staged diff contains a key-shaped string.

Invoked by ``.githooks/pre-commit``, which ``make setup`` wires in via
``core.hooksPath``. Runs on the staged diff only, so it costs nothing on a large tree.

Deliberately narrow: it matches known vendor key shapes rather than "looks like
entropy". A scanner that cries wolf gets disabled, and a disabled scanner protects
nothing. Escape hatch for a genuine false positive is the allowlist marker below.
"""

from __future__ import annotations

import re
import subprocess
import sys

ALLOWLIST_MARKER = "pragma: allow-secret"

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("Anthropic API key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}")),
    ("Razorpay live key id", re.compile(r"rzp_live_[A-Za-z0-9]{8,}")),
    ("Razorpay test key id", re.compile(r"rzp_test_[A-Za-z0-9]{8,}")),
    ("Razorpay key secret", re.compile(r"(?i)key[_-]?secret\s*[:=]\s*[\"'][A-Za-z0-9]{16,}[\"']")),
    ("AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Private key block", re.compile(r"BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY")),
    ("Bearer token literal", re.compile(r"(?i)authorization\s*[:=]\s*[\"']bearer\s+[A-Za-z0-9._\-]{20,}")),
]


def staged_additions() -> list[tuple[str, int, str]]:
    """Every added line in the staged diff, as (path, line number, text)."""
    diff = subprocess.run(
        ["git", "diff", "--cached", "--unified=0", "--no-color"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    additions: list[tuple[str, int, str]] = []
    path, lineno = "<unknown>", 0
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            path = line[len("+++ b/") :]
        elif line.startswith("@@"):
            match = re.search(r"\+(\d+)", line)
            lineno = int(match.group(1)) if match else 0
        elif line.startswith("+"):
            additions.append((path, lineno, line[1:]))
            lineno += 1
    return additions


def scan(additions: list[tuple[str, int, str]]) -> list[tuple[str, int, str]]:
    findings: list[tuple[str, int, str]] = []
    for path, lineno, text in additions:
        if ALLOWLIST_MARKER in text:
            continue
        for label, pattern in PATTERNS:
            if pattern.search(text):
                findings.append((path, lineno, label))
                break
    return findings


def main() -> int:
    findings = scan(staged_additions())
    if not findings:
        return 0

    print("Commit blocked: the staged diff contains key-shaped strings.", file=sys.stderr)
    print(file=sys.stderr)
    for path, lineno, label in findings:
        print(f"  {path}:{lineno}  {label}", file=sys.stderr)
    print(file=sys.stderr)
    print("Move the value into .env (which is gitignored) and reference it from", file=sys.stderr)
    print(f"os.environ. If this is a false positive, append '{ALLOWLIST_MARKER}'", file=sys.stderr)
    print("to the offending line.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
