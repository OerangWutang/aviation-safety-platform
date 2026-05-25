# ── Stage 1: build wheel ────────────────────────────────────────────────────
# A separate build stage keeps build tools (pip, setuptools, wheel) and the
# editable .egg-link out of the final image.  Only the compiled .whl is
# copied forward; the runtime layer has no write access to source trees.
#
# BASE IMAGE PINNING
# ------------------
# To pin to a specific digest (recommended for reproducible builds):
#
#   docker pull python:3.12-slim
#   docker inspect --format='{{index .RepoDigests 0}}' python:3.12-slim
#
# Then replace `python:3.12-slim` with `python:3.12-slim@sha256:<digest>` in
# both FROM lines.  Re-run on each intentional base image update.
FROM python:3.12-slim@sha256:090ba77e2958f6af52a5341f788b50b032dd4ca28377d2893dcf1ecbdfdfe203 AS builder

ENV PIP_NO_CACHE_DIR=1

WORKDIR /build

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir build \
 && python -m build --wheel --outdir /build/dist


# ── Stage 2: production runtime ─────────────────────────────────────────────
FROM python:3.12-slim@sha256:090ba77e2958f6af52a5341f788b50b032dd4ca28377d2893dcf1ecbdfdfe203

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Non-root user: containers should never run as root in production.
RUN useradd --no-create-home --shell /bin/false --uid 1001 atlas

WORKDIR /app

# Install from the pinned lock file. requirements.txt must be committed -
# building from requirements.in is intentionally unsupported because unpinned
# builds are non-reproducible.
COPY requirements.txt /tmp/requirements.txt
COPY --from=builder /build/dist/*.whl /tmp/

RUN pip install --no-cache-dir -r /tmp/requirements.txt \
 && pip install --no-cache-dir --no-deps /tmp/*.whl \
 && rm -rf /tmp/*.whl /tmp/requirements.txt

# Migration artifacts are needed at runtime (alembic upgrade head on startup).
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
COPY gunicorn_conf.py ./gunicorn_conf.py

USER atlas
EXPOSE 8000


CMD ["gunicorn", "atlas.presentation.api.app:app", "--config", "gunicorn_conf.py", "--worker-tmp-dir", "/dev/shm"]
