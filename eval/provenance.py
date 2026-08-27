"""Run provenance — what a metrics row was actually measured against.

A metrics row records a number. Without provenance it does not record *what the number is
about*, and a row from before a dataset was regenerated is then indistinguishable from one
after. Since `docs/METRICS.md` is append-only and rows are compared across phases, that
ambiguity would quietly invalidate every comparison in the file.

So every row carries three identifiers:

* **git SHA** — which code produced it
* **dataset SHA** — which *data* it ran against
* **timestamp** — when

The dataset SHA is a digest over that dataset's emitted files. It is computed **from the
files on disk at eval time**, not read out of the committed manifest, and then cross-checked
against the manifest. That ordering matters: if the two disagree, the manifest is describing
something other than what was measured, and reading the manifest would report the wrong
provenance with total confidence. Divergence is surfaced as `drift`, not smoothed over.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

REPO_ROOT = Path(__file__).resolve().parents[1]
GENERATED_DIR = REPO_ROOT / "data" / "generated"
MANIFEST_PATH = REPO_ROOT / "data" / "DATASET_HASHES.txt"

# Short enough to sit beside a git short SHA in a fixed-width block, long enough that a
# collision across this project's lifetime is not a concern. The full digest is always
# recoverable from DATASET_HASHES.txt.
SHORT_LEN = 8

ManifestState = Literal["match", "drift", "absent"]

UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class RunProvenance:
    """Everything needed to know what a metrics row is about."""

    dataset: str
    dataset_sha: str
    git_sha: str
    started_at_utc: str
    manifest_state: ManifestState
    manifest_sha: str | None = None

    @property
    def is_trustworthy(self) -> bool:
        """False when the row cannot be tied to committed, reproducible data."""
        return self.manifest_state == "match" and self.dataset_sha != UNKNOWN

    def header_fragment(self) -> str:
        """The `Dataset:` portion of the metrics block header line.

        Drift is spelled out inline rather than footnoted. A reader scanning the block must
        not have to know to go looking for it.
        """
        text = f"Dataset: {self.dataset}  data {self.dataset_sha}"
        if self.manifest_state == "drift":
            text += f"  !! DRIFT: manifest says {self.manifest_sha}"
        elif self.manifest_state == "absent":
            text += "  !! NO MANIFEST: run `make seed`"
        return text


def dataset_files(name: str, *, generated_dir: Path = GENERATED_DIR) -> list[Path]:
    """That dataset's emitted files, in a stable order.

    Sorted explicitly: `glob` order is filesystem-dependent, and a digest built over an
    unstable order would change between machines while the data stayed identical.
    """
    return sorted(generated_dir.glob(f"{name}_*"))


def _digest_lines(lines: list[str]) -> str:
    joined = "\n".join(sorted(lines)) + "\n"
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:SHORT_LEN]


def dataset_sha(name: str, *, generated_dir: Path = GENERATED_DIR) -> str:
    """A short digest over the on-disk files of one dataset.

    Per-dataset rather than one digest over the whole manifest: a metrics row names a single
    dataset, so regenerating the holdout must not invalidate the provenance of every dev row.

    Returns `"unknown"` when the dataset is not on disk, which is a fact worth printing
    rather than an error worth raising — the harness can still report its numbers, clearly
    marked as untraceable.
    """
    files = dataset_files(name, generated_dir=generated_dir)
    if not files:
        return UNKNOWN

    return _digest_lines(
        [
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}"
            for path in files
        ]
    )


def manifest_dataset_sha(name: str, *, manifest: Path = MANIFEST_PATH) -> str | None:
    """The same digest, recomputed from the committed manifest's recorded hashes.

    `None` when the manifest is absent or names none of this dataset's files.
    """
    if not manifest.exists():
        return None

    lines = []
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if "  " not in line:
            continue
        digest, relative = line.split("  ", 1)
        filename = relative.rsplit("/", 1)[-1]
        if filename.startswith(f"{name}_"):
            lines.append(f"{digest}  {filename}")

    return _digest_lines(lines) if lines else None


def git_sha(*, short: bool = True, root: Path = REPO_ROOT) -> str:
    """The current commit, or `"unknown"` outside a repository.

    Checks `FINCTL_GIT_SHA` first. A container image deliberately does not carry `.git`, so
    without that the deployed artefact could not report which code produced its numbers — the
    same provenance gap the dataset SHA closes, reopened on the code axis. The Dockerfile bakes
    it in as a build argument.

    Never raises: a harness must still print its numbers from a tarball with no `.git`, as long
    as it says so rather than inventing a SHA.
    """
    baked = os.environ.get("FINCTL_GIT_SHA", "").strip()
    if baked:
        return baked[:7] if short else baked

    command = ["git", "rev-parse", "--short" if short else "HEAD", "HEAD"]
    if not short:
        command = ["git", "rev-parse", "HEAD"]
    try:
        result = subprocess.run(
            command, cwd=root, capture_output=True, text=True, check=True, timeout=10
        )
    except (subprocess.SubprocessError, OSError):
        return UNKNOWN
    return result.stdout.strip() or UNKNOWN


def capture(
    dataset: str,
    *,
    generated_dir: Path = GENERATED_DIR,
    manifest: Path = MANIFEST_PATH,
    root: Path = REPO_ROOT,
) -> RunProvenance:
    """Snapshot provenance for one run. Call this once, before the run starts."""
    on_disk = dataset_sha(dataset, generated_dir=generated_dir)
    recorded = manifest_dataset_sha(dataset, manifest=manifest)

    if recorded is None:
        state: ManifestState = "absent"
    elif recorded == on_disk:
        state = "match"
    else:
        state = "drift"

    return RunProvenance(
        dataset=dataset,
        dataset_sha=on_disk,
        git_sha=git_sha(root=root),
        started_at_utc=datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
        manifest_state=state,
        manifest_sha=recorded,
    )
