#!/usr/bin/env bash
# Idempotent provisioning for the Cantonese STT host (SenseVoice/FunASR).
# Runs ON the GPU box. Safe to re-run.
set -euo pipefail
export PATH=/opt/conda/bin:$PATH
export DEBIAN_FRONTEND=noninteractive

APP=/opt/stt
mkdir -p "$APP"

echo "[provision] installing python deps..."
pip install -q --no-input \
  funasr soundfile scipy "numpy<2" librosa \
  huggingface_hub fastapi "uvicorn[standard]" opencc

echo "[provision] writing launcher..."
cat > "$APP/run.sh" <<'SH'
#!/usr/bin/env bash
set -a; [ -f /opt/stt/stt.env ] && . /opt/stt/stt.env; set +a
export PATH=/opt/conda/bin:$PATH
exec python /opt/stt/server.py
SH
chmod +x "$APP/run.sh"

echo "[provision] pre-downloading SenseVoiceSmall (so first request is fast)..."
python - <<'PY'
from huggingface_hub import snapshot_download
from funasr import AutoModel
d = snapshot_download("FunAudioLLM/SenseVoiceSmall")
AutoModel(model=d, disable_update=True, device="cuda:0")
print("model cached OK")
PY

echo "[provision] starting service under tmux..."
command -v tmux >/dev/null || (export DEBIAN_FRONTEND=noninteractive; apt-get install -y -qq tmux)
tmux kill-session -t stt 2>/dev/null || true
tmux new-session -d -s stt "bash $APP/run.sh > $APP/server.log 2>&1"
sleep 4
grep -q "Uvicorn running" "$APP/server.log" && echo "[provision] service up on :8087" || { echo "[provision] FAILED to start"; tail "$APP/server.log"; exit 1; }

echo "[provision] done."
