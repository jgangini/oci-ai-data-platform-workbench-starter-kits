"""Draft provider tests run against fixed fake transports, never live services."""
import asyncio
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest


VIEWER = Path(__file__).parents[2] / "prisma-viewer"
spec = importlib.util.spec_from_file_location("provider_probes", VIEWER / "provider_probes.py")
probes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probes)
SECRET = "unsaved-private-key"


def fake_http(monkeypatch, handler):
    def transport(**kwargs):
        assert kwargs == {"retries": 0, "trust_env": False}
        return httpx.MockTransport(handler)
    monkeypatch.setattr(probes.httpx, "AsyncHTTPTransport", transport)


@pytest.mark.parametrize("provider,host,path,body,scope", [
    ("openai", "api.openai.com", "/v1/models", {"data": []}, "openai_model_catalog_only"),
    ("firms", "firms.modaps.eosdis.nasa.gov", "/mapserver/mapkey_status/", {"transaction_limit": 5000, "current_transactions": 1}, "firms_map_key_quota"),
    ("tomtom", "api.tomtom.com", "/traffic/services/4/flowSegmentData/relative0/10/json", {"flowSegmentData": {"currentSpeed": 20}}, "tomtom_traffic_flow"),
    ("cesium-ion", "api.cesium.com", "/v1/assets/1/endpoint", {"url": "https://assets.cesium.com/example", "type": "TERRAIN", "accessToken": SECRET}, "cesium_world_terrain_asset_1"),
    ("launch-library", "ll.thespacedevs.com", "/2.3.0/launches/", {"results": []}, "launch_library_authenticated_feed"),
])
def test_draft_tests_use_fixed_endpoint_and_return_only_safe_scope(monkeypatch, caplog, provider, host, path, body, scope):
    calls = []
    def respond(request):
        calls.append(request)
        assert request.url.host == host and request.url.path == path
        assert request.method == "GET" and request.url.scheme == "https"
        assert request.extensions["timeout"]["read"] == 10
        return httpx.Response(200, json=body)
    fake_http(monkeypatch, respond)
    environment = {probes.FIELDS[provider][0]: SECRET, "UNRELATED_SECRET": "must-not-leave"}
    original = dict(environment)
    result = asyncio.run(probes.test_provider(provider, environment, "https://globe.example"))
    assert result["ok"] and result["status"] == "success" and result["scope"] == scope
    assert len(calls) == 1 and environment == original
    assert SECRET not in json.dumps(result) and "must-not-leave" not in str(calls[0].headers)
    assert SECRET not in caplog.text
    if provider == "openai":
        assert "Realtime and inference were not tested" in result["message"]
        assert calls[0].headers["authorization"] == "Bearer " + SECRET
    if provider == "launch-library":
        assert calls[0].url.params["limit"] == "1"
        assert calls[0].headers["authorization"] == "Token " + SECRET


def test_google_separates_browser_tiles_and_optional_server_places_scopes(monkeypatch):
    calls = []
    def respond(request):
        calls.append(request)
        if request.url.host == "tile.googleapis.com":
            assert request.headers["referer"] == "https://globe.example/"
            assert request.url.params["key"] == "browser-draft"
            return httpx.Response(200, json={"root": {}})
        assert request.url == "https://places.googleapis.com/v1/places:searchNearby"
        assert request.headers["x-goog-api-key"] == "server-draft"
        assert request.headers["x-goog-fieldmask"] == "places.id"
        assert "referer" not in request.headers
        assert json.loads(request.content)["maxResultCount"] == 1
        return httpx.Response(403, json={"error": {"message": SECRET}})
    fake_http(monkeypatch, respond)
    result = asyncio.run(probes.test_provider("google-maps", {
        "GOOGLE_MAPS_API_KEY": "browser-draft", "GOOGLE_MAPS_SERVER_API_KEY": "server-draft"}, "https://globe.example"))
    assert not result["ok"] and result["status"] == "invalid"
    assert [check["status"] for check in result["checks"]] == ["success", "invalid"]
    assert len(calls) == 2 and SECRET not in json.dumps(result)


def test_google_without_separate_server_key_does_not_claim_places_access(monkeypatch):
    def respond(request):
        assert request.url.host == "tile.googleapis.com"
        return httpx.Response(200, json={"root": {}})
    fake_http(monkeypatch, respond)
    result = asyncio.run(probes.test_provider("google-maps", {"GOOGLE_MAPS_API_KEY": SECRET}))
    assert result["ok"] and result["scope"] == "google_3d_tiles"


