# FinCtl — one container serving the JSON API and the static page.
#
# Base is python:3.13-slim rather than the brief's 3.11-slim, deliberately: the local venv is
# 3.13 and a dev/prod interpreter skew is the wrong bug to accept in a project whose entire claim
# is that its numbers are trustworthy (D-0001, confirmed at the Phase 0 review).

FROM python:3.13-slim AS build

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Dependencies first, so a source change does not re-resolve the whole tree on every build.
COPY requirements.txt .
RUN python -m pip install --prefix=/install -r requirements.txt


FROM python:3.13-slim

# Baked in at build time because the image deliberately carries no .git. Without it the deployed
# container cannot say which code produced its numbers.
ARG GIT_SHA=unknown

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    FINCTL_GIT_SHA=${GIT_SHA} \
    # The deployed default. Serves the pre-computed run from committed fixtures: no API key, no
    # cold-start model call, no quota to exhaust, and byte-identical output on every click. A
    # replay cache miss raises rather than reaching the network, so an incomplete fixture set
    # fails the container at startup instead of silently serving something else.
    DEMO_MODE=1 \
    PORT=8000

# Non-root. Created before COPY so the app files can be owned by it directly.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin finctl

COPY --from=build /install /usr/local
WORKDIR /app

COPY --chown=finctl:finctl core/ ./core/
COPY --chown=finctl:finctl data/ ./data/
COPY --chown=finctl:finctl audit/ ./audit/
COPY --chown=finctl:finctl eval/ ./eval/
COPY --chown=finctl:finctl api/ ./api/
COPY --chown=finctl:finctl scripts/ ./scripts/
COPY --chown=finctl:finctl fixtures/ ./fixtures/
COPY --chown=finctl:finctl docs/ ./docs/
# The page skeleton and stylesheet. Inlined into the output at render time, never served as
# assets — but the renderer reads them from disk, so the image needs them.
COPY --chown=finctl:finctl web/ ./web/
COPY --chown=finctl:finctl cli.py Makefile ./

USER finctl

# The datasets are gitignored because they are deterministic from their seed, so the image builds
# them rather than shipping them. No network and no key needed, and the committed hash manifest
# is what makes "the image has the right data" checkable rather than assumed.
RUN python -m data.generator --all \
 && python -m scripts.render_report --out docs/index.html

EXPOSE 8000

# Reports readiness AND the dataset/git SHAs it is serving, so a healthy container that is
# serving stale data is distinguishable from one that is not.
#
# Reads PORT rather than hardcoding 8000: the platform picks the port (Render defaults to 10000
# and uvicorn honours it below), and a probe pinned to 8000 would report an otherwise healthy
# container as failing — a health check lying in the safe direction is still a health check lying.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import os,sys,urllib.request; port=os.environ.get('PORT','8000'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=4).status==200 else 1)"]

CMD ["sh", "-c", "uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
