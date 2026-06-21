#!/usr/bin/env bash
# Install CosyVoice2-0.5B (Cantonese TTS) on the GPU box, isolated from the STT
# env. Keeps the box's Blackwell cu128 torch (CosyVoice's pinned torch would
# downgrade off sm_120). Idempotent-ish; safe to re-run.
set -uo pipefail
export PATH=/opt/conda/bin:$PATH
export DEBIAN_FRONTEND=noninteractive

echo "[tts] system deps"
apt-get update -qq >/dev/null 2>&1 || true
apt-get install -y -qq git sox ffmpeg >/dev/null 2>&1 || true

cd /opt
if [ ! -d CosyVoice ]; then
  echo "[tts] cloning CosyVoice"
  git clone --recursive https://github.com/FunAudioLLM/CosyVoice.git || exit 1
fi
cd /opt/CosyVoice
git submodule update --init --recursive 2>/dev/null || true

echo "[tts] pynini (conda-forge; pip build is painful)"
python -c "import pynini" 2>/dev/null || conda install -y -c conda-forge "pynini==2.1.6" >/dev/null 2>&1 || pip install -q "pynini==2.1.6" || true

echo "[tts] python deps WITHOUT touching torch/torchaudio (keep cu128 Blackwell build)"
grep -viE '^torch([=<>]|audio|vision)|^onnxruntime-gpu' requirements.txt > /tmp/cosy_req.txt || cp requirements.txt /tmp/cosy_req.txt
pip install -q -r /tmp/cosy_req.txt 2>&1 | tail -3 || pip install -q -r /tmp/cosy_req.txt 2>&1 | tail -3
pip install -q WeTextProcessing modelscope 2>/dev/null || true

echo "[tts] download CosyVoice2-0.5B"
python - <<'PY'
from modelscope import snapshot_download
snapshot_download('iic/CosyVoice2-0.5B', local_dir='/opt/CosyVoice/pretrained_models/CosyVoice2-0.5B')
print("[tts] model downloaded")
PY

echo "[tts] build a 16k Cantonese reference clip from the SenseVoice example"
python - <<'PY'
import glob, soundfile as sf, numpy as np
from math import gcd
from scipy.signal import resample_poly
ex = glob.glob('/root/.cache/huggingface/**/example/yue.mp3', recursive=True)
src = ex[0]
try:
    import librosa; a, sr = librosa.load(src, sr=16000, mono=True)
except Exception:
    a, sr = sf.read(src, dtype='float32', always_2d=True); a = a.mean(1)
    if sr != 16000:
        g = gcd(int(sr), 16000); a = resample_poly(a, 16000//g, int(sr)//g)
import os; os.makedirs('/opt/stt', exist_ok=True)
sf.write('/opt/stt/yue_prompt.wav', a.astype('float32'), 16000, subtype='PCM_16')
print('[tts] reference clip ok', round(len(a)/16000, 2), 'sec')
PY

echo "[tts] smoke-load CosyVoice2"
python - <<'PY'
import sys; sys.path.append('/opt/CosyVoice'); sys.path.append('/opt/CosyVoice/third_party/Matcha-TTS')
from cosyvoice.cli.cosyvoice import CosyVoice2
m = CosyVoice2('/opt/CosyVoice/pretrained_models/CosyVoice2-0.5B', load_jit=False, load_trt=False, fp16=True)
print('[tts] CosyVoice2 LOADED OK; sample_rate', m.sample_rate)
PY

echo "[tts] provision-tts done"
