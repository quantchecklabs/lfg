#!/usr/bin/env bash
# Runs ON the box. Launches the (slow) bootstrap detached so it survives the
# controlling ssh closing. Idempotent: re-running just starts another bootstrap,
# which is itself idempotent (health-gated).
set -euo pipefail
cd /opt/stt
nohup setsid bash /opt/stt/bootstrap-remote.sh > /opt/stt/deploy.log 2>&1 < /dev/null &
echo "deploy launched, pid $!"
