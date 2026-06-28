// Provision (or update) the ElevenLabs managed agent for lfg "Option B".
//
// Creates a workspace secret holding our custom-LLM bearer, then creates an
// agent whose LLM is OUR backend (custom-llm → /v1/chat/completions). ElevenLabs
// owns STT (Scribe) / TTS / turn-taking; our box stays the brain (see
// src/voice-eleven-llm.ts). Per-user scoping rides in via the `user_id` dynamic
// variable, which the browser passes at startSession.
//
// Run:  bun deploy/voice/provision-eleven-agent.ts create
//       bun deploy/voice/provision-eleven-agent.ts show
//       bun deploy/voice/provision-eleven-agent.ts delete
//
// Reads ELEVENLABS_API_KEY + LFG_ELEVEN_LLM_SECRET from .env. Writes the created
// agent id to data/eleven-agent.json.

import { readFileSync, writeFileSync, existsSync } from "node:fs";
import { join } from "node:path";

const ROOT = join(import.meta.dir, "..", "..");
const OUT = join(ROOT, "data", "eleven-agent.json");
const API = "https://api.elevenlabs.io";

// minimal .env loader (don't depend on process env being populated)
function env(key: string): string {
  if (process.env[key]) return process.env[key]!;
  try {
    const line = readFileSync(join(ROOT, ".env"), "utf8")
      .split("\n")
      .find((l) => l.startsWith(key + "="));
    return line ? line.slice(key.length + 1).trim() : "";
  } catch {
    return "";
  }
}

const KEY = env("ELEVENLABS_API_KEY");
const SECRET = env("LFG_ELEVEN_LLM_SECRET");
// Public HTTPS base ElevenLabs' cloud will POST to (it appends
// /v1/chat/completions). Defaults to the Tailscale Funnel mount on :8443.
const LLM_URL =
  env("LFG_ELEVEN_LLM_URL") || "https://dev.tail8c417.ts.net:8443";
const VOICE_ID = env("ELEVENLABS_VOICE_ID") || "EXAVITQu4vr4xnSDxMaL";
const MODEL = env("LFG_VOICE_MODEL") || "claude-haiku-4-5";

if (!KEY) throw new Error("ELEVENLABS_API_KEY missing");

async function api(path: string, init?: RequestInit): Promise<any> {
  const r = await fetch(`${API}${path}`, {
    ...init,
    headers: {
      "xi-api-key": KEY,
      "content-type": "application/json",
      ...(init?.headers || {}),
    },
  });
  const text = await r.text();
  let body: any = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = text;
  }
  if (!r.ok) {
    throw new Error(`${init?.method || "GET"} ${path} → ${r.status}: ${text.slice(0, 500)}`);
  }
  return body;
}

// Create (or reuse) the workspace secret holding our bearer. ElevenLabs sends
// this back to us as `Authorization: Bearer <value>`, which our endpoint gate
// checks against LFG_ELEVEN_LLM_SECRET.
async function ensureSecret(): Promise<string> {
  if (!SECRET) throw new Error("LFG_ELEVEN_LLM_SECRET missing (run setup first)");
  const name = "LFG_CUSTOM_LLM_BEARER";
  // Try to find an existing one by name first.
  const list = await api("/v1/convai/secrets").catch(() => ({ secrets: [] }));
  const existing = (list?.secrets || []).find((s: any) => s.name === name);
  if (existing) return existing.secret_id;
  const created = await api("/v1/convai/secrets", {
    method: "POST",
    body: JSON.stringify({ name, value: SECRET, type: "new" }),
  });
  return created.secret_id || created.id;
}