def test_opensky_requires_two_credentials_and_tests_bounded_states_without_exposing_oauth_token(monkeypatch):
    calls = []
    def respond(request):
        calls.append(request)
        if request.method == "POST":
            assert request.url.host == "auth.opensky-network.org"
            assert b"grant_type=client_credentials" in request.content
            assert b"client_secret=" + SECRET.encode() in request.content
            return httpx.Response(200, json={"access_token": "ephemeral-oauth-secret", "expires_in": 1800})
        assert request.url.host == "opensky-network.org" and request.url.path == "/api/states/all"
        assert request.headers["authorization"] == "Bearer ephemeral-oauth-secret"
        assert float(request.url.params["lamax"]) - float(request.url.params["lamin"]) < 0.1
        return httpx.Response(429, json={"message": "ephemeral-oauth-secret"})
    fake_http(monkeypatch, respond)
    missing = asyncio.run(probes.test_provider("opensky", {"OPENSKY_CLIENT_ID": "id"}))
    assert missing["status"] == "invalid" and not calls
    result = asyncio.run(probes.test_provider("opensky", {"OPENSKY_CLIENT_ID": "id", "OPENSKY_CLIENT_SECRET": SECRET}))
    assert result["status"] == "quota" and len(calls) == 2
    assert [check["status"] for check in result["checks"]] == ["success", "quota"]
    assert "ephemeral" not in json.dumps(result)


@pytest.mark.parametrize("code,expected", [(301, "unavailable"), (400, "invalid"), (401, "invalid"), (403, "invalid"), (429, "quota"), (500, "unavailable")])
def test_no_redirect_retry_or_raw_upstream_error(monkeypatch, code, expected):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(code, headers={"Location": "https://attacker.example/" + SECRET},
                              json={"error": {"message": SECRET, "private_key": SECRET}})
    fake_http(monkeypatch, respond)
    result = asyncio.run(probes.test_provider("openai", {"OPENAI_API_KEY": SECRET}))
    assert result["status"] == expected and len(calls) == 1
    assert SECRET not in json.dumps(result) and "attacker" not in json.dumps(result)


@pytest.mark.parametrize("payload", [b"not JSON", b"[]", b"{}", b"x" * (probes.MAX_RESPONSE_BYTES + 1)],
                         ids=["invalid-json", "array", "missing-fields", "oversized"])
def test_malformed_and_oversized_success_body_is_not_success(monkeypatch, payload):
    fake_http(monkeypatch, lambda request: httpx.Response(200, content=payload))
    result = asyncio.run(probes.test_provider("openai", {"OPENAI_API_KEY": SECRET}))
    assert result["status"] == "unavailable"


def test_firms_exhausted_quota_and_google_resource_exhausted_are_distinct(monkeypatch):
    fake_http(monkeypatch, lambda request: httpx.Response(200, json={"transaction_limit": 5, "current_transactions": 5}))
    assert asyncio.run(probes.test_provider("firms", {"FIRMS_MAP_KEY": SECRET}))["status"] == "quota"
    fake_http(monkeypatch, lambda request: httpx.Response(403, json={"error": {"status": "RESOURCE_EXHAUSTED", "message": SECRET}}))
    assert asyncio.run(probes.test_provider("google-maps", {"GOOGLE_MAPS_API_KEY": SECRET}))["status"] == "quota"


def test_transport_failure_is_sanitised_and_cancellation_propagates(monkeypatch):
    def failure(request):
        raise httpx.ConnectError(SECRET, request=request)
    fake_http(monkeypatch, failure)
    result = asyncio.run(probes.test_provider("openai", {"OPENAI_API_KEY": SECRET}))
    assert result["status"] == "unavailable" and SECRET not in json.dumps(result)
    async def cancelled(*args):
        raise asyncio.CancelledError
    monkeypatch.setattr(probes, "_dispatch", cancelled)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(probes.test_provider("openai", {"OPENAI_API_KEY": SECRET}))


@pytest.mark.parametrize("origin", ["https://user:pass@example.org", "https://example.org/?secret=x", "https://example.org/#x", "https://example.org/path", "http://untrusted.example", "https://example.org\r\nX-Key: secret"])
def test_referer_accepts_only_canonical_origin_without_credentials_or_path(origin):
    assert probes._origin_headers(origin) == {}
    assert probes._origin_headers("http://localhost:18081") == {"Referer": "http://localhost:18081/"}


