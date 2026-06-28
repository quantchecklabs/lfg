#!/usr/bin/env bash
# Expose ONLY the custom-LLM endpoint publicly so ElevenLabs' cloud can reach our
# brain (Option B). Everything else on :8766 stays tailnet-only.
#
# Prereq (one-time, human): enable Funnel for the tailnet by visiting the link
# `tailscale funnel` prints (https://login.tailscale.com/f/funnel?node=...).
#
# Mounts  https://<magicdns>:8443/v1/chat/completions  ->  127.0.0.1:8766/...
# The endpoint is bearer-gated (LFG_ELEVEN_LLM_SECRET), so a path leak alone
# can't drive the fleet.
set -euo pipefail

PORT=8443
TARGET="http://127.0.0.1:8766/v1/chat/completions"

echo "== raising funnel on :$PORT (path /v1/chat/completions only) =="
tailscale funnel --bg --https="$PORT" --set-path=/v1/chat/completions "$TARGET"

echo "== funnel status =="
tailscale funnel status

HOST="$(tailscale status --json | node -pe 'JSON.parse(require("fs").readFileSync(0)).Self.DNSName.replace(/\.$/,"")')"
URL="https://${HOST}:${PORT}/v1/chat/completions"
SECRET="$(grep '^LFG_ELEVEN_LLM_SECRET=' "$(dirname "$0")/../../.env" | cut -d= -f2-)"

echo "== verifying public reachability =="
echo "-- no auth (expect 401):"
curl -sS -o /dev/null -w "   HTTP %{http_code}\n" -X POST "$URL" \
  -H 'content-type: application/json' -d '{"messages":[{"role":"user","content":"hi"}]}' || true
echo "-- with bearer (expect a streamed reply):"
curl -sS -X POST "$URL" -H "authorization: Bearer $SECRET" \
  -H 'content-type: application/json' \
  -d '{"messages":[{"role":"user","content":"say hi in five words"}]}' --max-time 30 \
  | grep -o '"content":"[^"]*"' | head -1 || true

echo
echo "Public custom-LLM URL (set as ElevenLabs custom_llm.url base):"
echo "   https://${HOST}:${PORT}"
echo "Agent already points here. Done."
