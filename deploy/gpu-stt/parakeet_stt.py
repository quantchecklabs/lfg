#!/usr/bin/env python3
"""NVIDIA Parakeet STT for lfg — runs ON the Novita 5090, alongside SenseVoice.

Same contract lfg-serve's /api/voice/stt proxy speaks (octet-stream WAV in,
Bearer auth, { "text": ... } out), so the frontend/proxy stay unchanged. This
is the *English* (or multilingual-v3) option; SenseVoice on :8087 remains the
Cantonese default. Pick which one the orb uses by pointing STT_UPSTREAM at the
matching tunnel port (:8087 SenseVoice, :8089 Parakeet).

Env:
  STT_TOKEN     required; shared secret, must match lfg's STT_TOKEN
  STT_MODEL     default nvidia/parakeet-tdt-0.6b-v2 (English).
                Use nvidia/parakeet-tdt-0.6b-v3 for multilingual (~25 langs).
  STT_DEVICE    cuda (default) | cpu
  STT_HOST      bind host, default 0.0.0.0
  PARAKEET_PORT bind port, default 8091 (:8087-8090 are taken by other engines)
"""
import io
import os
import tempfile
from math import gcd

import numpy as np
import soundfile as sf
import torch
from fastapi import FastAPI, Header, HTTPException, Request
from scipy.signal import resample_poly

MODEL_NAME = os.environ.get("STT_MODEL", "nvidia/parakeet-tdt-0.6b-v2")
DEVICE = os.environ.get("STT_DEVICE", "cuda" if torch.cuda.is_available() else "cpu")
TOKEN = os.environ.get("STT_TOKEN")
TARGET_SR = 16000

if not TOKEN:
    raise SystemExit("STT_TOKEN is required")

app = FastAPI()
_model = None


def model():
    global _model
    if _model is None:
        import nemo.collections.asr as nemo_asr

        m = nemo_asr.models.ASRModel.from_pretrained(model_name=MODEL_NAME)
        _model = m.to(DEVICE).eval()
    return _model


@app.on_event("startup")
def _warm():
    """Load + autotune in a background thread so the first real request is hot."""
    import threading

    def go():
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav") as f:
                sf.write(f.name, np.zeros(TARGET_SR, dtype="float32"), TARGET_SR, subtype="PCM_16")
                with torch.inference_mode():
                    model().transcribe([f.name], batch_size=1)
        except Exception:
            pass

    threading.Thread(target=go, daemon=True).start()


def to_16k_mono(raw: bytes) -> np.ndarray:
    data, sr = sf.read(io.BytesIO(raw), dtype="float32", always_2d=True)
    mono = data.mean(axis=1)
    if sr != TARGET_SR:
        g = gcd(int(sr), TARGET_SR)
        mono = resample_poly(mono, TARGET_SR // g, int(sr) // g).astype(np.float32)
    return mono


@app.get("/health")
def health():
    return {"ok": True, "engine": "parakeet", "model": MODEL_NAME, "device": DEVICE, "warm": _model is not None}


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

    with tempfile.NamedTemporaryFile(suffix=".wav") as f:
        sf.write(f.name, audio, TARGET_SR, subtype="PCM_16")
        with torch.inference_mode():
            out = model().transcribe([f.name], batch_size=1)
    first = out[0] if out else ""
    text = getattr(first, "text", first)
    return {"text": (text or "").strip()}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("STT_HOST", "0.0.0.0"),
        port=int(os.environ.get("PARAKEET_PORT", "8091")),
    )
