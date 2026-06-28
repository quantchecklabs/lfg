import json
import os
import urllib.error
import urllib.request

import modal

app = modal.App("sakana-egress-probe")
image = modal.Image.debian_slim(python_version="3.11")


def _request(url: str, *, method: str = "GET", headers: dict[str, str] | None = None, body: bytes | None = None):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return {
                "status": res.status,
                "content_type": res.headers.get("content-type"),
                "body": res.read(400).decode("utf-8", "replace"),
            }
    except urllib.error.HTTPError as exc:
        return {
            "status": exc.code,
            "content_type": exc.headers.get("content-type"),
            "body": exc.read(400).decode("utf-8", "replace"),
        }


@app.function(image=image, timeout=60)
def probe(api_key: str):
    ipinfo_raw = _request("https://ipinfo.io/json")
    try:
        ipinfo = json.loads(ipinfo_raw["body"])
    except Exception:
        ipinfo = {"raw": ipinfo_raw}

    payload = json.dumps(
        {
            "model": "fugu",
            "messages": [{"role": "user", "content": "Reply ok"}],
        }
    ).encode()
    sakana = _request(
        "https://api.sakana.ai/v1/chat/completions",
        method="POST",
        headers={
            "content-type": "application/json",
            "accept": "application/json",
            "authorization": f"Bearer {api_key}",
        },
        body=payload,
    )

    body = sakana["body"]
    content = None
    if sakana["content_type"] and "json" in sakana["content_type"]:
        try:
            parsed = json.loads(body)
            content = parsed.get("choices", [{}])[0].get("message", {}).get("content")
        except Exception:
            content = None

    return {
        "egress": {
            "ip": ipinfo.get("ip"),
            "city": ipinfo.get("city"),
            "region": ipinfo.get("region"),
            "country": ipinfo.get("country"),
            "org": ipinfo.get("org"),
        },
        "sakana": {
            "status": sakana["status"],
            "content_type": sakana["content_type"],
            "content": content,
            "body_start": body[:160] if not content else None,
        },
    }


@app.local_entrypoint()
def main():
    auth_path = os.path.expanduser("~/.local/share/opencode/auth.json")
    with open(auth_path, "r", encoding="utf-8") as f:
        auth = json.load(f)
    api_key = auth.get("fugu", {}).get("key")
    if not api_key:
        raise RuntimeError("missing fugu key in OpenCode auth")
    print(json.dumps(probe.remote(api_key), indent=2))
