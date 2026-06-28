#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# One-shot deploy for Voice Option B (ElevenLabs managed agent + our brain).
#
# Idempotent and re-runnable. Walks the full bring-up as a checklist and STOPS
# with a clear instruction at the ONE step a human must do (enable Tailscale
# Funnel — a single click). Re-run after the click and it finishes on its own.
#
#   bash deploy/voice/eleven-deploy.sh
#
# What it does:
#   1. preflight   — .env keys, serve up, bearer gate (401/200) on 127.0.0.1
#   2. agent       — ensure data/eleven-agent.json (provision if missing)
#   3. funnel      — raise public mount for ONLY /v1/chat/completions on :8443
#                    (gates here with the enable link if Funnel is off)
#   4. self-heal   — PATCH the agent's custom_llm.url to the live MagicDNS host
#   5. verify      — public 401-without-bearer + streamed-reply-with-bearer
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
PORT=8443
LLM_PATH="/v1/chat/completions"
LOCAL="http://127.0.0.1:8766${LLM_PATH}"

green() { printf '\033[32m✓\033[0m %s\n' "$*"; }
warn()  { printf '\033[33m→\033[0m %s\n' "$*"; }
fail()  { printf '\033[31m✗ %s\033[0m\n' "$*"; }
hr()    { printf '\033[2m%s\033[0m\n' "────────────────────────────────────────────────────────"; }
die()   { fail "$*"; exit 1; }

envval() { grep "^$1=" "$ROOT/.env" 2>/dev/null | head -1 | cut -d= -f2-; }

echo; printf '\033[1mVoice Option B — one-shot deploy\033[0m\n'; hr

# ── 1. preflight ─────────────────────────────────────────────────────────────
echo "1) preflight"
command -v tailscale >/dev/null || die "tailscale CLI not found"
command -v node >/dev/null || die "node not found"
command -v bun  >/dev/null || die "bun not found"

KEY="$(envval ELEVENLABS_API_KEY)";  [ -n "$KEY" ]    || die ".env missing ELEVENLABS_API_KEY"
SECRET="$(envval LFG_ELEVEN_LLM_SECRET)"; [ -n "$SECRET" ] || die ".env missing LFG_ELEVEN_LLM_SECRET"
green "ELEVENLABS_API_KEY + LFG_ELEVEN_LLM_SECRET present"

if ! curl -sS -o /dev/null --max-time 4 "http://127.0.0.1:8766/api/voice/config"; then
  die "lfg-serve not responding on :8766 — start it (systemctl --user start lfg-serve.service)"
fi
green "lfg-serve is up on :8766"

code_noauth="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 8 -X POST "$LOCAL" \
  -H 'content-type: application/json' -d '{"messages":[{"role":"user","content":"hi"}]}')"
[ "$code_noauth" = "401" ] || die "bearer gate not active locally (got HTTP $code_noauth, want 401) — restart serve after the latest edits"
got="$(curl -sS --max-time 25 -X POST "$LOCAL" -H "authorization: Bearer $SECRET" \
  -H 'content-type: application/json' -d '{"messages":[{"role":"user","content":"say hi in three words"}]}' \
  | grep -o '"content":"[^"]*"' | head -1)"
[ -n "$got" ] || die "brain endpoint returned no content with a valid bearer"
green "bearer gate OK (401 without, streams with) — brain says: ${got}"

# ── 2. agent ─────────────────────────────────────────────────────────────────
echo; echo "2) agent"
if [ ! -f "$ROOT/data/eleven-agent.json" ]; then
  warn "no data/eleven-agent.json — provisioning agent via ElevenLabs API…"
  bun deploy/voice/provision-eleven-agent.ts create || die "agent provisioning failed"
fi
AGENT_ID="$(node -pe 'require("./data/eleven-agent.json").agent_id')"
green "agent: $AGENT_ID"

# ── 3. funnel (human gate) ───────────────────────────────────────────────────
echo; echo "3) public ingress (Tailscale Funnel, /v1/chat/completions only on :$PORT)"
TARGET="http://127.0.0.1:8766${LLM_PATH}"
# Detached stdio (tempfile + </dev/null) so the backgrounded funnel child can't
# hold this script's stdout open; timeout is a belt-and-suspenders safety net.
ftmp="$(mktemp)"
timeout 15 tailscale funnel --bg --https="$PORT" --set-path="$LLM_PATH" "$TARGET" </dev/null >"$ftmp" 2>&1
rc=$?
out="$(cat "$ftmp" 2>/dev/null)"; rm -f "$ftmp"
if echo "$out" | grep -qiE 'not enabled|login\.tailscale\.com/f/funnel'; then
  link="$(echo "$out" | grep -oE 'https://login\.tailscale\.com/f/funnel[^ ]*' | head -1)"
  echo
  fail "Tailscale Funnel is not enabled on this tailnet."
  hr
  printf '\033[1m  MANUAL STEP (one click), then re-run this script:\033[0m\n'
  printf '    %s\n' "${link:-https://login.tailscale.com/f/funnel}"
  hr
  exit 2
fi
[ $rc -eq 0 ] || warn "funnel command returned $rc (continuing): $out"
green "funnel mount raised on :$PORT"

HOST="$(tailscale status --json | node -pe 'JSON.parse(require("fs").readFileSync(0)).Self.DNSName.replace(/\.$/,"")')"
[ -n "$HOST" ] || die "could not resolve MagicDNS host"
BASE="https://${HOST}:${PORT}"
PUB="${BASE}${LLM_PATH}"
green "public base: $BASE"

# ── 4. self-heal agent URL ───────────────────────────────────────────────────
echo; echo "4) reconcile agent custom_llm.url"
LFG_ELEVEN_LLM_URL="$BASE" bun deploy/voice/provision-eleven-agent.ts seturl "$BASE" || warn "could not reconcile agent url (check manually)"

# ── 5. verify public reachability ────────────────────────────────────────────
echo; echo "5) verify public reachability (ElevenLabs' path)"
sleep 2
pub_noauth="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 12 -X POST "$PUB" \
  -H 'content-type: application/json' -d '{"messages":[{"role":"user","content":"hi"}]}' 2>/dev/null || echo 000)"
if [ "$pub_noauth" = "401" ]; then green "public endpoint reachable + gated (401 without bearer)"
else warn "public no-auth returned HTTP $pub_noauth (want 401) — funnel may still be propagating; re-run in ~30s"; fi
pub_reply="$(curl -sS --max-time 30 -X POST "$PUB" -H "authorization: Bearer $SECRET" \
  -H 'content-type: application/json' -d '{"messages":[{"role":"user","content":"say hi in four words"}]}' 2>/dev/null \
  | grep -o '"content":"[^"]*"' | head -1)"
[ -n "$pub_reply" ] && green "public brain reply: $pub_reply" || warn "no public reply yet — re-run in ~30s if funnel just came up"

# ── done ─────────────────────────────────────────────────────────────────────
echo; hr; printf '\033[1mDEPLOYED\033[0m\n'
echo "  agent_id : $AGENT_ID"
echo "  brain url: $PUB"
echo "  revoke   : tailscale funnel reset"
echo
echo "  Next: cut the frontend over to useElevenVoice() (web/src/eleven-voice.ts),"
echo "        npm --prefix web run build, then tap the orb and talk."
hr
