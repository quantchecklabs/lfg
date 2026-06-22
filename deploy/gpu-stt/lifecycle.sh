#!/usr/bin/env bash
# Orchestrates the Novita GPU STT box from the dev box.
#   lifecycle.sh status | start | stop | sync-tunnel | ensure | watchdog
#
# Persistence model: the box has no in-container autostart (Novita manages SSH,
# our service runs in tmux). So on every START we re-resolve the SSH endpoint +
# password (they rotate), re-point the tunnel, and re-ensure the service. A
# watchdog timer repeats this if the box migrates/crashes while it should be up.
set -euo pipefail
HERE=$(dirname "$(readlink -f "$0")")
REPO=/home/dev/repos/lfg
set -a; . "$REPO/.env"; [ -f "$HERE/instance.env" ] && . "$HERE/instance.env"; set +a
: "${NOVITA_API_KEY:?}"; : "${DE_INSTANCE_ID:?}"; : "${STT_TOKEN:?}"
API=https://api.novita.ai/gpu-instance/openapi/v1
KEY=$NOVITA_API_KEY
SSHKEY=/home/dev/.ssh/id_ed25519
TUNENV=/home/dev/.config/lfg-stt-tunnel.env
UNIT=/home/dev/.config/systemd/user/lfg-stt-tunnel.service
export XDG_RUNTIME_DIR="/run/user/$(id -u)"

_get() { curl -s -m 20 -H "Authorization: Bearer $KEY" "$API/gpu/instance?instanceId=$DE_INSTANCE_ID"; }
_post() { curl -s -m 30 -X POST -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d "{\"instanceId\":\"$DE_INSTANCE_ID\"}" "$API/gpu/instance/$1"; }

status() { _get | python3 -c 'import sys,json;print(json.load(sys.stdin).get("status",""))'; }
_endpoint() { _get | python3 -c '
import sys,json,re
d=json.load(sys.stdin); c=d.get("connectComponentSSH") or {}
m=re.search(r"-p (\d+) (\S+)", c.get("sshCommand",""))
pw=c.get("password") or d.get("sshPassword") or ""
print(f"{m.group(1)} {m.group(2)} {pw}" if m else "")'; }

remote_healthy() { curl -s -m 6 http://127.0.0.1:8087/health >/dev/null 2>&1; }

sync_tunnel() {
  read -r PORT HOSTU PW <<<"$(_endpoint)"
  [ -n "${PORT:-}" ] && [ -n "${PW:-}" ] || { echo "no SSH endpoint yet"; return 1; }
  printf 'SSHPASS=%s\n' "$PW" > "$TUNENV"; chmod 600 "$TUNENV"
  cat > "$UNIT" <<UNITEOF
[Unit]
Description=lfg STT SSH tunnel -> Novita 5090 (Cantonese SenseVoice on :8087)
After=network-online.target
Wants=network-online.target
[Service]
EnvironmentFile=%h/.config/lfg-stt-tunnel.env
ExecStart=/usr/bin/sshpass -e /usr/bin/ssh -N -T -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/home/dev/.ssh/known_hosts -p ${PORT} -L 127.0.0.1:8087:127.0.0.1:8087 -L 127.0.0.1:8088:127.0.0.1:8088 -L 127.0.0.1:8089:127.0.0.1:8089 -L 127.0.0.1:8090:127.0.0.1:8090 -L 127.0.0.1:8091:127.0.0.1:8091 ${HOSTU}
Restart=always
RestartSec=5
[Install]
WantedBy=default.target
UNITEOF
  systemctl --user daemon-reload
  systemctl --user restart lfg-stt-tunnel.service
  echo "tunnel -> ${HOSTU}:${PORT}"
}

ensure_service() {
  read -r PORT HOSTU PW <<<"$(_endpoint)"
  [ -n "${PORT:-}" ] && [ -n "${PW:-}" ] || { echo "no SSH endpoint"; return 1; }
  local SSHO="-p $PORT -o StrictHostKeyChecking=accept-new -o ConnectTimeout=25"
  sshpass -p "$PW" scp -P "$PORT" -o StrictHostKeyChecking=accept-new \
    "$HERE/server.py" "$HERE/parakeet_stt.py" "$HERE/bootstrap-remote.sh" "$HOSTU:/opt/stt/" 2>/dev/null || { echo "scp failed (sshd not up?)"; return 1; }
  printf 'STT_TOKEN=%s\nSTT_LANG=yue\nSTT_TO_TRADITIONAL=1\nSTT_PORT=8087\nPARAKEET_ENABLE=%s\nPARAKEET_PORT=%s\n' \
    "$STT_TOKEN" "${PARAKEET_ENABLE:-0}" "${PARAKEET_PORT:-8091}" \
    | sshpass -p "$PW" ssh $SSHO "$HOSTU" 'cat > /opt/stt/stt.env && chmod 600 /opt/stt/stt.env'
  sshpass -p "$PW" ssh $SSHO "$HOSTU" 'bash /opt/stt/bootstrap-remote.sh'
}

wait_running() { for i in $(seq 1 40); do [ "$(status)" = "running" ] && return 0; sleep 8; done; return 1; }
wait_sshd()   { read -r PORT HOSTU PW <<<"$(_endpoint)"; for i in $(seq 1 30); do sshpass -p "$PW" ssh -p "$PORT" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 -o BatchMode=yes "$HOSTU" true 2>/dev/null && return 0; sleep 8; done; return 1; }

case "${1:-status}" in
  status)
    echo "instance: $(status)"; remote_healthy && echo "stt /health: OK" || echo "stt /health: DOWN" ;;
  start)
    echo "starting instance..."; _post start >/dev/null; wait_running || { echo "did not reach running"; exit 1; }
    echo "waiting for sshd..."; wait_sshd || { echo "sshd not reachable"; exit 1; }
    ensure_service; sync_tunnel
    sleep 2; remote_healthy && echo "START OK (/health up)" || { echo "START: /health still down"; exit 1; } ;;
  stop)
    echo "stopping instance..."; _post stop >/dev/null; echo "stopped" ;;
  sync-tunnel) sync_tunnel ;;
  ensure) ensure_service ;;
  watchdog)
    # only act if the box should be up (running) but STT is unreachable
    if [ "$(status)" = "running" ] && ! remote_healthy; then
      echo "watchdog: box running but STT down -> re-syncing"; wait_sshd && ensure_service && sync_tunnel
    else
      echo "watchdog: nothing to do ($(status), $(remote_healthy && echo up || echo down))"
    fi ;;
  *) echo "usage: $0 status|start|stop|sync-tunnel|ensure|watchdog"; exit 2 ;;
esac
