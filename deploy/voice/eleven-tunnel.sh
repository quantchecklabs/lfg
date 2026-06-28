#!/usr/bin/env bash
# Run a cloudflared quick tunnel in front of the edge proxy, capture the public
# trycloudflare URL, and repoint the ElevenLabs agent's custom_llm at it. Meant
# to run under systemd (Restart=always): if cloudflared dies the URL rotates, so
# on every (re)start we re-extract and re-point the agent automatically.
#
# foreground: stays alive as long as cloudflared does (systemd supervises).
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
EDGE_PORT="${LFG_EDGE_PORT:-8788}"
LOG="$ROOT/data/eleven-cloudflared.log"
URLFILE="$ROOT/data/eleven-tunnel-url.txt"
: > "$LOG"

# Start cloudflared (quick tunnel — no account needed). It prints the public URL
# to its logs within a few seconds.
cloudflared tunnel --no-autoupdate --url "http://127.0.0.1:${EDGE_PORT}" \
  --logfile "$LOG" --loglevel info &
CF_PID=$!
trap 'kill $CF_PID 2>/dev/null' EXIT TERM INT

# Wait for the https://*.trycloudflare.com URL to appear.
URL=""
for _ in $(seq 1 40); do
  URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG" | head -1)"
  [ -n "$URL" ] && break
  # cloudflared died early?
  kill -0 "$CF_PID" 2>/dev/null || { echo "[eleven-tunnel] cloudflared exited early"; exit 1; }
  sleep 1
done
[ -n "$URL" ] || { echo "[eleven-tunnel] no tunnel URL after 40s"; exit 1; }

echo "$URL" > "$URLFILE"
echo "[eleven-tunnel] public URL: $URL  (agent custom_llm base; appends /v1/chat/completions)"

# Repoint the agent at the new public base (idempotent — no-op if unchanged).
LFG_ELEVEN_LLM_URL="$URL" bun deploy/voice/provision-eleven-agent.ts seturl "$URL" \
  || echo "[eleven-tunnel] WARN: agent repoint failed (will retry on next restart)"

# Stay alive with cloudflared so systemd treats us as the service lifetime.
wait "$CF_PID"
