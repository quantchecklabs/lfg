"""
lfg voice agent worker (LiveKit Agents 1.6.x).

Pipeline: LiveKit room audio -> (bundled Silero VAD) -> custom STT (lfg
/api/voice/stt, faster-whisper) -> custom LLM (bridges to a dedicated Haiku
Claude Code session via /api/sessions/<id>/send + /stream) -> custom TTS
(lfg /api/voice/tts) -> agent audio track back to the room.

Run:  LIVEKIT_URL=ws://127.0.0.1:7880 LIVEKIT_API_KEY=... LIVEKIT_API_SECRET=... \
      /home/dev/lk-agent/bin/python agent.py dev
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import time
import wave
from pathlib import Path

import aiohttp

from livekit import rtc
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    WorkerOptions,
    cli,
    llm,
    stt,
    tts,
    utils,
)

LFG = os.environ.get("LFG_BASE", "http://127.0.0.1:8766")
CREDS_FILE = Path.home() / ".claude" / ".credentials.json"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
# The voice brain is Haiku (fast, cheap). When a question is genuinely hard or
# risky it consults ONE advisor: a single Opus hop, in-process, sharing the same
# fleet tools so it can both reason AND act. This replaces the old confusing
# setup — a Haiku→Sonnet→Opus ladder AND a separate slow backend advisor session
# — with one clear, fast escalation the user can actually follow.
HAIKU_MODEL = "claude-haiku-4-5"
OPUS_MODEL = "claude-opus-4-8"
SONNET_MODEL = "claude-sonnet-4-6"
# The advisor is Opus; if it's momentarily unavailable (e.g. rate-limited) fall
# back to Sonnet so a consult still returns something useful. Still ONE advisor
# from the user's view — just resilient, not a recursive ladder.
ADVISOR_MODELS = [OPUS_MODEL, SONNET_MODEL]
# Appended to the system prompt when Opus is answering as the advisor, so it
# knows it may ACT on the fleet, not just hand back advice.
ADVISOR_NOTE = (
    "\n\nYou are the advisor: the stronger model the voice assistant consults "
    "when a question is hard or ambiguous or an action is risky. You have the "
    "SAME fleet tools and may ACT directly — reply to a session, answer a "
    "blocked prompt, close a session — in addition to reasoning. Re-read live "
    "state with get_fleet_status / list_sessions before acting, stay within the "
    "speaking user's sessions, and never touch the voice session itself. Answer "
    "in at most 3 short, plain spoken sentences; it is read aloud."
)
VOICE_PROMPT = (
    "You are a hands-free voice assistant inside lfg, a dashboard for managing "
    "AI coding-agent sessions (Claude Code and similar). Reply in at most 1-2 "
    "short, plain spoken sentences. No markdown, no code blocks, no bullet "
    "lists, no symbols meant to be read aloud. Be direct and conversational.\n"
    "WHO YOU SERVE: you are scoped to ONE person — the user speaking to you. The "
    "snapshot and list_sessions show only THEIR sessions, and you act only on "
    "their fleet. 'My project', 'it', 'this', or 'that session' (with no name) "
    "means their currently focused session — resolve it from the FOCUS line in "
    "your context before acting.\n"
    "CRITICAL for speed and not being annoying:\n"
    "- Answer in ONE short sentence. NEVER narrate or preface — no 'let me "
    "check', 'one moment', 'I'm checking'. Just answer.\n"
    "- NEVER claim an action happened until you have seen its tool result. Say "
    "what you are ABOUT to do (present tense), never that it is already done, "
    "until the result comes back — then confirm it, or say it failed.\n"
    "- Do NOT call a tool unless the user CLEARLY asks about session/fleet "
    "status or to act on a specific session. For greetings/small talk, just "
    "reply — no tools.\n"
    "- If what you heard is short, empty, unclear, or garbled, do NOT guess and "
    "do NOT act — briefly ask the user to repeat.\n"
    "- For a SIMPLE ambiguity (which of two sessions did they mean?), just ASK "
    "the user in one short sentence. Do NOT consult the advisor for that.\n"
    "If something needs a long answer or code, give a one-sentence summary and "
    "offer to open it in a session.\n\n"
    "You can act on the fleet with tools. ALWAYS resolve a session to its exact "
    "id (from list_sessions or the snapshot) BEFORE reply_to_session, "
    "answer_session_prompt, or close_session — never act on a guessed id.\n"
    "- get_fleet_status — re-read live status of the user's sessions. The "
    "snapshot in your context goes stale fast: ALWAYS call this first whenever "
    "the user asks what's happening now, the current status, whether a session "
    "finished/changed, or anything time-sensitive. Answer from the fresh "
    "result, not the connect-time snapshot.\n"
    "- list_sessions — get session ids + titles (needed before acting on one).\n"
    "- list_repos — list the projects/repos a new session can start in (name + "
    "path); use it to resolve the folder when the user names a project.\n"
    "- create_session — start a NEW coding-agent session, either to DO a task or "
    "to GO FIND OUT something the user wants to know (spin up an agent to "
    "investigate, e.g. 'check our analytics' or 'see how the auth fix is "
    "going'). Pass a clear one-line `prompt`. It defaults to the user's focused "
    "project; pass `cwd` (from list_repos) only when they name a different one. "
    "You CAN create sessions from voice — do it when the user asks, don't tell "
    "them to use the dashboard.\n"
    "- reply_to_session — send an instruction to another session.\n"
    "- answer_session_prompt — pick an option for a session that is BLOCKED on "
    "a permission/plan prompt (use the option index from its snapshot line).\n"
    "- close_session — shut down / end a session the user is done with. Resolve "
    "the exact id first; never close your own voice session.\n"
    "- consult_advisor — hand a genuinely HARD or RISKY question to a stronger "
    "deep-thinking model that has full repo + tool access and can act on the "
    "fleet itself. Use it ONLY when careful reasoning is truly needed, NOT for "
    "simple disambiguation. It takes a while, so first say one short spoken "
    "sentence telling the user you're checking with the advisor.\n"
    "Prefer answer_session_prompt over reply_to_session when a session is "
    "waiting on a choice. Never act on your own voice session."
)

# Module state for the single active voice job (room "voice", one job at a time).
ROOM: rtc.Room | None = None
SYSTEM_PROMPT: str = VOICE_PROMPT
# The lfg user the current speaker is (set from the human participant's `lfg.user`
# attribute, which the web orb publishes from its chosen user). Empty / "__all"
# means "no scoping" — show the whole fleet, as before. When set, every fleet
# read and action is filtered to this user's sessions so one person never sees
# or touches another's work.
CURRENT_USER: str = ""
# Where the lfg-sessions skill persists which session the user is focused on
# ("it" / "my project"). Read fresh each turn so it tracks what they're on.
FOCUS_FILE = Path.home() / ".lfg" / "active-session"


def _oauth_token() -> str | None:
    """Read the current Claude OAuth access token (kept fresh by Claude Code)."""
    try:
        c = json.loads(CREDS_FILE.read_text())
        return (c.get("claudeAiOauth") or {}).get("accessToken")
    except Exception:
        return None


_http: aiohttp.ClientSession | None = None


async def get_http() -> aiohttp.ClientSession:
    global _http
    if _http is None or _http.closed:
        # Bound every request so a stalled Anthropic/LFG call can't pin the voice
        # turn open. Without this the LLM stream never finishes and the orb is
        # stuck narrating "Thinking…" (LiveKit's lk.agent.state stays "thinking"
        # for the whole life of LfgLLMStream). connect/sock_read are short; total
        # leaves headroom for a slow deep-think consult.
        timeout = aiohttp.ClientTimeout(total=60, connect=10, sock_read=45)
        _http = aiohttp.ClientSession(timeout=timeout)
    return _http


def _pcm16_wav(pcm: bytes, rate: int, ch: int) -> bytes:
    buf = io.BytesIO()
    w = wave.open(buf, "wb")
    w.setnchannels(ch)
    w.setsampwidth(2)
    w.setframerate(rate)
    w.writeframes(pcm)
    w.close()
    return buf.getvalue()


def _wav_to_pcm(data: bytes) -> tuple[int, int, bytes]:
    w = wave.open(io.BytesIO(data), "rb")
    return w.getframerate(), w.getnchannels(), w.readframes(w.getnframes())


def _speakable(md: str) -> str:
    t = md
    t = re.sub(r"```[\s\S]*?```", " ", t)
    t = re.sub(r"`([^`]+)`", r"\1", t)
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"^\s{0,3}#{1,6}\s+", "", t, flags=re.M)
    t = re.sub(r"^\s*[-*+]\s+", "", t, flags=re.M)
    t = re.sub(r"[*_~]{1,3}([^*_~]+)[*_~]{1,3}", r"\1", t)
    return re.sub(r"\s+", " ", t).strip()


# ── STT: lfg /api/voice/stt (faster-whisper) ────────────────────────────────
class LfgSTT(stt.STT):
    def __init__(self) -> None:
        super().__init__(
            capabilities=stt.STTCapabilities(streaming=False, interim_results=False)
        )

    async def _recognize_impl(self, buffer, *, language=None, conn_options=None):
        frame = rtc.combine_audio_frames(buffer)
        wav = _pcm16_wav(bytes(frame.data), frame.sample_rate, frame.num_channels)
        text = ""
        try:
            http = await get_http()
            async with http.post(
                f"{LFG}/api/voice/stt",
                data=wav,
                headers={"Content-Type": "application/octet-stream"},
            ) as r:
                if r.status == 200:
                    j = await r.json()
                    text = (j.get("text") or "").strip()
        except Exception:
            pass
        if text:
            print(f"[voice] user: {text}", flush=True)
        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            alternatives=[stt.SpeechData(language=language or "en", text=text)],
        )


# ── fleet tools (Anthropic tool-use, backed by the lfg HTTP API) ─────────────
# The voice brain (Haiku) gets these tools plus consult_advisor; the advisor it
# escalates to is a separate, more powerful backend session — see consult_advisor.
FLEET_TOOLS = [
    {
        "name": "get_fleet_status",
        "description": "Re-read the live status of the user's lfg sessions (blocked / working / idle, with the pending question for blocked ones).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_sessions",
        "description": "List the user's sessions with their ids and titles. Call this to resolve a session id before reply_to_session, answer_session_prompt, or close_session.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_repos",
        "description": "List the repos/projects a new session can be started in (name + path). Call this to resolve the working folder before create_session when the user names a project.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "create_session",
        "description": "Start a NEW coding-agent session to work on a task OR to investigate/find out something the user asked about. Give it a clear one-line instruction in `prompt`. Optionally pass `cwd` (a repo path from list_repos); omit to default to the user's focused project. Returns the new session id. Slow (a few seconds) — say a short spoken preamble first.",
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "cwd": {"type": "string"},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "reply_to_session",
        "description": "Send an instruction to another session (queued; it steers that session's next turn).",
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "text": {"type": "string"},
            },
            "required": ["session_id", "text"],
        },
    },
    {
        "name": "answer_session_prompt",
        "description": "Answer a session that is BLOCKED on a permission/plan prompt by picking an option index (0-based) from its snapshot line.",
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "option_index": {"type": "integer"},
            },
            "required": ["session_id", "option_index"],
        },
    },
    {
        "name": "close_session",
        "description": "Close / shut down a session the user is done with. Resolve the exact session id first (via list_sessions or the snapshot) — this is destructive. NEVER close your own voice session.",
        "input_schema": {
            "type": "object",
            "properties": {"session_id": {"type": "string"}},
            "required": ["session_id"],
        },
    },
]

# The escalation tool — hands a hard/risky question to the single deep-think
# advisor (a stronger Opus backend session with full repo + tool access).
ESCALATE_TOOL = {
    "name": "consult_advisor",
    "description": "Escalate a genuinely HARD or RISKY question to the deep-think advisor: a stronger model running as a real lfg session with full repo + tool access, so it can both reason carefully AND act on the fleet. Use only when careful reasoning is truly needed, not for simple disambiguation (just ask the user). Slow — say a short spoken preamble first. Returns a short spoken answer.",
    "input_schema": {
        "type": "object",
        "properties": {"question": {"type": "string"}},
        "required": ["question"],
    },
}

# The voice brain always has the fleet tools plus the one advisor escalation.
BRAIN_TOOLS = FLEET_TOOLS + [ESCALATE_TOOL]


async def set_activity(state: str) -> None:
    """Publish a custom orb state (consulting / replying / "") to the room."""
    if ROOM is None:
        return
    try:
        await ROOM.local_participant.set_attributes({"lfg.activity": state})
    except Exception:
        pass


# Short, spoken-style labels the orb shows while a tool is running, so the user
# sees WHAT the brain is doing (not just that it's busy). Streamed to the orb
# frontend via the `lfg.tool` participant attribute.
TOOL_LABELS = {
    "get_fleet_status": "Checking fleet status",
    "list_sessions": "Listing sessions",
    "list_repos": "Listing repos",
    "create_session": "Creating a session",
    "reply_to_session": "Messaging a session",
    "answer_session_prompt": "Answering a prompt",
    "close_session": "Closing a session",
    "consult_advisor": "Consulting the advisor",
}

# Spoken-style preambles said the instant a tool call starts, so the user hears
# that work is happening before any (possibly slow) tool runs — never dead air
# through "thinking". Present-tense intent only ("about to" / "one moment"),
# never a completion claim (the result isn't in yet). Used only as a fallback
# when the model didn't already produce its own spoken text on the tool turn.
TOOL_PREAMBLES = {
    "get_fleet_status": "Let me check on the fleet, one moment.",
    "list_sessions": "Let me pull up the sessions, one moment.",
    "list_repos": "Let me look at the repos, one moment.",
    "create_session": "Okay, spinning up a session for that, one moment.",
    "reply_to_session": "Okay, sending that over.",
    "answer_session_prompt": "Okay, answering that prompt, one moment.",
    "close_session": "One moment, closing that out.",
    "consult_advisor": "Let me check with the advisor, one moment.",
}
# Said when a tool call has no specific preamble above, so every tool call still
# gets spoken acknowledgement.
GENERIC_PREAMBLE = "One moment, let me take care of that."


async def set_tool(label: str) -> None:
    """Publish the current tool/thinking detail (a short label, or "" to clear)
    to the orb via the `lfg.tool` participant attribute."""
    if ROOM is None:
        return
    try:
        await ROOM.local_participant.set_attributes({"lfg.tool": label})
    except Exception:
        pass


# ── speaker scoping + focus (who is talking, and what project they're on) ─────
def _refresh_current_user() -> None:
    """Read the speaking user from the human participant's `lfg.user` attribute
    (published by the web orb) and make CURRENT_USER authoritative for whoever is
    in the room *right now*.

    This worker is long-lived and the "voice" room is reused across taps, so we
    must CLEAR CURRENT_USER when no human is publishing an `lfg.user` — otherwise
    a new speaker who hasn't chosen a user inherits the previous speaker's
    identity, and any session they create gets assigned to the wrong user. On a
    fresh tap the orb publishes `lfg.user` a beat after connecting, so this may
    momentarily resolve to "" (unscoped → whole fleet, and a create lands
    unassigned rather than mis-assigned); `participant_attributes_changed`
    repopulates it the instant the attribute lands."""
    global CURRENT_USER
    if ROOM is None:
        return
    found = ""
    try:
        for p in ROOM.remote_participants.values():
            u = ((p.attributes or {}).get("lfg.user") or "").strip()
            if u and u != "__all":
                found = u
                break
    except Exception:
        return
    CURRENT_USER = found


def _read_focus() -> str:
    """The user's currently focused session id (what 'it' / 'my project' means)."""
    try:
        return FOCUS_FILE.read_text().strip()
    except Exception:
        return ""


