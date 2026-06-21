#!/usr/bin/env python3
"""Cantonese STT host for lfg — SenseVoiceSmall (FunASR) on the Novita 5090.

Honors the exact contract lfg-serve's /api/voice/stt proxy speaks:

    POST /stt
      Content-Type: application/octet-stream   (a WAV file; any rate/channels)
      Authorization: Bearer $STT_TOKEN
    -> 200 { "text": "..." }
    GET /health -> { ok, model, lang, device }

SenseVoiceSmall is non-autoregressive (very low latency), natively supports
Cantonese (yue) + English code-switch, Apache-2.0. We resample to 16 kHz mono
here so the lfg frontend/proxy stay unchanged, and optionally convert the
output to Hong Kong traditional characters via OpenCC (s2hk).

Env:
  STT_TOKEN            required; shared secret, must match lfg's STT_TOKEN
  STT_MODEL            default iic/SenseVoiceSmall
  STT_LANG             default yue (Cantonese); auto|zh|en|yue|ja|ko
  STT_DEVICE           default cuda:0
  STT_TO_TRADITIONAL   default 1 -> convert simplified->HK traditional
  STT_HOST / STT_PORT  default 0.0.0.0 / 8087
"""
import io
import os
import tempfile
from math import gcd

import numpy as np
import soundfile as sf
from fastapi import FastAPI, Header, HTTPException, Request
from scipy.signal import resample_poly

MODEL_NAME = os.environ.get("STT_MODEL", "FunAudioLLM/SenseVoiceSmall")
LANG = os.environ.get("STT_LANG", "yue")
DEVICE = os.environ.get("STT_DEVICE", "cuda:0")
TOKEN = os.environ.get("STT_TOKEN")
TO_TRAD = os.environ.get("STT_TO_TRADITIONAL", "1") == "1"
TARGET_SR = 16000

if not TOKEN:
    raise SystemExit("STT_TOKEN is required")

app = FastAPI()
_model = None
_cc = None


def model():
    global _model
    if _model is None:
        from funasr import AutoModel
        from huggingface_hub import snapshot_download

        # Download the repo (config.yaml points at the built-in SenseVoiceSmall
        # class) and load from the local dir — avoids the modelscope remote-code
        # import dance that fails with "No module named 'model'".
        local_dir = snapshot_download(MODEL_NAME)
        _model = AutoModel(
            model=local_dir,
            disable_update=True,
            device=DEVICE,
        )
    return _model


def cc():
    global _cc
    if _cc is None and TO_TRAD:
        from opencc import OpenCC

        _cc = OpenCC("s2hk")
    return _cc


def to_16k_mono(raw: bytes) -> np.ndarray:
    data, sr = sf.read(io.BytesIO(raw), dtype="float32", always_2d=True)
    mono = data.mean(axis=1)
    if sr != TARGET_SR:
        g = gcd(int(sr), TARGET_SR)
        mono = resample_poly(mono, TARGET_SR // g, int(sr) // g).astype(np.float32)
    return mono


@app.get("/health")
def health():
    return {"ok": True, "model": MODEL_NAME, "lang": LANG, "device": DEVICE}


@app.post("/stt")
async def stt(request: Request, authorization: str = Header(default="")):
    if authorization != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="unauthorized")
    raw = await request.body()
    if not raw:
        raise HTTPException(status_code=400, detail="empty audio")
    try:
        audio = to_16k_mono(raw)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"bad audio: {e}")
    if audio.size == 0:
        return {"text": ""}

    from funasr.utils.postprocess_utils import rich_transcription_postprocess

    with tempfile.NamedTemporaryFile(suffix=".wav") as f:
        sf.write(f.name, audio, TARGET_SR, subtype="PCM_16")
        res = model().generate(
            input=f.name, cache={}, language=LANG, use_itn=True, batch_size_s=60
        )
    text = rich_transcription_postprocess(res[0]["text"]) if res else ""
    if TO_TRAD and text and cc():
        text = cc().convert(text)
    return {"text": (text or "").strip()}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("STT_HOST", "0.0.0.0"),
        port=int(os.environ.get("STT_PORT", "8087")),
    )
