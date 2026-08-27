"""Tests for run provenance — the identifiers that make a metrics row interpretable.

The interesting cases are the failure ones. A provenance function that only works when
everything is in order tells you nothing on the day it matters, which is the day the datasets
were regenerated and someone is comparing a row from before against a row from after.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eval.provenance import (
    UNKNOWN,
    _SHA_ENV_VARS,
    capture,
    dataset_files,
    dataset_sha,
    git_sha,
    manifest_dataset_sha,
)

DEV, HOLDOUT = "dev_seed_11", "holdout_seed_97"


@pytest.fixture
def fake_dataset(tmp_path: Path) -> tuple[Path, Path]:
    """A miniature generated/ plus a matching manifest, so drift can be induced on demand."""
    generated = tmp_path / "generated"
    generated.mkdir()

    contents = {
        f"{DEV}_merchant_ledger.csv": "row_id\nml_000001\n",
        f"{DEV}_bank_statement.csv": "row_id\nbk_000001\n",
        f"{HOLDOUT}_merchant_ledger.csv": "row_id\nml_000002\n",
    }
    for name, text in contents.items():
        (generated / name).write_text(text, encoding="utf-8")

    import hashlib

    manifest = tmp_path / "DATASET_HASHES.txt"
    manifest.write_text(
        "\n".join(
            f"{hashlib.sha256((generated / name).read_bytes()).hexdigest()}  "
            f"data/generated/{name}"
            for name in sorted(contents)
        )
        + "\n",
        encoding="utf-8",
    )
    return generated, manifest


def test_dataset_sha_is_stable_and_per_dataset(fake_dataset: tuple[Path, Path]) -> None:
    """Per-dataset, so regenerating the holdout does not invalidate every dev row."""
    generated, _ = fake_dataset

    first = dataset_sha(DEV, generated_dir=generated)
    assert first == dataset_sha(DEV, generated_dir=generated), "not stable across calls"
    assert first != dataset_sha(HOLDOUT, generated_dir=generated), "datasets share a digest"


def test_dataset_sha_changes_when_the_data_changes(fake_dataset: tuple[Path, Path]) -> None:
    generated, _ = fake_dataset
    before = dataset_sha(DEV, generated_dir=generated)

    (generated / f"{DEV}_bank_statement.csv").write_text("row_id\nbk_999999\n", encoding="utf-8")

    assert dataset_sha(DEV, generated_dir=generated) != before, (
        "the digest ignored a change to a dataset file, so a metrics row could not "
        "distinguish before from after"
    )


def test_dataset_sha_ignores_the_other_dataset(fake_dataset: tuple[Path, Path]) -> None:
    generated, _ = fake_dataset
    before = dataset_sha(DEV, generated_dir=generated)

    (generated / f"{HOLDOUT}_merchant_ledger.csv").write_text("changed\n", encoding="utf-8")

    assert dataset_sha(DEV, generated_dir=generated) == before, (
        "regenerating the holdout changed dev's digest, which would wrongly invalidate every "
        "dev row in the run log"
    )


def test_dataset_sha_does_not_depend_on_filesystem_order(fake_dataset: tuple[Path, Path]) -> None:
    """`glob` order is filesystem-dependent; the digest must not be."""
    generated, _ = fake_dataset
    files = dataset_files(DEV, generated_dir=generated)
    assert files == sorted(files)


def test_missing_dataset_reports_unknown_rather_than_raising(tmp_path: Path) -> None:
    """A harness must still print its numbers — clearly marked as untraceable."""
    empty = tmp_path / "generated"
    empty.mkdir()
    assert dataset_sha(DEV, generated_dir=empty) == UNKNOWN


def test_capture_reports_match_when_disk_agrees_with_the_manifest(
    fake_dataset: tuple[Path, Path],
) -> None:
    generated, manifest = fake_dataset
    provenance = capture(DEV, generated_dir=generated, manifest=manifest)

    assert provenance.manifest_state == "match"
    assert provenance.is_trustworthy
    assert "DRIFT" not in provenance.header_fragment()


def test_capture_reports_drift_when_disk_and_manifest_disagree(
    fake_dataset: tuple[Path, Path],
) -> None:
    """The case this whole module exists for.

    Reading the digest out of the manifest would report stale provenance with total
    confidence. Computing from disk and cross-checking surfaces the disagreement instead.
    """
    generated, manifest = fake_dataset
    (generated / f"{DEV}_bank_statement.csv").write_text("row_id\nbk_999999\n", encoding="utf-8")

    provenance = capture(DEV, generated_dir=generated, manifest=manifest)

    assert provenance.manifest_state == "drift"
    assert not provenance.is_trustworthy
    assert provenance.dataset_sha != provenance.manifest_sha
    fragment = provenance.header_fragment()
    assert "DRIFT" in fragment, f"drift must be visible in the header, got {fragment!r}"
    assert provenance.manifest_sha in fragment, "the header must show what was expected"


def test_capture_reports_absent_when_there_is_no_manifest(
    fake_dataset: tuple[Path, Path],
) -> None:
    generated, manifest = fake_dataset
    manifest.unlink()

    provenance = capture(DEV, generated_dir=generated, manifest=manifest)

    assert provenance.manifest_state == "absent"
    assert not provenance.is_trustworthy
    assert "NO MANIFEST" in provenance.header_fragment()


def test_manifest_dataset_sha_is_none_for_an_unknown_dataset(
    fake_dataset: tuple[Path, Path],
) -> None:
    _, manifest = fake_dataset
    assert manifest_dataset_sha("not_a_dataset", manifest=manifest) is None


def test_git_sha_never_raises_outside_a_repository(tmp_path: Path) -> None:
    """A tarball with no .git must still produce a printable run header."""
    assert git_sha(root=tmp_path) in {UNKNOWN} or len(git_sha(root=tmp_path)) >= 7


@pytest.mark.parametrize(
    "variable", ["FINCTL_GIT_SHA", "RENDER_GIT_COMMIT", "RAILWAY_GIT_COMMIT_SHA"]
)
def test_git_sha_reads_each_platform_commit_variable(
    variable: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every declared source is actually consulted.

    `root=tmp_path` removes the git fallback, so a variable that is *not* read would leave this
    returning UNKNOWN rather than the SHA — the test fails if a name is declared but unused.
    """
    for name in _SHA_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(variable, "abcdef1234567890")

    assert git_sha(root=tmp_path) == "abcdef1"
    assert git_sha(short=False, root=tmp_path) == "abcdef1234567890"


