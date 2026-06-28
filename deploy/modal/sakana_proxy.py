import os

import modal

app = modal.App("sakana-egress-proxy")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install("fastapi[standard]", "httpx")
)

UPSTREAM = os.environ.get("SAKANA_PROXY_UPSTREAM", "https://api.sakana.ai")
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

@app.function(
    image=image,
    timeout=7200,
    scaledown_window=300,
)
@modal.asgi_app()
def proxy():
    from collections.abc import AsyncIterator

    import httpx
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, Response, StreamingResponse

    web = FastAPI()

    def _forward_headers(request: Request) -> dict[str, str]:
        headers: dict[str, str] = {}
        for key, value in request.headers.items():
            lower = key.lower()
            if lower in HOP_BY_HOP_HEADERS or lower in {"host", "content-length"}:
                continue
            headers[key] = value
        return headers

    def _response_headers(response: httpx.Response) -> dict[str, str]:
        headers: dict[str, str] = {}
        for key, value in response.headers.items():
            lower = key.lower()
            if lower in HOP_BY_HOP_HEADERS or lower in {"content-length", "content-encoding"}:
                continue
            headers[key] = value
        headers["x-sakana-egress-proxy"] = "modal"
        return headers

    @web.get("/health")
    async def health():
        return {"ok": True, "upstream": UPSTREAM}

    @web.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    async def proxy_v1(path: str, request: Request):
        url = f"{UPSTREAM}/v1/{path}"
        if request.url.query:
            url = f"{url}?{request.url.query}"

        body = await request.body()
        timeout = httpx.Timeout(connect=30.0, read=None, write=30.0, pool=30.0)
        client = httpx.AsyncClient(timeout=timeout, follow_redirects=False)

        try:
            upstream_request = client.build_request(
                request.method,
                url,
                headers=_forward_headers(request),
                content=body,
            )
            upstream_response = await client.send(upstream_request, stream=True)
        except Exception as exc:
            await client.aclose()
            return JSONResponse(
                {"error": {"message": f"proxy upstream request failed: {type(exc).__name__}"}},
                status_code=502,
            )

        async def stream_body() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream_response.aiter_raw():
                    yield chunk
            finally:
                await upstream_response.aclose()
                await client.aclose()

        headers = _response_headers(upstream_response)
        media_type = upstream_response.headers.get("content-type")
        if request.method == "HEAD":
            await upstream_response.aclose()
            await client.aclose()
            return Response(status_code=upstream_response.status_code, headers=headers, media_type=media_type)

        return StreamingResponse(
            stream_body(),
            status_code=upstream_response.status_code,
            headers=headers,
            media_type=media_type,
        )

    return web
