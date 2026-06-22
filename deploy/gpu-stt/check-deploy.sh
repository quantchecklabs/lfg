#!/usr/bin/env bash
# Runs ON the box. Quick status probe for the Parakeet deploy.
echo "=== deploy.log tail ==="
tail -n 25 /opt/stt/deploy.log 2>/dev/null || echo "(no deploy.log)"
echo "=== tmux ==="
tmux ls 2>&1 || true
echo "=== parakeet :8091 health ==="
curl -s -m 4 http://127.0.0.1:8091/health 2>/dev/null || echo "DOWN"
