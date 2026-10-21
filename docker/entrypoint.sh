#!/usr/bin/env sh
# Single-command startup: generate data (if absent) -> build DB -> serve dashboard.
set -e

: "${GEN_NUM_USERS:=100}"
: "${GEN_NUM_SESSIONS:=5000}"
: "${GEN_DAYS:=60}"
: "${GEN_SEED:=42}"
: "${GEN_PROVIDERS:=claude_code,openai,gemini,deepseek}"

generate() {  # $1 = output dir, $2 = comma-separated providers
  python data/generate_fake_data.py \
    --num-users "$GEN_NUM_USERS" \
    --num-sessions "$GEN_NUM_SESSIONS" \
    --days "$GEN_DAYS" \
    --seed "$GEN_SEED" \
    --providers "$2" \
    --output-dir "$1"
}

provider_file() {  # provider id -> its raw file name (empty for claude_code / unknown)
  case "$1" in
    openai)   echo openai_usage.jsonl ;;
    gemini)   echo gemini_usage.jsonl ;;
    deepseek) echo deepseek_usage.csv ;;
    *)        echo "" ;;
  esac
}

# Normalise + validate GEN_PROVIDERS once: trim whitespace, drop empty items,
# reject unknown names (same rule as the generator), empty list = all providers.
providers=""
has_claude_code=""
old_ifs="$IFS"; IFS=","
for p in $GEN_PROVIDERS; do
  p="$(printf '%s' "$p" | tr -d ' \t')"
  [ -n "$p" ] || continue
  case "$p" in
    claude_code) has_claude_code=1 ;;
    openai|gemini|deepseek) ;;
    *)
      IFS="$old_ifs"
      echo "ERROR: unknown provider '$p' in GEN_PROVIDERS; valid: claude_code,openai,gemini,deepseek" >&2
      exit 1 ;;
  esac
  providers="${providers:+$providers,}$p"
done
IFS="$old_ifs"
if [ -z "$providers" ]; then
  providers="claude_code,openai,gemini,deepseek"
  has_claude_code=1
fi
GEN_PROVIDERS="$providers"

if [ ! -f "$TELEMETRY_JSONL" ]; then
  # The ETL requires the Claude Code dataset, so a first start without it would only
  # crash-loop. Fail early (explicit config is never silently overridden).
  if [ -z "$has_claude_code" ]; then
    echo "ERROR: no data yet at $TELEMETRY_JSONL and GEN_PROVIDERS ($GEN_PROVIDERS) excludes" \
         "claude_code, which the ETL requires. Add claude_code to GEN_PROVIDERS (or leave it empty)." >&2
    exit 1
  fi
  echo "→ Generating synthetic usage data ($GEN_NUM_USERS users, $GEN_NUM_SESSIONS sessions, $GEN_DAYS days; $GEN_PROVIDERS)…"
  generate "$DATA_DIR" "$GEN_PROVIDERS"
else
  echo "→ Telemetry already present at $TELEMETRY_JSONL; skipping generation."
  # A data volume from before the multi-provider extension may lack provider files:
  # generate only the enabled-but-missing providers (no Claude Code sessions) into a
  # scratch dir and copy just those files over; existing files are left untouched.
  missing=""
  old_ifs="$IFS"; IFS=","
  for p in $GEN_PROVIDERS; do
    f="$(provider_file "$p")"
    if [ -n "$f" ] && [ ! -f "$DATA_DIR/$f" ]; then
      missing="${missing:+$missing,}$p"
    fi
  done
  IFS="$old_ifs"
  if [ -n "$missing" ]; then
    echo "→ Generating missing provider data: $missing"
    tmp="$(mktemp -d)"
    generate "$tmp" "$missing" > /dev/null
    old_ifs="$IFS"; IFS=","
    for p in $missing; do
      f="$(provider_file "$p")"
      [ -f "$tmp/$f" ] && cp "$tmp/$f" "$DATA_DIR/$f"
    done
    IFS="$old_ifs"
    rm -rf "$tmp"
  fi
fi

echo "→ Running ETL…"
python -m analytics.etl

echo "→ Launching dashboard on http://localhost:${STREAMLIT_SERVER_PORT}…"
exec streamlit run dashboard/app.py \
  --server.port "$STREAMLIT_SERVER_PORT" \
  --server.address "$STREAMLIT_SERVER_ADDRESS"