def test_ais_uses_installed_ws_and_stdin_only_then_returns_safe_ack(monkeypatch):
    class Child:
        returncode = None
        async def communicate(self, input):
            assert json.loads(input) == {"key": SECRET}
            self.returncode = 0
            return b"success", None
    async def create(*args, **kwargs):
        assert args[:2] == ("node", "-e") and SECRET not in args[2]
        assert "perMessageDeflate: true" in args[2] and "SubscriptionConfirmation" in args[2]
        assert kwargs["stdin"] == asyncio.subprocess.PIPE
        assert kwargs["stderr"] == asyncio.subprocess.DEVNULL
        assert kwargs["cwd"] == VIEWER / ".upstream"
        return Child()
    monkeypatch.setattr(probes.asyncio, "create_subprocess_exec", create)
    result = asyncio.run(probes.test_provider("aisstream", {"AISSTREAM_API_KEY": SECRET}))
    assert result["ok"] and result["scope"] == "aisstream_subscription"
    assert SECRET not in json.dumps(result)


@pytest.mark.parametrize("exception", [TimeoutError, asyncio.CancelledError])
def test_ais_timeout_or_cancellation_kills_child(monkeypatch, exception):
    class Child:
        returncode = None
        killed = False
        async def communicate(self, input):
            raise exception()
        def kill(self):
            self.killed = True
        async def wait(self):
            self.returncode = 1
    child = Child()
    async def create(*args, **kwargs):
        return child
    monkeypatch.setattr(probes.asyncio, "create_subprocess_exec", create)
    if exception is asyncio.CancelledError:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(probes.test_provider("aisstream", {"AISSTREAM_API_KEY": SECRET}))
    else:
        assert asyncio.run(probes.test_provider("aisstream", {"AISSTREAM_API_KEY": SECRET}))["status"] == "unavailable"
    assert child.killed and child.returncode == 1


def test_unknown_provider_and_invalid_draft_never_open_network(monkeypatch):
    def unexpected(**kwargs):
        raise AssertionError("Must not open network")
    monkeypatch.setattr(probes.httpx, "AsyncHTTPTransport", unexpected)
    assert asyncio.run(probes.test_provider("https://evil.example", {}))["status"] == "unsupported"
    for value in ("", "bad\nheader", "x" * 513, None):
        assert asyncio.run(probes.test_provider("openai", {"OPENAI_API_KEY": value}))["status"] == "invalid"


def test_probe_fields_match_write_only_provider_metadata_allowlist():
    runtime_spec = importlib.util.spec_from_file_location("provider_runtime_probe_contract", VIEWER / "native" / "provider_runtime.py")
    runtime = importlib.util.module_from_spec(runtime_spec)
    runtime_spec.loader.exec_module(runtime)
    assert set(runtime.PROVIDERS) == set(probes.FIELDS) - {"google-maps-server"}
    for provider, (_, fields) in runtime.PROVIDERS.items():
        expected = set(probes.FIELDS[provider])
        if provider == "google-maps":
            expected.update(probes.FIELDS["google-maps-server"])
        assert set(fields) == expected


@pytest.mark.parametrize("message,status", [
    ({"MessageType": "SubscriptionConfirmation", "Message": {"CompressionEnabled": True}}, "success"),
    ({"MessageType": "SubscriptionConfirmation", "Message": {"CompressionEnabled": False}}, "unavailable"),
    ({"error": "invalid API key " + SECRET}, "invalid"),
    ({"error": "account connection limit " + SECRET}, "quota"),
])
def test_actual_ais_script_negotiates_compression_and_only_emits_fixed_status(message, status):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node runtime is required to exercise the AIS helper")
    # Replace only ws in this child: the real helper executes without sockets.
    prelude = r"""
const Module = require('module'), EventEmitter = require('events');
const originalLoad = Module._load;
class FakeWebSocket extends EventEmitter {
  constructor(url, options) {
    super();
    if (url !== 'wss://stream.aisstream.io/v0/stream' || !options.perMessageDeflate || options.followRedirects) process.exit(2);
    setImmediate(() => this.emit('open'));
  }
  send(payload) {
    const parsed = JSON.parse(payload);
    if (parsed.APIKey !== 'unsaved-private-key' || parsed.BoundingBoxes.length !== 1) process.exit(3);
    this.emit('message', Buffer.from(JSON.stringify(MESSAGE)));
  }
  terminate() { this.emit('close'); }
}
Module._load = function(name, ...args) { return name === 'ws' ? FakeWebSocket : originalLoad.call(this, name, ...args); };
""".replace("MESSAGE", json.dumps(message))
    result = subprocess.run([node, "-e", prelude + probes.AIS_SCRIPT],
                            input=json.dumps({"key": SECRET}), capture_output=True, text=True, timeout=4)
    assert result.returncode == 0 and result.stdout == status and result.stderr == ""
