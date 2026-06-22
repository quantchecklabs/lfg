#!/usr/bin/env bash
# lfg box-ensure — one ATTEMPT per run (driven by lfg-box-ensure.timer every ~2min).
#
# The GPU box (Novita RTX 5090, eu-ger-2) is currently un-startable because Novita
# has no 5090 capacity free (INSUFFICIENT_RESOURCE). This keeps requesting the
# start each tick; the moment a GPU frees it brings services + tunnel up and runs
# the TTS failover, then SELF-DISABLES its timer once the orb's voice is verified.
#
# Durable by design: it's a systemd --user timer, so it survives this Claude
# session ending AND a dev-box reboot (unlike a session-scoped background task).
# It is a one-time RECOVERY retrier — after success the normal daily timers
# (lfg-stt-start @03:00, lfg-tts-failover @03:10, lfg-stt-stop @15:00) take over.
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export DBUS_SESSION_BUS_ADDRESS="unix:path=${XDG_RUNTIME_DIR}/bus"
cd /home/dev/repos/lfg || exit 0
set -a; . ./.env; [ -f deploy/gpu-stt/instance.env ] && . deploy/gpu-stt/instance.env; set +a
API=https://api.novita.ai/gpu-instance/openapi/v1
LOG=/home/dev/.local/state/lfg-box-ensure.log; mkdir -p "$(dirname "$LOG")"
say(){ echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" | tee -a "$LOG"; }

qstatus(){ curl -s -m 15 -H "Authorization: Bearer $NOVITA_API_KEY" "$API/gpu/instance?instanceId=$DE_INSTANCE_ID" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("status",""))' 2>/dev/null; }
orb_ok(){
  local code b; code=$(curl -s -o /tmp/lfg-orb-chk.bin -w '%{http_code}' --max-time 25 \
    -X POST http://127.0.0.1:8766/api/voice/tts -H 'content-type: application/json' \
    -d '{"text":"voice check"}' 2>/dev/null || echo 000)
  b=$(stat -c%s /tmp/lfg-orb-chk.bin 2>/dev/null || echo 0)
  [ "$code" = "200" ] && [ "${b:-0}" -gt 10000 ]
}
done_disable(){ say "$1 — voice verified, disabling lfg-box-ensure.timer"; systemctl --user disable --now lfg-box-ensure.timer >>"$LOG" 2>&1; }

st=$(qstatus)
say "tick: instance status=${st:-unknown}"

if [ "$st" = "running" ]; then
  if orb_ok; then done_disable "box running and orb TTS healthy"; exit 0; fi
  say "box running but orb TTS not healthy — ensuring services + failover"
  bash deploy/gpu-stt/lifecycle.sh start >>"$LOG" 2>&1 || say "lifecycle start non-zero (continuing)"
  bash deploy/ops/lfg-tts-failover.sh >>"$LOG" 2>&1 || say "failover non-zero"
  if orb_ok; then done_disable "orb TTS fixed"; else say "still not healthy; retry next tick"; fi
  exit 0
fi

# not running: request a start; capacity may still be exhausted
resp=$(curl -s -m 30 -X POST -H "Authorization: Bearer $NOVITA_API_KEY" -H "Content-Type: application/json" \
  -d "{\"instanceId\":\"$DE_INSTANCE_ID\"}" "$API/gpu/instance/start" 2>/dev/null)
if echo "$resp" | grep -q INSUFFICIENT_RESOURCE; then
  say "no 5090 capacity yet (INSUFFICIENT_RESOURCE); will retry next tick"
else
  say "start requested (resp=$resp); next tick will pick it up when running"
fi
exit 0