def _user_qs() -> str:
    """Query suffix that scopes a fleet read to the speaking user (or "")."""
    return f"?user={CURRENT_USER}" if CURRENT_USER else ""


async def _lfg_get(path: str) -> dict:
    http = await get_http()
    async with http.get(f"{LFG}{path}") as r:
        return await r.json() if r.status == 200 else {"error": f"http {r.status}"}


async def _lfg_post(path: str, payload: dict) -> dict:
    http = await get_http()
    async with http.post(f"{LFG}{path}", json=payload) as r:
        try:
            j = await r.json()
        except Exception:
            j = {}
        return j if r.status == 200 else {"error": f"http {r.status}", **j}


async def _scoped_sessions() -> list[dict]:
    """Live sessions visible to the speaking user: their own (by assignedUser)
    plus whatever they're focused on. With no CURRENT_USER, the whole fleet."""
    j = await _lfg_get("/api/sessions")
    focus = _read_focus()
    out = []
    for s in j.get("sessions", []):
        if not s.get("sessionId"):
            continue
        if (
            CURRENT_USER
            and s.get("assignedUser") != CURRENT_USER
            and s.get("sessionId") != focus
        ):
            continue
        out.append(s)
    return out


async def run_tool(name: str, args: dict) -> str:
    """Execute one fleet tool; returns a compact string for the tool_result.
    Reads and actions are scoped to the speaking user (CURRENT_USER)."""
    try:
        if name == "get_fleet_status":
            return (
                await _lfg_get(f"/api/voice/snapshot{_user_qs()}")
            ).get("snapshot", "(none)")
        if name == "list_sessions":
            focus = _read_focus()
            rows = []
            for s in await _scoped_sessions():
                rows.append(
                    {
                        "id": s.get("sessionId"),
                        "title": (s.get("title") or "")[:60],
                        "user": s.get("assignedUser"),
                        "last": (s.get("lastUserText") or "")[:60],
                        "focused": s.get("sessionId") == focus,
                    }
                )
            return json.dumps(rows)
        if name == "list_repos":
            j = await _lfg_get("/api/repos")
            rows = [
                {"name": r.get("name"), "cwd": r.get("cwd")}
                for r in j.get("repos", [])
                if r.get("cwd")
            ]
            return json.dumps(rows)
        if name == "create_session":
            prompt = (args.get("prompt") or "").strip()
            if not prompt:
                return "need a task/prompt to start a session"
            payload: dict = {"prompt": prompt}
            cwd = (args.get("cwd") or "").strip()
            # Default the working folder to the user's focused project, so a
            # voice "go check on X" spins up in the right repo without them
            # naming it. Explicit cwd from the model wins.
            if not cwd:
                focus = _read_focus()
                if focus:
                    for s in await _scoped_sessions():
                        if s.get("sessionId") == focus and s.get("cwd"):
                            cwd = s["cwd"]
                            break
            if cwd:
                payload["cwd"] = cwd
            if CURRENT_USER:
                payload["user"] = CURRENT_USER
            await set_activity("replying")
            try:
                j = await _lfg_post("/api/sessions/new", payload)
            finally:
                await set_activity("")
            if j.get("ok"):
                sid = j.get("sessionId") or j.get("tmuxName") or ""
                return f"created session {sid}"
            return j.get("error") or "create failed"
        if name in ("reply_to_session", "answer_session_prompt", "close_session"):
            # Resolve-before-act: never act on a guessed/blank id, and never reach
            # outside the speaking user's scope. Force the model to list first.
            sid = (args.get("session_id") or "").strip()
            visible = {s.get("sessionId") for s in await _scoped_sessions()}
            if not sid or sid not in visible:
                return (
                    "couldn't find that session for you — call list_sessions to "
                    "resolve the exact id first, then try again"
                )
            if name == "reply_to_session":
                await set_activity("replying")
                try:
                    j = await _lfg_post(
                        f"/api/sessions/{sid}/send",
                        {"text": args.get("text", "")},
                    )
                finally:
                    await set_activity("")
                return "sent" if j.get("ok") else (j.get("error") or "send failed")
            if name == "answer_session_prompt":
                j = await _lfg_post(
                    f"/api/sessions/{sid}/answer",
                    {"index": int(args.get("option_index", 0))},
                )
                return "answered" if j.get("ok") else (j.get("error") or "answer failed")
            if name == "close_session":
                j = await _lfg_post(f"/api/sessions/{sid}/close", {})
                return "closed" if (j.get("ok") or not j.get("error")) else j["error"]
    except Exception as e:
        return f"tool error: {e}"
    return f"unknown tool {name}"


