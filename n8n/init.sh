#!/bin/sh
# One-shot setup for n8n: import credentials (from environment variables) and the
# Campaign Agent workflow, then publish it. Safe to re-run.
set -eu
: "${NITX_DEMO_API_KEY:?NITX_DEMO_API_KEY is not set}"
if [ -z "${DEEPSEEK_API_KEY:-}" ]; then
  echo "WARNING: DEEPSEEK_API_KEY is empty - the agent will fail until you set it in .env and re-run 'docker compose up -d'." >&2
fi
umask 077
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
cat > "$tmp" <<JSON
[
  {"id": "deepseekCred0001", "name": "DeepSeek", "type": "deepSeekApi",
   "data": {"apiKey": "${DEEPSEEK_API_KEY:-unset}", "url": "https://api.deepseek.com"}},
  {"id": "nitxDemoCred0001", "name": "Nitx platform (demo tenant)", "type": "httpHeaderAuth",
   "data": {"name": "Authorization", "value": "Bearer ${NITX_DEMO_API_KEY}"}}
]
JSON
n8n import:credentials --input="$tmp"
n8n import:workflow --input=/workflows/campaign-agent.json
n8n publish:workflow --id=campaignAgent001
echo "n8n initialised"