function agentBody(secretId: string) {
  return {
    name: "lfg voice (Option B — custom LLM)",
    conversation_config: {
      agent: {
        first_message:
          "Hey, I'm your lfg voice. What do you want to check on?",
        language: "en",
        prompt: {
          // With a custom LLM, OUR backend supplies the real system prompt and
          // tools (src/voice-eleven-llm.ts). This text is only what ElevenLabs
          // forwards as a system message, which our brain ignores — kept short.
          prompt:
            "You are the lfg voice assistant. Defer entirely to the connected custom LLM backend.",
          llm: "custom-llm",
          custom_llm: {
            url: LLM_URL,
            model_id: MODEL,
            api_key: { secret_id: secretId },
            request_headers: {},
          },
        },
      },
      asr: {
        provider: "elevenlabs",
        quality: "high",
        user_input_audio_format: "pcm_16000",
      },
      tts: {
        voice_id: VOICE_ID,
        model_id: "eleven_flash_v2",
        agent_output_audio_format: "pcm_16000",
      },
      conversation: { text_only: false, max_duration_seconds: 1800 },
      // Declare the per-user variable so the client can pass it at startSession.
      dynamic_variables: { dynamic_variable_placeholders: { user_id: "" } },
    },
    platform_settings: {
      // Allow the client to override the first message / language at runtime.
      overrides: {
        conversation_config_override: {
          agent: { first_message: true, language: true },
        },
      },
    },
  };
}

async function create() {
  const secretId = await ensureSecret();
  console.log(`✓ secret ready: ${secretId}`);
  const created = await api("/v1/convai/agents/create", {
    method: "POST",
    body: JSON.stringify(agentBody(secretId)),
  });
  const agentId = created.agent_id || created.agentId;
  const rec = {
    agent_id: agentId,
    secret_id: secretId,
    llm_url: LLM_URL,
    voice_id: VOICE_ID,
    model: MODEL,
  };
  writeFileSync(OUT, JSON.stringify(rec, null, 2));
  console.log(`✓ agent created: ${agentId}`);
  console.log(`  custom LLM url: ${LLM_URL}/v1/chat/completions`);
  console.log(`  saved → ${OUT}`);
}

async function show() {
  if (!existsSync(OUT)) return console.log("no data/eleven-agent.json yet");
  const rec = JSON.parse(readFileSync(OUT, "utf8"));
  const a = await api(`/v1/convai/agents/${rec.agent_id}`);
  console.log(JSON.stringify({ saved: rec, live_name: a.name, llm: a?.conversation_config?.agent?.prompt?.llm }, null, 2));
}

async function del() {
  if (!existsSync(OUT)) return console.log("nothing to delete");
  const rec = JSON.parse(readFileSync(OUT, "utf8"));
  await api(`/v1/convai/agents/${rec.agent_id}`, { method: "DELETE" });
  console.log(`✓ deleted agent ${rec.agent_id}`);
}

// Point the live agent's custom_llm at a (new) public URL — idempotent, used by
// eleven-deploy.sh to self-heal if the MagicDNS host/port ever changes. Merges
// into the existing custom_llm so secret/model/headers are preserved.
async function setUrl() {
  const url = process.argv[3] || LLM_URL;
  if (!existsSync(OUT)) throw new Error("no data/eleven-agent.json — run create first");
  const rec = JSON.parse(readFileSync(OUT, "utf8"));
  const live = await api(`/v1/convai/agents/${rec.agent_id}`);
  const cur = live?.conversation_config?.agent?.prompt?.custom_llm || {};
  if (cur.url === url) {
    console.log(`✓ agent url already ${url} — no change`);
    return;
  }
  await api(`/v1/convai/agents/${rec.agent_id}`, {
    method: "PATCH",
    body: JSON.stringify({
      conversation_config: {
        agent: { prompt: { llm: "custom-llm", custom_llm: { ...cur, url } } },
      },
    }),
  });
  rec.llm_url = url;
  writeFileSync(OUT, JSON.stringify(rec, null, 2));
  console.log(`✓ agent custom_llm.url → ${url}`);
}

const cmd = process.argv[2] || "create";
const fn = { create, show, delete: del, seturl: setUrl }[cmd];
if (!fn) {
  console.error(`unknown command: ${cmd} (use create|show|delete|seturl <url>)`);
  process.exit(1);
}
fn().catch((e) => {
  console.error("✗", e.message);
  process.exit(1);
});