async def anthropic_call(
    messages: list[dict],
    system: str,
    *,
    model: str = HAIKU_MODEL,
    tools: list[dict] | None = None,
    max_tokens: int,
) -> dict | None:
    """One non-streaming Messages API call (OAuth). Returns parsed JSON or None."""
    token = _oauth_token()
    if not token:
        return None
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": messages,
    }
    if tools:
        body["tools"] = tools
    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    http = await get_http()
    try:
        async with http.post(ANTHROPIC_URL, json=body, headers=headers) as resp:
            if resp.status != 200:
                return None
            return await resp.json()
    except Exception:
        return None


# ── the advisor: ONE in-process Opus hop ─────────────────────────────────────
# The voice brain (Haiku) handles everything live. When a question is genuinely
# hard or risky, it consults Opus ONCE — same fleet tools, so the advisor can
# both reason and act. The advisor does NOT get consult_advisor itself, so there
# is no recursive escalation and exactly one, fast, legible advisor.
async def consult_advisor(question: str) -> str:
    """Run one Opus pass over a hard/risky question and return its spoken answer.
    Opus shares the fleet tools (it can act) but cannot escalate further."""
    q = (question or "").strip() or "Please advise."
    print(f"[voice] consult advisor: {q[:80]}", flush=True)
    await set_activity("consulting")
    try:
        for model in ADVISOR_MODELS:
            # Fresh msgs each attempt — run_brain mutates it with the tool loop.
            answer = await run_brain(
                [{"role": "user", "content": q}],
                model=model,
                system=SYSTEM_PROMPT + ADVISOR_NOTE,
                tools=FLEET_TOOLS,  # no consult_advisor → no recursion
            )
            if answer:
                print(f"[voice] advisor ({model}) answered", flush=True)
                return answer
    finally:
        await set_activity("")
    return "The advisor is busy right now — give me a moment and ask again."


