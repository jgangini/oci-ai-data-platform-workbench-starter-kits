"""Authenticated fixed-destination bridge to the unmodified native feed providers."""
import os
from urllib.parse import quote

import httpx
from fastapi import HTTPException
from starlette.responses import HTMLResponse, StreamingResponse

NATIVE_ORIGIN = "http://127.0.0.1:4173"
API_ROOTS = frozenset({"opensky", "opensky-track", "adsblol", "adsbdb", "celestrak", "tomtom", "firms",
    "launches", "terrain", "overpass", "military-installations", "regional-brief", "geocode", "weather-effects",
    "cctv", "radio", "gbfs", "local-receivers", "transit", "ais-live", "openai", "google", "realtime", "route",
    "wind", "weather", "cyclones", "fire-perimeters"})
RESPONSE_HEADERS = {"content-type", "content-length", "content-encoding", "content-range", "accept-ranges",
    "cache-control", "etag", "last-modified", "content-security-policy", "x-frame-options", "x-content-type-options"}


def enabled():
    return os.getenv("GODS_EYE_NATIVE_ENABLED", "false").lower() == "true"


def check_origin(request):
    # VM1 overwrites this header after its session check; VM2 accepts only VM1.
    origin = request.headers.get("origin")
    expected = request.headers.get("x-gev-origin")
    if not expected:
        raise HTTPException(403, "An authenticated public origin is required")
    if request.headers.get("sec-fetch-site", "") not in {"", "same-origin", "none"}:
        raise HTTPException(403, "Cross-origin provider requests are refused")
    if origin and origin != expected:
        raise HTTPException(403, "The provider request origin is invalid")
    if request.method not in {"GET", "HEAD"} and not origin:
        raise HTTPException(403, "An exact request origin is required")


def native_json(path):
    try:
        with httpx.Client(timeout=3, trust_env=False) as client:
            response = client.get(NATIVE_ORIGIN + path)
            response.raise_for_status()
            return response.json()
    except (httpx.HTTPError, ValueError) as error:
        raise HTTPException(503, "The native God's Eye View runtime is unavailable") from error


def health():
    if enabled() and native_json("/__native_health").get("status") != "ok":
        raise HTTPException(503, "The native God's Eye View runtime is not ready")


async def proxy(request, path, *, html_transform=None):
    if not enabled():
        raise HTTPException(503, "The native God's Eye View runtime is not enabled")
    check_origin(request)
    if path.startswith("api/") and path.split("/", 2)[1] not in API_ROOTS:
        raise HTTPException(404)
    if any(part in {".", ".."} for part in path.split("/")) or path.startswith(("/", "__")):
        raise HTTPException(404)
    if html_transform and (path not in {"", "index.html"} or request.method != "GET"):
        raise HTTPException(404)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 1_000_000:
            raise HTTPException(413)
    # Never forward a session cookie, OCI key, forwarded identity, or user-selected target.
    headers = {key: request.headers[key] for key in ("accept", "content-type", "range", "if-none-match", "if-modified-since") if key in request.headers}
    if html_transform:
        for key in ("range", "if-none-match", "if-modified-since"):
            headers.pop(key, None)
    headers.update(origin=NATIVE_ORIGIN, **{"sec-fetch-site": "same-origin", "accept-encoding": "identity"})
    url = NATIVE_ORIGIN + "/" + quote(path, safe="/-._~")
    if request.url.query:
        url += "?" + request.url.query
    client = httpx.AsyncClient(timeout=httpx.Timeout(120, connect=5), trust_env=False, follow_redirects=False)
    try:
        upstream = await client.send(client.build_request(request.method, url, headers=headers, content=bytes(body)), stream=True)
    except httpx.HTTPError as error:
        await client.aclose()
        raise HTTPException(503, "The native data provider is unavailable") from error

    if html_transform:
        try:
            if upstream.status_code != 200 or upstream.headers.get("content-type", "").split(";")[0] != "text/html":
                raise ValueError("Native HTML is unavailable")
            document = bytearray()
            async for chunk in upstream.aiter_bytes():
                document.extend(chunk)
                if len(document) > 2_000_000:
                    raise ValueError("Native HTML exceeds its limit")
            return HTMLResponse(html_transform(document.decode("utf-8")), headers={
                "cache-control": "no-store", **{key: value for key, value in upstream.headers.items()
                    if key.lower() in {"content-security-policy", "x-frame-options", "x-content-type-options"}},
            })
        except (httpx.HTTPError, ValueError) as error:
            raise HTTPException(503, "The native viewer identity is unavailable") from error
        finally:
            await upstream.aclose()
            await client.aclose()

    async def content():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(content(), status_code=upstream.status_code,
        headers={key: value for key, value in upstream.headers.items() if key.lower() in RESPONSE_HEADERS})
