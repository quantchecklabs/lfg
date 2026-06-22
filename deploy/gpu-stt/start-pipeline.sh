#!/usr/bin/env bash
# Bring up the CURRENT English voice pipeline on the box, idempotently:
#   Whisper STT  -> :8087 (tmux `stt`,        run-whisper.sh)
#   Chatterbox   -> :8090 (tmux `chatterbox`, run-chatterbox.sh)
# Used by lifecycle.sh ensure_service on every scheduled start. Safe to re-run.
export PATH=/opt/conda/bin:$PATH
APP=/opt/stt

check() {
  /opt/conda/bin/python -c "import urllib.request,sys
try:
    urllib.request.urlopen('http://127.0.0.1:'+sys.argv[1]+'/health',timeout=3)
except Exception:
    sys.exit(1)" "$1" 2>/dev/null
}

# Whisper STT :8087
if ! check 8087; then
  echo "[pipeline] starting whisper :8087"
  tmux kill-session -t stt 2>/dev/null || true
  tmux new-session -d -s stt "bash $APP/run-whisper.sh > $APP/whisper.log 2>&1"
  for i in $(seq 1 40); do check 8087 && break; sleep 2; done
fi
check 8087 && echo "[pipeline] whisper :8087 UP" || echo "[pipeline] whisper FAILED"

# Chatterbox TTS :8090
if ! check 8090; then
  echo "[pipeline] starting chatterbox :8090"
  tmux kill-session -t chatterbox 2>/dev/null || true
  tmux new-session -d -s chatterbox "bash $APP/run-chatterbox.sh > $APP/chatterbox.log 2>&1"
  for i in $(seq 1 60); do check 8090 && break; sleep 2; done
fi
check 8090 && echo "[pipeline] chatterbox :8090 UP" || echo "[pipeline] chatterbox FAILED (lazy load?)"

echo "[pipeline] done"
