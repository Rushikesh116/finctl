# FinCtl -- build, seed, reconcile, evaluate, report.
# Every target here is documented in CLAUDE.md. Keep the two in sync.
#
# `make demo` is what a judge runs: it must work on a clean clone with no API key.

BOOTSTRAP_PYTHON ?= python3.13
VENV             := .venv
PY               := $(VENV)/bin/python
DEV_DATASET      ?= dev_seed_11
HOLDOUT_DATASET  ?= holdout_seed_97

.DEFAULT_GOAL := help
.PHONY: help setup hooks seed run eval eval-holdout llm-curve report serve test demo clean

help:
	@echo "FinCtl targets"
	@echo "  setup   Create $(VENV) on $(BOOTSTRAP_PYTHON), install pinned deps, install git hooks"
	@echo "  seed    Generate $(DEV_DATASET) and $(HOLDOUT_DATASET) into data/generated/"
	@echo "  run     Reconcile $(DEV_DATASET), write decisions + matches to SQLite"
	@echo "  eval    Full harness: dev + holdout + ablation; prints the metrics block"
	@echo "  report  Render the static run report to docs/index.html (no server, no fetch)"
	@echo "  serve   Run the FastAPI app -- API and UI from one process -- on :8000"
	@echo "  test    Run pytest"
	@echo "  demo    seed + run + eval + report from clean; needs no API key"
	@echo "  clean   Remove generated datasets, run databases and caches"

setup:
	@test -d $(VENV) || $(BOOTSTRAP_PYTHON) -m venv $(VENV)
	@$(PY) -m pip install --quiet --upgrade pip
	@$(PY) -m pip install --quiet -r requirements.txt
	@$(MAKE) --no-print-directory hooks
	@printf 'setup ok -- %s\n' "$$($(PY) --version)"

hooks:
	@git config core.hooksPath .githooks
	@chmod +x .githooks/pre-commit
	@echo "git hooks -> .githooks (staged-diff secret scan active)"

seed:
	$(PY) -m data.generator --all

run:
	$(PY) -m cli reconcile --dataset $(DEV_DATASET)

eval:
	$(PY) -m eval.harness --dev $(DEV_DATASET) --ablation

# Deliberately NOT part of `make eval`. The holdout is evaluated ONCE, in Phase 6.
# Iterating against it converts it into a training set and every number after that is a lie.
eval-holdout:
	@echo "About to evaluate $(HOLDOUT_DATASET). This is a Phase 6, once-only action."
	@echo "Whatever it prints is what ships, even if it is worse than dev."
	$(PY) -m eval.harness --dev $(DEV_DATASET) --holdout $(HOLDOUT_DATASET) --ablation

llm-curve:
	$(PY) -m scripts.llm_curve --runs 4

report:
	$(PY) -m scripts.render_report --out docs/index.html

serve:
	$(VENV)/bin/uvicorn api.main:app --host 127.0.0.1 --port 8000

test:
	$(PY) -m pytest

demo: seed run eval report
	@echo "demo complete -- open docs/index.html"

clean:
	rm -rf data/generated/* .pytest_cache
	rm -f $(DEV_DATASET).db $(HOLDOUT_DATASET).db finctl.db
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	@echo "clean ok"
