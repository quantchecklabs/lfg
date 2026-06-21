#!/usr/bin/env python3
"""faster-whisper STT for lfg — large-v3-turbo on the Novita 5090 (English).

Same contract as before: POST /stt (octet-stream WAV, Bearer auth) -> { text }.
Resamples to 16 kHz mono. Warms the model at startup so the first real request
isn't a cold cuDNN/cuBLAS autotune.
"""
import io
import os
from math import gcd

import numpy as np
import soundfile as sf
from fastapi import FastAPI, Header, HTTPException, Request
from scipy.signal import resample_poly

TOKEN = os.environ.get("STT_TOKEN")
MODEL = os.environ.get("STT_MODEL", "large-v3-turbo")
LANG = os.environ.get("STT_LANG", "en")
if not TOKEN:
    raise SystemExit("STT_TOKEN is required")

app = FastAPI()
_m = None


def model():
    global _m
    if _m is None:
        from faster_whisper import WhisperModel

        _m = WhisperModel(MODEL, device="cuda", compute_type="float16")
    return _m


@app.on_event("startup")
def _warm():
    import threading

    def go():
        try:
            list(model().transcribe(np.zeros(16000, dtype="float32"), language=LANG, beam_size=1)[0])
        except Exception:
            pass

    threading.Thread(target=go, daemon=True).start()


@app.get("/health")
def health():
    return {"ok": True, "engine": "faster-whisper", "model": MODEL, "lang": LANG, "warm": _m is not None}


@app.post("/stt")
async def stt(request: Request, authorization: str = Header(default="")):
    if authorization != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="unauthorized")
    raw = await request.body()
    if not raw:
        raise HTTPException(status_code=400, detail="empty audio")
    try:
        data, sr = sf.read(io.BytesIO(raw), dtype="float32", always_2d=True)
        a = data.mean(axis=1)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"bad audio: {e}")
    if sr != 16000:
        g = gcd(int(sr), 16000)
        a = resample_poly(a, 16000 // g, int(sr) // g).astype(np.float32)
    if a.size == 0:
        return {"text": ""}
    segs, _ = model().transcribe(a, language=LANG, beam_size=1, vad_filter=False)
    text = "".join(s.text for s in segs).strip()
    return {"text": text}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("STT_PORT", "8087")))