def test_explicit_build_arg_beats_a_platform_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ours wins. A deliberate `--build-arg GIT_SHA=` must not be shadowed by the host."""
    monkeypatch.setenv("FINCTL_GIT_SHA", "1111111aaaa")
    monkeypatch.setenv("RENDER_GIT_COMMIT", "2222222bbbb")

    assert git_sha(root=tmp_path) == "1111111"


def test_blank_platform_variable_falls_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty or whitespace value is absence, not a SHA.

    Platforms set variables to `""` in some build contexts; returning that would print an empty
    provenance field that reads as a real answer.
    """
    monkeypatch.setenv("FINCTL_GIT_SHA", "   ")
    monkeypatch.setenv("RENDER_GIT_COMMIT", "3333333cccc")

    assert git_sha(root=tmp_path) == "3333333"


def test_baked_unknown_sentinel_does_not_shadow_the_platform_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact condition of a platform-built image, which the first version got wrong.

    `ARG GIT_SHA=unknown` in the Dockerfile means `FINCTL_GIT_SHA=unknown` is *always* set in an
    image built without a build arg — which is every platform build. The earlier test passed only
    because it set that variable to blank, a state no deployment produces. So the suite was green
    while the deployed service reported `git_sha: unknown`.

    Asserts the deployed condition verbatim: sentinel baked in, platform variable present.
    """
    monkeypatch.setenv("FINCTL_GIT_SHA", UNKNOWN)
    monkeypatch.setenv("RENDER_GIT_COMMIT", "9d22c78aaaabbbb")

    assert git_sha(root=tmp_path) == "9d22c78"


def test_unknown_is_still_returned_when_it_is_the_only_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Skipping the sentinel must not turn "no provenance" into a crash or a blank.

    With no source available the honest answer is still `unknown` — the point of the skip is
    ordering, not suppression.
    """
    for name in _SHA_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FINCTL_GIT_SHA", UNKNOWN)

    assert git_sha(root=tmp_path) == UNKNOWN


def test_dockerfile_healthcheck_is_not_pinned_to_one_port() -> None:
    """The probe must read PORT.

    Render assigns PORT=10000 and uvicorn honours it, so a health check hardcoding 8000 marks a
    working container unhealthy. This asserts the property rather than the absence of the literal,
    because the CMD may legitimately keep 8000 as its default.
    """
    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(encoding="utf-8")
    healthcheck = next(
        line for line in dockerfile.splitlines() if line.lstrip().startswith("CMD [\"python\"")
    )
    assert "PORT" in healthcheck, "health check does not consult PORT"
    assert "127.0.0.1:8000" not in healthcheck, "health check is pinned to port 8000"


def test_real_repository_provenance_is_trustworthy() -> None:
    """End-to-end against the committed manifest and the checked-in datasets.

    Skips rather than fails when the datasets have not been generated: a fresh clone has a
    manifest but no `data/generated/`, and that is not a defect.
    """
    from eval.provenance import GENERATED_DIR

    if not dataset_files(DEV, generated_dir=GENERATED_DIR):
        pytest.skip("run `make seed` first — data/generated/ is gitignored")

    provenance = capture(DEV)

    assert provenance.manifest_state == "match", (
        f"on-disk datasets disagree with the committed manifest: {provenance}. Either re-run "
        "`make seed` and commit DATASET_HASHES.txt, or a generator change moved the data."
    )
    assert provenance.git_sha != UNKNOWN
    assert provenance.dataset_sha != UNKNOWN
