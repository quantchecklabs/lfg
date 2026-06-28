#!/usr/bin/env bash
set -euo pipefail

IFACE="${SAKANA_WG_IFACE:-wg-sakana}"
WG_NET="${SAKANA_WG_NET:-10.66.77.0/24}"
SERVER_ADDR="${SAKANA_WG_SERVER_ADDR:-10.66.77.1/24}"
CLIENT_ADDR="${SAKANA_WG_CLIENT_ADDR:-10.66.77.2/32}"
LISTEN_PORT="${SAKANA_WG_PORT:-51820}"
TARGET_HOST="${SAKANA_WG_TARGET_HOST:-api.sakana.ai}"
LOCAL_CONF="/etc/wireguard/${IFACE}.conf"
ROUTE_SCRIPT="/usr/local/sbin/${IFACE}-routes"
ROUTE_STATE="/run/${IFACE}-routes"
SERVICE="/etc/systemd/system/${IFACE}-routes.service"
TIMER="/etc/systemd/system/${IFACE}-routes.timer"

usage() {
  cat <<EOF
Usage:
  $0 install root@US_VPS [endpoint-host-or-ip]
  $0 status
  $0 test
  $0 down

Creates a WireGuard tunnel to a US VPS and routes only ${TARGET_HOST}
through that VPS. The rest of this machine's traffic stays unchanged.

Environment overrides:
  SAKANA_WG_IFACE=${IFACE}
  SAKANA_WG_PORT=${LISTEN_PORT}
  SAKANA_WG_TARGET_HOST=${TARGET_HOST}
EOF
}

need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "missing required command: $1" >&2
    exit 1
  }
}

install_local_packages() {
  if command -v wg >/dev/null 2>&1 && command -v wg-quick >/dev/null 2>&1; then
    return
  fi
  sudo apt-get update
  sudo apt-get install -y wireguard iproute2 dnsutils curl
}

remote_host_part() {
  local remote="$1"
  remote="${remote##*@}"
  remote="${remote%%:*}"
  printf '%s\n' "$remote"
}