async def run_brain(msgs, *, model: str, system: str, emit=None, tools=None) -> str:
    """Tool-use loop at `model`. Speaks each text chunk via emit() when given
    (the live voice turn); always returns the final assistant text. `tools`
    defaults to the full brain set (fleet + consult_advisor); the advisor passes
    FLEET_TOOLS so it can act but not escalate again."""
    if tools is None:
        tools = BRAIN_TOOLS
    for _ in range(6):
        resp = await anthropic_call(
            msgs, system, model=model, tools=tools, max_tokens=600
        )
        if not resp:
            return ""
        blocks = resp.get("content") or []
        if resp.get("stop_reason") == "tool_use":
            msgs.append({"role": "assistant", "content": blocks})
            tool_names = [
                b.get("name", "") for b in blocks if b.get("type") == "tool_use"
            ]
            # Speak any preamble the model produced alongside the tool call (e.g.
            # "let me check with the advisor"). Without this, text blocks on a
            # tool-use turn are dropped and the user hears dead air through a
            # slow consult. Guarantee a "hold on" even with no preamble — and
            # keep it present-tense intent ("about to"), never "done" (the model
            # is told never to claim completion before it sees the result).
            preamble = "".join(
                b.get("text", "") for b in blocks if b.get("type") == "text"
            ).strip()
            if not preamble:
                # Guarantee spoken feedback for EVERY tool call (not just a
                # hand-picked few) — pick the first tool's preamble, falling
                # back to a generic one so no tool ever runs in silence.
                first_tool = next((n for n in tool_names if n), "")
                preamble = TOOL_PREAMBLES.get(first_tool, GENERIC_PREAMBLE)
            if preamble and emit:
                print(f"[voice] say (preamble): {preamble}", flush=True)
                emit(preamble)
            print(f"[voice] tool_use ({model}): {tool_names}", flush=True)
            results = []
            for b in blocks:
                if b.get("type") != "tool_use":
                    continue
                name = b.get("name", "")
                args = b.get("input") or {}
                # Stream what we're doing to the orb so it can narrate the
                # tool call (and, for a consult, the "thinking" hand-off).
                await set_tool(TOOL_LABELS.get(name, name))
                try:
                    if name == "consult_advisor":
                        out = await consult_advisor(args.get("question", ""))
                    else:
                        out = await run_tool(name, args)
                finally:
                    await set_tool("")
                results.append(
                    {"type": "tool_result", "tool_use_id": b.get("id"), "content": out}
                )
            msgs.append({"role": "user", "content": results})
            continue
        # final answer
        text = "".join(
            b.get("text", "") for b in blocks if b.get("type") == "text"
        ).strip()
        if text and emit:
            print(f"[voice] reply ({model}): {text}", flush=True)
            emit(text)
        return text
    return ""


