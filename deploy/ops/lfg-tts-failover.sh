#!/usr/bin/env bash
# lfg orb TTS failover — runs each morning after the GPU box starts.
#
# The orb's TTS goes through serve's /api/voice/tts proxy → TTS_UPSTREAM. The
# voice-fixes session is migrating the Chatterbox engine (:8090) to a streaming
# variant; while that's broken it returns HTTP 200 but emits ZERO audio and
# hangs the orb. CosyVoice2 (:8088) is the known-good fallback.
#
# This script picks a WORKING engine deterministically: prefer Chatterbox :8090
# once its deploy lands and it actually produces audio; otherwise fall back to
# :8088. Then it restarts serve and verifies real audio end-to-end. It only ever
# edits the TTS_UPSTREAM line in .env — it does NOT touch any voice-fixes files.
#
# Installed as a systemd user timer (see deploy/ops/lfg-tts-failover.{service,timer}
# and deploy/ops/README.md). Runtime log: ~/.local/state/lfg-tts-failover.log
set -uo pipefail
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export DBUS_SESSION_BUS_ADDRESS="unix:path=${XDG_RUNTIME_DIR}/bus"

REPO=/home/dev/repos/lfg
ENV="$REPO/.env"
LIFECYCLE="$REPO/deploy/gpu-stt/lifecycle.sh"
LOG=/home/dev/.local/state/lfg-tts-failover.log
mkdir -p "$(dirname "$LOG")"
log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" | tee -a "$LOG" >&2; }

TOK=$(grep -E '^TTS_TOKEN=' "$ENV" | head -1 | cut -d= -f2-)

# probe_tts URL -> echoes "<http_code> <bytes>" for a /tts synthesis probe
probe_tts() {
  local url="$1" out code; out=$(mktemp)
  code=$(curl -s -o "$out" -w '%{http_code}' --max-time 22 \
    -X POST "$url" -H "Authorization: Bearer $TOK" \
    -H 'content-type: application/json' \
    -d '{"text":"failover probe","voice":"F1"}' 2>/dev/null || echo 000)
  echo "$code $(stat -c%s "$out" 2>/dev/null || echo 0)"; rm -f "$out"
}

log "=== failover start ==="
log "box status: $(bash "$LIFECYCLE" status 2>/dev/null | head -1)"

# 1. Wait for CosyVoice2 :8088 (lazy model load) up to ~6 min. If it never comes,
#    try to bring the box up ourselves, then wait again.
wait_8088() { for _ in $(seq 1 36); do curl -s -o /dev/null --max-time 6 http://127.0.0.1:8088/health && return 0; sleep 10; done; return 1; }
if ! wait_8088; then
  log "8088 not healthy; running lifecycle start"
  bash "$LIFECYCLE" start >>"$LOG" 2>&1 || log "lifecycle start returned non-zero"
  wait_8088 || { log "FAIL: CosyVoice2 :8088 never healthy — voice still down, needs human"; exit 1; }
fi
log "CosyVoice2 :8088 healthy"

# 2. Prefer Chatterbox :8090 only if it returns real audio quickly.
engine=8088
read -r cb_code cb_bytes <<<"$(probe_tts http://127.0.0.1:8090/tts)"
if [ "$cb_code" = "200" ] && [ "${cb_bytes:-0}" -gt 10000 ]; then
  engine=8090
  log "Chatterbox :8090 OK (bytes=$cb_bytes) -> using 8090 (streaming deploy landed)"
else
  log "Chatterbox :8090 no audio (code=$cb_code bytes=$cb_bytes) -> staying on 8088"
fi

# 3. Point TTS_UPSTREAM at the chosen engine (only that line; keep the comment).
sed -i -E "s|^TTS_UPSTREAM=.*|TTS_UPSTREAM=http://127.0.0.1:${engine}|" "$ENV"
log "TTS_UPSTREAM -> :$engine"

# 4. Restart serve so it picks up the upstream.
systemctl --user restart lfg-serve.service && sleep 3

# 5. Verify end-to-end through the orb's actual proxy.
curl -s -o /tmp/lfg-tts-e2e.bin -w '' --max-time 25 \
  -X POST http://127.0.0.1:8766/api/voice/tts -H 'content-type: application/json' \
  -d '{"text":"morning voice check"}' >/dev/null 2>&1
v_code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 25 \
  -X POST http://127.0.0.1:8766/api/voice/tts -H 'content-type: application/json' \
  -d '{"text":"morning voice check"}' 2>/dev/null || echo 000)
v_bytes=$(stat -c%s /tmp/lfg-tts-e2e.bin 2>/dev/null || echo 0)
if [ "$v_code" = "200" ] && [ "${v_bytes:-0}" -gt 10000 ]; then
  log "OK: orb TTS verified via :$engine (http=$v_code bytes=$v_bytes)"
  log "=== failover done ==="
else
  log "FAIL: orb TTS broken (http=$v_code bytes=$v_bytes) — needs human"
  exit 1
fi
