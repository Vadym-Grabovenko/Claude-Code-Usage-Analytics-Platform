#!/usr/bin/env sh
# Single-command startup: generate data (if absent) -> build DB -> serve dashboard.
set -e

: "${GEN_NUM_USERS:=100}"
: "${GEN_NUM_SESSIONS:=5000}"
: "${GEN_DAYS:=60}"
: "${GEN_SEED:=42}"

if [ ! -f "$TELEMETRY_JSONL" ]; then
  echo "→ Generating synthetic telemetry ($GEN_NUM_USERS users, $GEN_NUM_SESSIONS sessions, $GEN_DAYS days)…"
  python data/generate_fake_data.py \
    --num-users "$GEN_NUM_USERS" \
    --num-sessions "$GEN_NUM_SESSIONS" \
    --days "$GEN_DAYS" \
    --seed "$GEN_SEED" \
    --output-dir "$DATA_DIR"
else
  echo "→ Telemetry already present at $TELEMETRY_JSONL; skipping generation."
fi

echo "→ Running ETL…"
python -m analytics.etl

echo "→ Launching dashboard on http://localhost:${STREAMLIT_SERVER_PORT}…"
exec streamlit run dashboard/app.py \
  --server.port "$STREAMLIT_SERVER_PORT" \
  --server.address "$STREAMLIT_SERVER_ADDRESS"
