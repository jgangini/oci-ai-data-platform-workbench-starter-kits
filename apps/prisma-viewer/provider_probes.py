"""Bounded, non-persistent checks of the native globe's provider credentials."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx


FIELDS = {
    "google-maps": ("GOOGLE_MAPS_API_KEY",),
    "google-maps-server": ("GOOGLE_MAPS_SERVER_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "aisstream": ("AISSTREAM_API_KEY",),
    "firms": ("FIRMS_MAP_KEY",),
    "tomtom": ("TOMTOM_API_KEY",),
    "cesium-ion": ("CESIUM_ION_TOKEN",),
    "opensky": ("OPENSKY_CLIENT_ID", "OPENSKY_CLIENT_SECRET"),
    "launch-library": ("LL2_API_TOKEN",),
}
MESSAGES = {
    "success": "The checked provider scope is available.",
    "invalid": "Credentials are missing, rejected, restricted, or not permitted for this scope.",
    "quota": "The provider reported a quota or rate limit. Try again later.",
    "unavailable": "This provider scope could not be verified. Try again later.",
    "unsupported": "This test is unavailable in the current runtime.",
}
MAX_RESPONSE_BYTES = 256 * 1024

# ws is already pinned by the native application's package-lock.json. Credentials
# travel on stdin, never command arguments. Only a fixed status leaves the child.
AIS_SCRIPT = r"""
let WebSocket;
try { WebSocket = require('ws'); } catch { process.stdout.write('unsupported'); process.exit(0); }
let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => { input += chunk; if (input.length > 2048) process.exit(1); });
process.stdin.on('end', () => {
  let key;
  try { key = JSON.parse(input).key; } catch { process.exit(1); }
  let done = false;
  const socket = new WebSocket('wss://stream.aisstream.io/v0/stream', {
    perMessageDeflate: true, handshakeTimeout: 5000, maxPayload: 16384, followRedirects: false,
  });
  const finish = status => {
    if (done) return;
    done = true; clearTimeout(timer); socket.terminate(); process.stdout.write(status);
  };
  const timer = setTimeout(() => finish('unavailable'), 9000);
  socket.on('open', () => socket.send(JSON.stringify({
    APIKey: key, BoundingBoxes: [[[25.603, -80.208], [25.835, -79.879]]],
    FilterMessageTypes: ['PositionReport'],
  })));
  socket.on('message', data => {
    try {
      const body = JSON.parse(data.toString());
      if (body.MessageType === 'SubscriptionConfirmation') {
        finish(body.Message?.CompressionEnabled === true ? 'success' : 'unavailable');
      } else if (body.error) {
        const error = String(body.error).toLowerCase();
        finish(/limit|quota|too many/.test(error) ? 'quota' :
          /invalid.*key|key.*invalid|unauthoriz/.test(error) ? 'invalid' : 'unavailable');
      }
    } catch { finish('unavailable'); }
  });
  socket.on('unexpected-response', (_request, response) => {
    response.resume();
    finish(response.statusCode === 429 ? 'quota' :
      [401, 403].includes(response.statusCode) ? 'invalid' : 'unavailable');
  });
  socket.on('error', () => finish('unavailable'));
  socket.on('close', () => finish('unavailable'));
});
"""


def _check(scope: str, status: str) -> dict:
    return {"scope": scope, "ok": status == "success", "status": status, "message": MESSAGES[status]}


def _origin_headers(public_origin: str | None) -> dict:
    """Referer documents the deployment origin; it is never an outbound target."""
    if not public_origin or any(ord(char) < 32 for char in public_origin):
        return {}
    parsed = urlsplit(public_origin)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        return {}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}):
        return {}
    return {"Referer": public_origin.rstrip("/") + "/"} if parsed.hostname else {}


async def _request(method: str, url: str, **kwargs) -> tuple[int, dict]:
    # Direct transport avoids AsyncClient's INFO log of URLs containing query
    # credentials (Google/FIRMS/TomTom). Neither requests nor raw errors are logged.
    request = httpx.Request(method, url, extensions={"timeout": {
        "connect": 5.0, "read": 10.0, "write": 5.0, "pool": 5.0}}, **kwargs)
    async with httpx.AsyncHTTPTransport(retries=0, trust_env=False) as transport:
        response = await transport.handle_async_request(request)
        try:
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > MAX_RESPONSE_BYTES:
                    raise ValueError("Provider response exceeds test limit")
            try:
                body = json.loads(content)
            except (ValueError, UnicodeError):
                if 200 <= response.status_code < 300:
                    raise ValueError("Provider returned invalid JSON") from None
                body = {}
            if not isinstance(body, dict):
                raise ValueError("Provider returned an unexpected response")
            return response.status_code, body
        finally:
            await response.aclose()


def _http_status(code: int, body: dict) -> str | None:
    error = body.get("error")
    if isinstance(error, dict) and error.get("status") == "RESOURCE_EXHAUSTED":
        return "quota"
    if code in (402, 429):
        return "quota"
    if code in (400, 401, 403):
        return "invalid"
    return None if 200 <= code < 300 else "unavailable"


async def _google(environment: dict, origin: str | None, server_only: bool = False) -> list[dict]:
    checks = []
    if not server_only:
        code, body = await _request("GET", "https://tile.googleapis.com/v1/3dtiles/root.json",
                                    params={"key": environment["GOOGLE_MAPS_API_KEY"]}, headers=_origin_headers(origin))
        status = _http_status(code, body) or ("success" if isinstance(body.get("root"), dict) else "unavailable")
        checks.append(_check("google_3d_tiles", status))
    server_key = environment.get("GOOGLE_MAPS_SERVER_API_KEY")
    if server_key:
        code, body = await _request("POST", "https://places.googleapis.com/v1/places:searchNearby",
            headers={"X-Goog-Api-Key": server_key, "X-Goog-FieldMask": "places.id"},
            json={"maxResultCount": 1, "locationRestriction": {"circle": {
                "center": {"latitude": 4.6097, "longitude": -74.0817}, "radius": 25.0}}})
        # Google omits places when no results match the bounded search.
        status = _http_status(code, body) or ("success" if isinstance(body.get("places", []), list) else "unavailable")
        checks.append(_check("google_places_server", status))
    return checks


async def _opensky(environment: dict) -> list[dict]:
    code, body = await _request("POST", "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token",
        data={"grant_type": "client_credentials", "client_id": environment["OPENSKY_CLIENT_ID"],
              "client_secret": environment["OPENSKY_CLIENT_SECRET"]})
    status = _http_status(code, body)
    token = body.get("access_token")
    if status or not isinstance(token, str) or not token:
        return [_check("opensky_oauth", status or "unavailable")]
    code, body = await _request("GET", "https://opensky-network.org/api/states/all",
        params={"lamin": 4.60, "lomin": -74.10, "lamax": 4.62, "lomax": -74.08},
        headers={"Authorization": "Bearer " + token})
    status = _http_status(code, body) or ("success" if "states" in body and isinstance(body.get("time"), int) else "unavailable")
    return [_check("opensky_oauth", "success"), _check("opensky_states", status)]


async def _aisstream(key: str) -> list[dict]:
    try:
        child = await asyncio.create_subprocess_exec("node", "-e", AIS_SCRIPT,
            cwd=Path(__file__).parent / ".upstream", stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    except OSError:
        return [_check("aisstream_subscription", "unsupported")]
    try:
        output, _ = await asyncio.wait_for(child.communicate(json.dumps({"key": key}).encode()), timeout=11)
        status = output.decode("ascii", errors="replace")
        if child.returncode != 0 or status not in MESSAGES:
            status = "unavailable"
        return [_check("aisstream_subscription", status)]
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()


async def _single(provider_id: str, environment: dict, origin: str | None) -> list[dict]:
    key = environment[FIELDS[provider_id][0]]
    requests = {
        "openai": ("https://api.openai.com/v1/models", {"headers": {"Authorization": "Bearer " + key}}, "openai_model_catalog_only"),
        "firms": ("https://firms.modaps.eosdis.nasa.gov/mapserver/mapkey_status/", {"params": {"MAP_KEY": key}}, "firms_map_key_quota"),
        "tomtom": ("https://api.tomtom.com/traffic/services/4/flowSegmentData/relative0/10/json", {"params": {"key": key, "point": "4.6097,-74.0817"}}, "tomtom_traffic_flow"),
        "cesium-ion": ("https://api.cesium.com/v1/assets/1/endpoint", {"params": {"access_token": key}, "headers": _origin_headers(origin)}, "cesium_world_terrain_asset_1"),
        "launch-library": ("https://ll.thespacedevs.com/2.3.0/launches/", {"params": {"limit": 1}, "headers": {"Authorization": "Token " + key}}, "launch_library_authenticated_feed"),
    }
    url, kwargs, scope = requests[provider_id]
    code, body = await _request("GET", url, **kwargs)
    status = _http_status(code, body)
    if status:
        return [_check(scope, status)]
    valid = {
        "openai": isinstance(body.get("data"), list),
        "firms": isinstance(body.get("transaction_limit"), (int, float)) and isinstance(body.get("current_transactions"), (int, float)),
        "tomtom": isinstance(body.get("flowSegmentData"), dict),
        "cesium-ion": isinstance(body.get("url"), str) and body.get("type") in {"TERRAIN", "3DTILES", "IMAGERY"},
        "launch-library": isinstance(body.get("results"), list),
    }[provider_id]
    status = "success" if valid else "unavailable"
    if provider_id == "firms" and valid and body["current_transactions"] >= body["transaction_limit"]:
        status = "quota"
    return [_check(scope, status)]


async def _dispatch(provider_id: str, environment: dict, public_origin: str | None) -> list[dict]:
    if provider_id in {"google-maps", "google-maps-server"}:
        return await _google(environment, public_origin, provider_id == "google-maps-server")
    if provider_id == "opensky":
        return await _opensky(environment)
    if provider_id == "aisstream":
        return await _aisstream(environment["AISSTREAM_API_KEY"])
    return await _single(provider_id, environment, public_origin)


async def test_provider(provider_id: str, environment: dict, public_origin: str | None = None) -> dict:
    """Test merged, validated draft credentials; never save or apply them."""
    if provider_id not in FIELDS:
        checks = [_check("provider", "unsupported")]
    elif any(not isinstance(environment.get(field), str) or not environment[field].strip()
             or len(environment[field]) > 512 or any(ord(char) < 32 for char in environment[field])
             for field in FIELDS[provider_id]):
        checks = [_check(provider_id, "invalid")]
    else:
        try:
            # Wall deadline also bounds two-step probes and a trickling response.
            async with asyncio.timeout(20):
                checks = await _dispatch(provider_id, environment, public_origin)
        except (httpx.HTTPError, OSError, ValueError, TimeoutError):
            checks = [_check(provider_id, "unavailable")]
    failure = next((check for check in checks if not check["ok"]), None)
    status = failure["status"] if failure else "success"
    message = MESSAGES[status]
    if provider_id == "openai" and status == "success":
        message = "Model catalog access verified. Realtime and inference were not tested."
    return {"ok": status == "success", "status": status, "message": message,
            "scope": ", ".join(check["scope"] for check in checks), "checks": checks,
            "checked_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
