# syntax=docker/dockerfile:1
#
# Multi-stage build: the builder installs the runtime dependencies into a venv and
# strips what is never imported at runtime; the final image is the same slim base
# plus that venv and the app code — no pip cache, no build tooling, no tests.
#
# Base pinned to an exact Python patch + Debian codename, and by multi-arch index
# digest (amd64 + arm64) for reproducible builds. To bump: pick the new tag, then
#   docker buildx imagetools inspect python:<tag>   # -> "Digest:" line
# Keep the Python minor in sync with CI (.github/workflows/ci.yml) and make sure
# duckdb / pandas / numpy / pyarrow / streamlit ship manylinux wheels for it.
ARG PYTHON_IMAGE=python:3.12.15-slim-trixie@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

# --- builder ------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# A pip-less venv; the base image's pip installs into it via --python, so neither pip
# nor setuptools end up in the copied venv. All deps are binary wheels (no compiler).
RUN python -m venv --without-pip /opt/venv

# constraints.txt pins the exact versions verified in this image (incl. transitive deps),
# so rebuilds don't drift; refresh it deliberately (see the header of constraints.txt).
COPY requirements.txt constraints.txt /tmp/
# --no-compile + removing __pycache__: ~110 MB less, at the cost of ~1.5 s extra
# import time once per process (bytecode is compiled in memory; the venv is read-only
# for the unprivileged runtime user anyway). Bundled test suites, type stubs and C
# headers/Cython sources are only used for development/compilation, never imported.
RUN pip --python /opt/venv/bin/python install --no-compile --only-binary=:all: \
        -r /tmp/requirements.txt -c /tmp/constraints.txt \
 && find /opt/venv -depth -type d \( -name __pycache__ -o -name tests \) -exec rm -rf {} + \
 && find /opt/venv -type f \( -name '*.pyi' -o -name '*.pyx' -o -name '*.pxd' -o -name '*.pxi' \
        -o -name '*.h' -o -name '*.hpp' \) -delete \
 && rm -rf /opt/venv/lib/python3*/site-packages/pyarrow/include \
           /opt/venv/lib/python3*/site-packages/pyarrow/src

# --- runtime ------------------------------------------------------------------
FROM ${PYTHON_IMAGE}

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/src \
    DATA_DIR=/app/data/output \
    TELEMETRY_JSONL=/app/data/output/telemetry_logs.jsonl \
    EMPLOYEES_CSV=/app/data/output/employees.csv \
    DUCKDB_PATH=/app/data/analytics.duckdb \
    STREAMLIT_SERVER_PORT=8501 \
    STREAMLIT_SERVER_ADDRESS=0.0.0.0

# Unprivileged runtime user. Both services start as root only to pick the right uid
# for the ./data bind mount, then drop privileges (docker/run-as-data-owner.sh).
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --home-dir /home/app --create-home \
        --shell /usr/sbin/nologin app

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY src/ ./src/
COPY dashboard/ ./dashboard/
COPY api/ ./api/
COPY data/generate_fake_data.py ./data/generate_fake_data.py
COPY --chmod=755 docker/entrypoint.sh docker/run-as-data-owner.sh ./docker/
RUN chown app:app /app/data

EXPOSE 8501 8000

# Entrypoint generates data (if missing), runs the ETL, then serves the dashboard.
ENTRYPOINT ["./docker/entrypoint.sh"]
