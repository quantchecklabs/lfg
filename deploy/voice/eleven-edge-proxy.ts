// Edge proxy for the cloudflared quick-tunnel in front of Option B.
//
// A trycloudflare quick tunnel forwards EVERY path of its target origin, so we
// must NOT point it straight at :8766 (that would expose the whole lfg dashboard
// + /api to the public internet). Instead cloudflared points at THIS proxy,
// which forwards ONLY `POST /v1/chat/completions` to the backend and 404s
// everything else. The endpoint itself is still bearer-gated in serve.ts, so
// this is defence-in-depth: wrong path → 404 here, wrong/no secret → 401 there.
//
// Bound to loopback; only cloudflared (also local) reaches it.

const UPSTREAM = process.env.LFG_BASE || "http://127.0.0.1:8766";
const PORT = Number(process.env.LFG_EDGE_PORT || 8788);
const ALLOW = "/v1/chat/completions";

Bun.serve({
  port: PORT,
  hostname: "127.0.0.1",
  // Long replies (brain + tool loop) can take a while; don't cut the SSE stream.
  idleTimeout: 120,
  async fetch(req) {
    const url = new URL(req.url);
    if (req.method === "GET" && url.pathname === "/healthz") {
      return new Response("ok", { status: 200 });
    }
    if (req.method !== "POST" || url.pathname !== ALLOW) {
      return new Response("not found", { status: 404 });
    }
    // Forward method, Authorization + Content-Type, and the raw body. Return the
    // upstream response (SSE stream) verbatim so chunks pass straight through.
    const headers: Record<string, string> = {
      "content-type": req.headers.get("content-type") || "application/json",
    };
    const auth = req.headers.get("authorization");
    if (auth) headers["authorization"] = auth;
    try {
      const upstream = await fetch(`${UPSTREAM}${ALLOW}`, {
        method: "POST",
        headers,
        body: await req.arrayBuffer(),
      });
      return new Response(upstream.body, {
        status: upstream.status,
        headers: {
          "content-type":
            upstream.headers.get("content-type") || "text/event-stream",
          "cache-control": "no-cache, no-transform",
        },
      });
    } catch (e) {
      return new Response(`upstream error: ${e}`, { status: 502 });
    }
  },
});

console.log(`[eleven-edge] proxy on 127.0.0.1:${PORT} → ${UPSTREAM}${ALLOW} (only)`);