class LfgLLMStream(llm.LLMStream):
    async def _run(self) -> None:
        # full conversation history (LiveKit accumulates it across turns)
        msgs: list[dict] = []
        for it in self._chat_ctx.items:
            role = getattr(it, "role", None)
            if role not in ("user", "assistant"):
                continue
            text = (it.text_content or "").strip()
            if text:
                msgs.append({"role": role, "content": text})
        if not msgs or msgs[-1]["role"] != "user":
            return

        def emit(text: str) -> None:
            self._event_ch.send_nowait(
                llm.ChatChunk(
                    id=utils.shortuuid(),
                    delta=llm.ChoiceDelta(role="assistant", content=text),
                )
            )

        # Make sure we know who is speaking before scoping any fleet read.
        _refresh_current_user()

        # The voice brain is Haiku; it consults the advisor when truly unsure.
        #
        # This MUST finish (and the stream close) no matter what: LiveKit holds
        # lk.agent.state at "thinking" for the entire life of this stream, so a
        # hang or an unhandled exception here leaves the orb stuck "Thinking…"
        # forever. Bound the whole turn, speak a graceful fallback on failure,
        # and always clear the orb's activity/tool detail on the way out.
        try:
            await asyncio.wait_for(
                run_brain(msgs, model=HAIKU_MODEL, system=SYSTEM_PROMPT, emit=emit),
                timeout=90,
            )
        except asyncio.TimeoutError:
            print("[voice] run_brain timed out — recovering", flush=True)
            emit("Sorry, that took too long. Could you try again?")
        except Exception as e:
            print(f"[voice] run_brain failed: {e} — recovering", flush=True)
            emit("Sorry, I hit a snag there. Could you say that again?")
        finally:
            # Drop any lingering deep-work / tool detail so the orb doesn't keep
            # showing a stale "Consulting…" / tool label after the turn ends.
            await set_activity("")
            await set_tool("")


