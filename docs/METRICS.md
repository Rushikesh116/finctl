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

| Phase | Dataset | Auto-match | False-match | Exceptions | SHA | Date |
|---|---|---|---|---|---|---|
| 2 (baseline, exact only) | `dev_seed_11` | TBD | TBD | TBD | TBD | TBD |
| 3 (+ netting) | `dev_seed_11` | TBD | TBD | TBD | TBD | TBD |
| 4 (+ fuzzy) | `dev_seed_11` | TBD | TBD | TBD | TBD | TBD |
| 5 (+ LLM) | `dev_seed_11` | TBD | TBD | TBD | TBD | TBD |
| 6 (final, once) | `holdout_seed_97` | TBD | TBD | TBD | TBD | TBD |

`holdout_seed_97` gets exactly one row, filled in Phase 6. Whatever it prints is what
ships, even if it is worse than dev.
