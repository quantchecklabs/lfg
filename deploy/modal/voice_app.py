# lfg voice on Modal — serverless Chatterbox TTS + Whisper STT.
#
# PROTOTYPE. Goal: prove the orb's voice can run on Modal serverless GPU instead
# of the always-on Novita box — killing the capacity/tunnel/timer machinery — and
# measure REAL cold-start + synth latency before committing.
#
# Exposes one token-protected web endpoint matching serve's existing proxy
# contract so lfg can point at it with a one-line .env change:
#   GET  /health            -> {"ok": true}
#   POST /tts  {text,voice} -> audio/wav            (Chatterbox)
#   POST /stt  <wav bytes>  -> {"text": "..."}      (faster-whisper)
#
# Deploy:  modal deploy deploy/modal/voice_app.py   (see deploy/modal/README.md)
#
# Cost note (L4 @ $0.000222/s): ~4s GPU/turn → ~$0.001/turn. Scale-to-zero, so
# you pay ~nothing when idle. enable_memory_snapshot keeps cold starts ~1-3s.
import io
import os
import time

import modal

GPU = os.environ.get("LFG_MODAL_GPU", "L4")  # L4 (cheapest, 24GB) | A10G | A100-40GB
WHISPER_SIZE = os.environ.get("LFG_WHISPER_SIZE", "small")  # tiny|base|small|medium

app = modal.App("lfg-voice")

# Bake model weights into the image at build time so cold starts only pay for the
# VRAM load, never a download. Chatterbox pulls from HF on first import; we warm
# the HF cache during the build via run_function below.
image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg", "git")
    .pip_install(
        "chatterbox-tts",            # Resemble AI Chatterbox (English)
        "faster-whisper",            # fast CTranslate2 Whisper
        "soundfile",
        "numpy<2",
        "fastapi[standard]",
        "torch",
        "torchaudio",
    )
)


def _prefetch():
    # Runs at image BUILD: download Chatterbox + Whisper weights into the image
    # layer so they're present (cached) before any request.
    from chatterbox.tts import ChatterboxTTS  # noqa
    from faster_whisper import WhisperModel  # noqa

    ChatterboxTTS.from_pretrained(device="cpu")
    WhisperModel(WHISPER_SIZE, device="cpu", compute_type="int8")


image = image.run_function(_prefetch)

# Optional token auth — create with: modal secret create lfg-voice TTS_TOKEN=...
# (falls back to open if the secret/env var is absent, fine for a prototype probe)
try:
    _secret = modal.Secret.from_name("lfg-voice")
    _secrets = [_secret]
except Exception:
    _secrets = []


@app.cls(
    image=image,
    gpu=GPU,
    secrets=_secrets,
    # GPU memory snapshot abandoned for now: it segfaulted on restore (exit 139)
    # and fell back to a full reload, so it only *added* latency. Cold start is a
    # clean ~25s model load; snappiness comes from keeping a container warm
    # (min_containers / the orb long-press), not from snapshots.
    scaledown_window=300,          # stay warm 5 min after last request, then →0
    min_containers=0,              # scale-to-zero (long-press the orb to keep warm)
    timeout=120,
)
@modal.concurrent(max_inputs=4)
class Voice:
    @modal.enter()
    def load(self):
        import io
        import soundfile as sf
        import torch
        from chatterbox.tts import ChatterboxTTS
        from faster_whisper import WhisperModel

        t0 = time.time()
        self.dev = "cuda" if torch.cuda.is_available() else "cpu"
        self.tts = ChatterboxTTS.from_pretrained(device=self.dev)
        self.stt = WhisperModel(
            WHISPER_SIZE,
            device=self.dev,
            compute_type="float16" if self.dev == "cuda" else "int8",
        )
        # Warm the graphs/kernels so the first real request isn't the slow one.
        try:
            wav = self.tts.generate("warming up the voice engine")
            buf = io.BytesIO()
            sf.write(buf, wav.squeeze(0).cpu().numpy(), self.tts.sr, format="WAV")
            buf.seek(0)
            list(self.stt.transcribe(buf, beam_size=1)[0])
        except Exception as e:
            print(f"[voice] warmup skipped: {e}", flush=True)
        print(f"[voice] loaded+warmed on {self.dev} in {time.time()-t0:.1f}s", flush=True)

    @modal.asgi_app()
    def web(self):
        import soundfile as sf
        from fastapi import FastAPI, Header, HTTPException, Request, Response

        api = FastAPI()
        token = os.environ.get("TTS_TOKEN", "")

        def auth(authorization: str | None):
            if token and authorization != f"Bearer {token}":
                raise HTTPException(status_code=401, detail="bad token")

        @api.get("/health")
        def health():
            return {"ok": True}

        @api.post("/tts")
        async def tts(req: Request, authorization: str | None = Header(default=None)):
            auth(authorization)
            import numpy as np
            import torchaudio.functional as AF

            body = await req.json()
            text = (body.get("text") or "").strip()
            if not text:
                raise HTTPException(status_code=400, detail="expected {text}")
            t0 = time.time()
            wav = self.tts.generate(text).squeeze(0)  # [n] @ self.tts.sr
            if int(self.tts.sr) != 24000:
                wav = AF.resample(wav, int(self.tts.sr), 24000)
            arr = wav.clamp(-1, 1).cpu().numpy()
            dur = time.time() - t0
            # The lfg worker (LfgTTS) wants RAW 24 kHz mono int16 PCM — no WAV
            # header — because it pushes the bytes straight into an output_emitter
            # initialized at sample_rate=24000, mime audio/pcm. A WAV header (or a
            # non-24k rate) plays as a click + wrong-pitch audio. `?wav=1` returns
            # a WAV instead, for easy curl/browser testing.
            if req.query_params.get("wav"):
                buf = io.BytesIO()
                sf.write(buf, arr, 24000, format="WAV")
                payload, mime = buf.getvalue(), "audio/wav"
            else:
                payload = (arr * 32767.0).astype("<i2").tobytes()
                mime = "audio/pcm"
            print(f"[tts] {len(text)} chars -> {len(payload)}B {mime} in {dur:.2f}s", flush=True)
            return Response(
                content=payload,
                media_type=mime,
                headers={"X-Synth-Seconds": f"{dur:.3f}", "Cache-Control": "no-store"},
            )

        @api.post("/stt")
        async def stt(req: Request, authorization: str | None = Header(default=None)):
            auth(authorization)
            audio = await req.body()
            if not audio:
                raise HTTPException(status_code=400, detail="expected audio bytes")
            t0 = time.time()
            segments, _ = self.stt.transcribe(io.BytesIO(audio), beam_size=1)
            text = "".join(s.text for s in segments).strip()
            print(f"[stt] -> {text[:60]!r} in {time.time()-t0:.2f}s", flush=True)
            return {"text": text}

        return api
