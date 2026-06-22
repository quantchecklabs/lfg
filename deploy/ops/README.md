# lfg ops — orb TTS failover

Keeps the voice orb's TTS working across the GPU box's daily overnight stop/start.

## Why
The orb's TTS is proxied by `serve` (`/api/voice/tts`) to `TTS_UPSTREAM` in
`.env`. Two engines run on the GPU box:

- **CosyVoice2 `:8088`** — known-good (24 kHz PCM, what the worker's `LfgTTS`
  adapter expects).
- **Chatterbox `:8090`** — the voice-fixes session's engine, mid-migration to a
  streaming variant. While broken it returns HTTP 200 but emits **zero audio**
  and hangs the orb in a stuck "thinking/talking" state.

## What `lfg-tts-failover.sh` does (each morning, after box start)
1. Waits for CosyVoice2 `:8088` to be healthy (starts the box itself if needed).
2. Probes Chatterbox `:8090` — **uses it only if it returns real audio**;
   otherwise points `TTS_UPSTREAM` back at `:8088`.
3. Restarts `serve` and verifies real audio end-to-end through the proxy.
4. Logs to `~/.local/state/lfg-tts-failover.log`.

It edits **only** the `TTS_UPSTREAM` line in `.env` — never the voice-fixes
session's files (`deploy/voice/agent.py`, `src/commands/serve.ts`,
`deploy/gpu-stt/*`, or the on-box `chatterbox_*.py`). It auto-adopts the
Chatterbox fix the moment that deploy lands and produces audio.

## Install (systemd user units)
```sh
ln -sf "$PWD/lfg-tts-failover.service" ~/.config/systemd/user/lfg-tts-failover.service
ln -sf "$PWD/lfg-tts-failover.timer"   ~/.config/systemd/user/lfg-tts-failover.timer
systemctl --user daemon-reload
systemctl --user enable --now lfg-tts-failover.timer
# also ensure the box actually starts each morning:
systemctl --user enable --now lfg-stt-start.timer
```

Schedule: box starts `lfg-stt-start.timer` @ 03:00 UTC; failover runs @ 03:10 UTC
(11:10 HKT).