class LfgLLM(llm.LLM):
    def chat(self, *, chat_ctx, tools=None, conn_options=None, **kwargs):
        return LfgLLMStream(
            self, chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options
        )


# ── TTS: lfg /api/voice/tts (SuperTonic, 44.1kHz mono WAV) ───────────────────
class LfgTTSStream(tts.ChunkedStream):
    async def _run(self, output_emitter) -> None:
        http = await get_http()
        output_emitter.initialize(
            request_id=utils.shortuuid(),
            sample_rate=24000,  # CosyVoice2 fixed output rate
            num_channels=1,
            mime_type="audio/pcm",
        )
        # Stream raw int16 PCM as the GPU produces each chunk -> the room starts
        # playing at ~first-chunk latency instead of after the full utterance.
        carry = b""
        async with http.post(
            f"{LFG}/api/voice/tts", json={"text": self._input_text}
        ) as r:
            async for chunk in r.content.iter_chunked(9600):
                buf = carry + chunk
                n = len(buf) - (len(buf) % 2)  # keep 16-bit sample alignment
                if n:
                    output_emitter.push(buf[:n])
                carry = buf[n:]
        output_emitter.flush()


class LfgTTS(tts.TTS):
    def __init__(self) -> None:
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=24000,
            num_channels=1,
        )

    def synthesize(self, text, *, conn_options=None):
        return LfgTTSStream(tts=self, input_text=text, conn_options=conn_options)


