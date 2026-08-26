# Metrics

Raw pasted stdout of `make eval`, with the git SHA and timestamp of the run above each
block. Nothing in this file is typed by hand — every number is pasted from a command's
output. A result that has not been run is `TBD`. See
`.claude/skills/eval-protocol/SKILL.md` for the paste rule and metric definitions.

---

## No runs yet

`TBD` — the harness lands in Phase 2. The first entry here will be the deterministic
baseline (Layer 1 exact matching only), which is what every later improvement gets measured
against.

Each entry takes this form:

```
$ git rev-parse --short HEAD
<sha>
$ date -u '+%Y-%m-%d %H:%M UTC'
<timestamp>
$ make eval
<pasted stdout, verbatim, including the ablation table>
```

### Run log

**Dataset SHA** is populated at eval time by `eval/provenance.py`, from the emitted files on
disk. Without it a row records a number but not *what the number is about*: a row from before
the datasets were regenerated is otherwise indistinguishable from one after, and since this
file is append-only and rows are compared across phases, that ambiguity would quietly
invalidate every comparison in it.

Read a row whose Dataset SHA differs from the rows around it as **measuring something else**.
It is not comparable to them, however similar the numbers look.

| Phase | Dataset | Dataset SHA | Auto-match | False-match | Exceptions | Git SHA | Date |
|---|---|---|---|---|---|---|---|
| 2 (baseline, exact only) | `dev_seed_11` | TBD | TBD | TBD | TBD | TBD | TBD |
| 3 (+ netting) | `dev_seed_11` | TBD | TBD | TBD | TBD | TBD | TBD |
| 4 (+ fuzzy) | `dev_seed_11` | TBD | TBD | TBD | TBD | TBD | TBD |
| 5 (+ LLM) | `dev_seed_11` | TBD | TBD | TBD | TBD | TBD | TBD |
| 6 (final, once) | `holdout_seed_97` | TBD | TBD | TBD | TBD | TBD | TBD |

`holdout_seed_97` gets exactly one row, filled in Phase 6. Whatever it prints is what
ships, even if it is worse than dev.

A row is also **not trustworthy** when its provenance reports `drift` (the files on disk
disagree with the committed manifest) or `absent` (no manifest). Both print inline in the
metrics block header, so the condition cannot be missed while reading the numbers.

### Dataset provenance, current

Not metrics — the reference point rows are measured against. Produced by
`eval/provenance.capture()`, at git `1e60254`:

```
Dataset: dev_seed_11      data 371df9be    manifest=match
Dataset: holdout_seed_97  data a3cccfd9    manifest=match
```

The digest is per dataset, so regenerating the holdout does not invalidate the provenance of
every dev row. Full per-file hashes are in `data/DATASET_HASHES.txt`.