resolve_endpoint_ip() {
  local endpoint="$1"
  if [[ "$endpoint" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    printf '%s\n' "$endpoint"
    return
  fi
  getent ahostsv4 "$endpoint" | awk 'NR == 1 { print $1 }'
}

install_remote() {
  local remote="$1"
  local endpoint="${2:-$(remote_host_part "$remote")}"
  local endpoint_ip
  endpoint_ip="$(resolve_endpoint_ip "$endpoint")"
  if [[ -z "$endpoint_ip" ]]; then
    echo "could not resolve endpoint: $endpoint" >&2
    exit 1
  fi

  install_local_packages
  need wg
  need wg-quick
  need ssh

  local server_priv server_pub client_priv client_pub psk
  local client_allowed_ip="${CLIENT_ADDR%%/*}/32"
  server_priv="$(wg genkey)"
  server_pub="$(printf '%s' "$server_priv" | wg pubkey)"
  client_priv="$(wg genkey)"
  client_pub="$(printf '%s' "$client_priv" | wg pubkey)"
  psk="$(wg genpsk)"

  ssh "$remote" "sudo bash -s" -- \
    "$IFACE" "$SERVER_ADDR" "$WG_NET" "$LISTEN_PORT" "$server_priv" "$client_pub" "$psk" "$client_allowed_ip" <<'REMOTE'
set -euo pipefail
IFACE="$1"
SERVER_ADDR="$2"
WG_NET="$3"
LISTEN_PORT="$4"
SERVER_PRIV="$5"
CLIENT_PUB="$6"
PSK="$7"
CLIENT_ALLOWED_IP="$8"

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y wireguard iproute2 iptables curl

WAN_IFACE="$(ip route show default 0.0.0.0/0 | awk 'NR == 1 { for (i = 1; i <= NF; i++) if ($i == "dev") print $(i + 1) }')"
if [ -z "$WAN_IFACE" ]; then
  echo "remote: could not determine default WAN interface" >&2
  exit 1
fi

install -d -m 700 /etc/wireguard
umask 077
cat >"/etc/wireguard/${IFACE}.conf" <<EOF
[Interface]
Address = ${SERVER_ADDR}
ListenPort = ${LISTEN_PORT}
PrivateKey = ${SERVER_PRIV}
PostUp = iptables -t nat -A POSTROUTING -s ${WG_NET} -o ${WAN_IFACE} -j MASQUERADE; iptables -A FORWARD -i %i -o ${WAN_IFACE} -j ACCEPT; iptables -A FORWARD -i ${WAN_IFACE} -o %i -m state --state RELATED,ESTABLISHED -j ACCEPT
PostDown = iptables -t nat -D POSTROUTING -s ${WG_NET} -o ${WAN_IFACE} -j MASQUERADE || true; iptables -D FORWARD -i %i -o ${WAN_IFACE} -j ACCEPT || true; iptables -D FORWARD -i ${WAN_IFACE} -o %i -m state --state RELATED,ESTABLISHED -j ACCEPT || true

[Peer]
PublicKey = ${CLIENT_PUB}
PresharedKey = ${PSK}
AllowedIPs = ${CLIENT_ALLOWED_IP}
EOF

cat >/etc/sysctl.d/99-wireguard-${IFACE}.conf <<EOF
net.ipv4.ip_forward = 1
EOF
sysctl --system >/dev/null

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q '^Status: active'; then
  ufw allow "${LISTEN_PORT}/udp"
fi

systemctl enable --now "wg-quick@${IFACE}"
systemctl restart "wg-quick@${IFACE}"
curl -sS https://ipinfo.io/json || true
REMOTE

  sudo install -d -m 700 /etc/wireguard
  umask 077
  sudo tee "$LOCAL_CONF" >/dev/null <<EOF
[Interface]
Address = ${CLIENT_ADDR}
PrivateKey = ${client_priv}
Table = off
MTU = 1420

[Peer]
PublicKey = ${server_pub}
PresharedKey = ${psk}
Endpoint = ${endpoint_ip}:${LISTEN_PORT}
AllowedIPs = 0.0.0.0/0
PersistentKeepalive = 25
EOF

  sudo tee "$ROUTE_SCRIPT" >/dev/null <<EOF
#!/usr/bin/env bash
set -euo pipefail
IFACE="${IFACE}"
TARGET_HOST="${TARGET_HOST}"
STATE="${ROUTE_STATE}"

if ! ip link show "\$IFACE" >/dev/null 2>&1; then
  exit 0
fi

mapfile -t new_ips < <(getent ahostsv4 "\$TARGET_HOST" | awk '{ print \$1 }' | sort -u)
if [ "\${#new_ips[@]}" -eq 0 ]; then
  echo "could not resolve \$TARGET_HOST" >&2
  exit 1
fi

old_ips=()
if [ -f "\$STATE" ]; then
  mapfile -t old_ips < "\$STATE"
fi

for old in "\${old_ips[@]}"; do
  keep=0
  for ip in "\${new_ips[@]}"; do
    [ "\$old" = "\$ip" ] && keep=1
  done
  if [ "\$keep" -eq 0 ]; then
    ip route del "\$old/32" dev "\$IFACE" 2>/dev/null || true
  fi
done

for ip in "\${new_ips[@]}"; do
  ip route replace "\$ip/32" dev "\$IFACE"
done

printf '%s\n' "\${new_ips[@]}" >"\$STATE"
EOF
  sudo chmod 700 "$ROUTE_SCRIPT"

  sudo tee "$SERVICE" >/dev/null <<EOF
[Unit]
Description=Refresh ${TARGET_HOST} routes through ${IFACE}
After=wg-quick@${IFACE}.service
Wants=wg-quick@${IFACE}.service

[Service]
Type=oneshot
ExecStart=${ROUTE_SCRIPT}
EOF

  sudo tee "$TIMER" >/dev/null <<EOF
[Unit]
Description=Refresh ${TARGET_HOST} WireGuard route

[Timer]
OnBootSec=20s
OnUnitActiveSec=5m
Unit=${IFACE}-routes.service

[Install]
WantedBy=timers.target
EOF

  sudo systemctl daemon-reload
  sudo systemctl enable --now "wg-quick@${IFACE}"
  sudo systemctl restart "wg-quick@${IFACE}"
  sudo systemctl enable --now "${IFACE}-routes.timer"
  sudo systemctl start "${IFACE}-routes.service"

  echo "installed ${IFACE}; ${TARGET_HOST} now routes through ${endpoint_ip}:${LISTEN_PORT}"
}

status() {
  sudo wg show "$IFACE" || true
  systemctl is-active "wg-quick@${IFACE}" || true
  systemctl is-active "${IFACE}-routes.timer" || true
  getent ahostsv4 "$TARGET_HOST" | awk '{ print $1 }' | sort -u | while read -r ip; do
    [ -n "$ip" ] && ip route get "$ip" || true
  done
}

test_route() {
  status
  echo
  echo "Public IP when forced over ${IFACE}:"
  curl -sS --interface "$IFACE" https://ipinfo.io/json || true
  echo
  echo "Sakana API status through current route:"
  local key
  key="$(node -e 'const fs=require("fs"); const p=process.env.HOME+"/.local/share/opencode/auth.json"; const j=JSON.parse(fs.readFileSync(p,"utf8")); process.stdout.write(j.fugu?.key||"")' 2>/dev/null || true)"
  if [ -z "$key" ]; then
    echo "no fugu key found in OpenCode auth"
    return
  fi
  curl -sS -D - -o /tmp/sakana-wg-test-body.txt \
    -X POST "https://${TARGET_HOST}/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${key}" \
    -d '{"model":"fugu","messages":[{"role":"user","content":"Reply ok"}]}' |
    sed -n '1,20p'
  sed -n '1,5p' /tmp/sakana-wg-test-body.txt
}

down() {
  sudo systemctl disable --now "${IFACE}-routes.timer" 2>/dev/null || true
  sudo systemctl stop "${IFACE}-routes.service" 2>/dev/null || true
  sudo wg-quick down "$IFACE" 2>/dev/null || true
}

case "${1:-}" in
  install)
    [ $# -ge 2 ] || { usage >&2; exit 1; }
    install_remote "$2" "${3:-}"
    ;;
  status)
    status
    ;;
  test)
    test_route
    ;;
  down)
    down
    ;;
  -h|--help|help|"")
    usage
    ;;
  *)
    usage >&2
    exit 1
    ;;
esac