# ── proactive briefing ──────────────────────────────────────────────────────
async def make_briefing(snapshot: str) -> str:
    """Turn the raw snapshot into a <=2 sentence spoken greeting + status."""
    msgs = [
        {
            "role": "user",
            "content": (
                "Greet me in one short sentence, then brief me on the fleet in "
                "at most one more sentence — lead with anything BLOCKED that "
                "needs my decision, else say how many are working/idle. Plain "
                "spoken words only.\n\nSNAPSHOT:\n" + (snapshot or "(no sessions)")
            ),
        }
    ]
    resp = await anthropic_call(msgs, VOICE_PROMPT, tools=None, max_tokens=160)
    if resp:
        text = "".join(
            b.get("text", "")
            for b in (resp.get("content") or [])
            if b.get("type") == "text"
        ).strip()
        if text:
            return text
    return "Hey, I'm online. Tap to ask me anything about your sessions."


# ── per-start seeding ───────────────────────────────────────────────────────
async def seed_system_prompt() -> str:
    """Refresh the global system prompt from a live, user-scoped fleet snapshot,
    the user's standing context, and their focused session. Returns the raw
    snapshot (for the briefing)."""
    global SYSTEM_PROMPT
    snapshot = ""
    try:
        snap = await _lfg_get(f"/api/voice/snapshot{_user_qs()}")
        snapshot = snap.get("snapshot", "")
        parts = [VOICE_PROMPT]
        focus = _read_focus()
        if focus:
            parts.append(
                "=== FOCUS ===\n"
                f"The user's currently focused session id is {focus}. When they "
                "say 'it', 'this', 'my project', or 'the session' without naming "
                "one, they mean this. New sessions default to its project.\n"
                "=== END FOCUS ==="
            )
        if snapshot:
            parts.append(
                "=== SESSION SNAPSHOT (point-in-time, captured when you "
                "connected — treat as STALE; call get_fleet_status for current "
                "status before answering status questions) ===\n"
                + snapshot
                + "\n=== END SNAPSHOT ==="
            )
        if snap.get("context"):
            parts.append("=== USER CONTEXT ===\n" + snap["context"])
        SYSTEM_PROMPT = "\n\n".join(parts)
    except Exception:
        SYSTEM_PROMPT = VOICE_PROMPT
    return snapshot


