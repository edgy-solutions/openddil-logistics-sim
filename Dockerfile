# =============================================================================
# openddil-logistics-sim
# =============================================================================
# Multi-profile per-element telemetry simulator. First-shipped profile is
# MRAD; LTAMDS / Patriot / future asset types land as additional entries
# in config/default.yaml's asset_profiles[] -- no rebuild needed.
#
# Honors upstream operational_state from telemetry-latest-state:
# power_state OFF/SHUTTING_DOWN/MAINTENANCE or health_state DEGRADED/
# FAULT/FAILED flip per-element synthesis into the degraded band; tx /
# rx flags from the customer feed propagate through to each face
# element's tx_active / rx_active fields.
# =============================================================================

# ---------- Stage 1: Builder ----------
FROM python:3.11-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc g++ make librdkafka-dev \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv && uv venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app
COPY pyproject.toml .
COPY src ./src
RUN uv pip compile pyproject.toml -o requirements.txt \
    && uv pip install --no-cache -r requirements.txt \
    && uv pip install --no-cache --no-deps -e .

# ---------- Stage 2: Runtime ----------
FROM python:3.11-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
        librdkafka1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app/src /app/src
COPY config /app/config
ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/src:/proto

WORKDIR /app

# `logistics-sim` script entry comes from pyproject.toml's
# [project.scripts] -- calls logistics_sim.main:main(). Same convention
# the other openddil-* Python services follow.
CMD ["logistics-sim"]
