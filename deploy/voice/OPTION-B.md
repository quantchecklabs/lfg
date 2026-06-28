# Voice Option B — ElevenLabs managed agent + our brain (custom LLM)

Replaces the self-hosted LiveKit worker (`deploy/voice/agent.py`) with ElevenLabs'
**managed agent**. ElevenLabs owns mic capture, STT (Scribe), turn-taking and TTS;
**our backend stays the brain** via a custom-LLM endpoint. No persistent shared
LiveKit room and no `participant_connected` handler → the duplicate-session race
is gone by construction.

```
browser ──(WebRTC, @elevenlabs/client)──> ElevenLabs managed agent
                                                │  (per user turn)
                                                ▼
                       POST {funnel}/v1/chat/completions   ← bearer-gated
                                                │
                                   src/voice-eleven-llm.ts  (Haiku brain + fleet tools)
                                                │  fetch 127.0.0.1:8766/api/...
                                                ▼
                                   existing lfg fleet endpoints (scoped to user_id)
```

## Pieces

| Piece | Where |
|---|---|
| Custom-LLM brain (OpenAI-compatible SSE, runs fleet tool loop) | `src/voice-eleven-llm.ts` → `handleElevenLlm` |
| Route `POST /v1/chat/completions` (bearer-gated) | `src/commands/serve.ts` |
| Per-connect WebRTC token mint `GET /api/voice/eleven-token` | `src/commands/serve.ts` → `handleElevenToken` |
| Agent provisioning via ElevenLabs API | `deploy/voice/provision-eleven-agent.ts` |
| Browser connector + React hook | `web/src/eleven-voice.ts` |
| Public ingress helper | `deploy/voice/eleven-funnel.sh` |

## Config (.env)

- `ELEVENLABS_API_KEY` — already present.
- `LFG_ELEVEN_LLM_SECRET` — shared bearer; ElevenLabs presents it, the endpoint checks it. (auto-added)
- `LFG_ELEVEN_LLM_URL` — public base ElevenLabs POSTs to (default `https://<magicdns>:8443`).
- `LFG_ELEVEN_AGENT_ID` — optional; otherwise read from `data/eleven-agent.json`.

## Public ingress — cloudflare (shipped, no Tailscale change)

Tailscale Funnel was blocked (tagged node needs an admin ACL grant of `funnel`
to `tag:dev`). Shipped via cloudflared instead. Two `systemd --user` services:

| Service | Role |
|---|---|
| `lfg-eleven-proxy.service` | `deploy/voice/eleven-edge-proxy.ts` — loopback proxy that forwards ONLY `POST /v1/chat/completions` to :8766, 404s everything else |
| `lfg-eleven-tunnel.service` | `deploy/voice/eleven-tunnel.sh` — cloudflared quick tunnel → edge proxy; extracts the public URL and repoints the agent's `custom_llm.url` |

Both `Restart=always`. The trycloudflare URL is **ephemeral** — it rotates when
cloudflared restarts — but the tunnel script re-extracts it and re-points the
agent automatically on every (re)start, so it self-heals. Current URL is in
`data/eleven-tunnel-url.txt`. For a stable URL later, switch to a named tunnel
(needs a Cloudflare account) or the Tailscale Funnel ACL grant.

Tear down:  `systemctl --user disable --now lfg-eleven-tunnel lfg-eleven-proxy`

## Security

The custom-LLM endpoint is internet-reachable (so ElevenLabs can call it) and can
drive fleet tools, so it is **bearer-gated** with `LFG_ELEVEN_LLM_SECRET` and the
funnel mounts **only** `/v1/chat/completions` — never the rest of `/api`. If the
secret is unset the endpoint fails closed (503). Revoke exposure any time with
`tailscale funnel reset`.

## Bring-up — one shot

```
bash deploy/voice/eleven-deploy.sh
```

Idempotent, re-runnable. It does preflight (env + serve + bearer gate), ensures
the agent exists (provisions if missing), raises the funnel, self-heals the
agent's `custom_llm.url`, and verifies public reachability. It **stops at exactly
one place** — if Tailscale Funnel isn't enabled it prints the one-click enable
link and exits 2. Click it, re-run, and it finishes on its own.

The frontend is already wired: the orb (`web/src/voice-orb.tsx`) routes through the
managed agent when the opt-in flag is set, and falls back to LiveKit otherwise.
Flip it per-browser in the console (no rebuild needed):

```
localStorage.setItem("lfg_voice_eleven","1")   // managed agent (Option B)
localStorage.removeItem("lfg_voice_eleven")     // LiveKit (default)
```

Then just tap the orb and talk. (`web/src/eleven-voice.ts` also exports a
standalone `useElevenVoice()` hook if you want a dedicated button elsewhere.)

Sub-tools the deploy script uses (also runnable standalone):
- `bun deploy/voice/provision-eleven-agent.ts create|show|delete|seturl <url>`
- `bash deploy/voice/eleven-funnel.sh` (funnel mount + verify only)
- revoke public exposure: `tailscale funnel reset`

## Known follow-up

`consult_advisor` (Opus) answers out-of-band in the LiveKit worker. ElevenLabs
custom-LLM is request/response and can't push unsolicited audio mid-call, so v1
fires the consult in the background and returns a holding line. Delivering the
answer back into the live call (ElevenLabs contextual-update / injected turn) is
the one open item.