async def start_session(session: AgentSession, *, clear: bool) -> None:
    """Begin a voice interaction: optionally wipe any prior conversation, refresh
    the system prompt, and speak a proactive briefing.

    The room ("voice") is persistent and the agent stays in it, so a reconnect
    reuses the SAME AgentSession — its chat_ctx still holds the previous chat.
    Clearing it here means each fresh start (a tap of the orb) begins clean, with
    no leftover context carried over from the last conversation."""
    if clear:
        try:
            await session.current_agent.update_chat_ctx(llm.ChatContext.empty())
            print("[voice] cleared previous context (new start)", flush=True)
        except Exception:
            pass
    # Resolve who is speaking before scoping the seeded snapshot to them.
    _refresh_current_user()
    snapshot = await seed_system_prompt()
    # Speak a proactive briefing the moment we connect (no user turn needed).
    try:
        await session.say(await make_briefing(snapshot))
    except Exception:
        pass


# ── worker entrypoint ───────────────────────────────────────────────────────
async def entrypoint(ctx: JobContext) -> None:
    global ROOM
    await ctx.connect()
    ROOM = ctx.room

    session = AgentSession(stt=LfgSTT(), llm=LfgLLM(), tts=LfgTTS())

    # A fresh tap of the orb shows up here as a participant (re)joining the
    # persistent "voice" room. Clear the prior conversation and re-brief so each
    # start is a clean slate — this fires on reconnects, where the job/session
    # (and its accumulated chat_ctx) outlived the previous browser disconnect.
    @ctx.room.on("participant_connected")
    def _on_participant(_p: rtc.RemoteParticipant) -> None:
        asyncio.create_task(start_session(session, clear=True))

    # The web orb publishes the speaking user as the `lfg.user` attribute,
    # sometimes a beat after connecting — keep CURRENT_USER in step with it.
    @ctx.room.on("participant_attributes_changed")
    def _on_attrs(_changed, _p) -> None:
        _refresh_current_user()

    await session.start(Agent(instructions="lfg voice assistant."), room=ctx.room)

    # Every start clears prior context. A normal reconnect dispatches a fresh job
    # (this entrypoint) with an already-empty session, so the clear is a no-op
    # there — but it makes "each start begins clean" an explicit invariant that
    # also holds if the session is ever kept warm across disconnects
    # (close_on_disconnect=False), where the participant handler above clears it.
    await start_session(session, clear=True)


if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            ws_url=os.environ.get("LIVEKIT_URL", "ws://127.0.0.1:7880"),
            api_key=os.environ.get("LIVEKIT_API_KEY"),
            api_secret=os.environ.get("LIVEKIT_API_SECRET"),
            num_idle_processes=1,
        )
    )
