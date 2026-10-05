import asyncio
import copy
import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest


VIEWER = Path(__file__).parents[2] / "territorial-viewer"
spec = importlib.util.spec_from_file_location("context_layers", VIEWER / "context_layers.py")
context = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = context
spec.loader.exec_module(context)
NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc).timestamp()


def response():
    return [{"latitude": lat, "longitude": lon, "utc_offset_seconds": 0,
             "current_units": {key: definition[1] for key, definition in context.FIELDS.items()},
             "current": {"time": "2026-10-03T12:00", "temperature_2m": 14.0, "precipitation": 0.4,
                         "wind_speed_10m": 9.0, "weather_code": 61}} for _, lat, lon in context.POINTS]


def test_weather_model_is_real_context_and_never_sensor_evidence():
    result = context.parse_weather(response(), NOW)
    assert result["status"] == "available" and result["data_type"] == "model_estimate"
    assert result["mode"] == "real_context" and len(result["points"]) == 6
    assert all(point["observed_at"] is None and point["valid_at"].endswith("Z") for point in result["points"])
    assert context.parse_weather(response(), NOW + 14400)["status"] == "stale"
    assert context.CAMERAS["status"] == "links_only"
    assert all(item["url"].startswith("https://www.movilidadbogota.gov.co/") and "stream_url" not in item for item in context.CAMERAS["items"])


@pytest.mark.parametrize("change", [
    {"latitude": 0}, {"longitude": float("nan")}, {"utc_offset_seconds": -18000},
    {"current_units": {}}, {"current": {"time": "not-a-date"}},
])
def test_weather_rejects_wrong_location_timezone_and_invalid_values(change):
    payload = response()
    payload[0].update(change)
    with pytest.raises((ValueError, KeyError)):
        context.parse_weather(payload, NOW)


def test_weather_cache_coalesces_requests_then_marks_failure_stale(monkeypatch):
    clock, calls = [1.0], []
    monkeypatch.setattr(context, "_cache", None)
    monkeypatch.setattr(context, "_retry_at", 0)
    monkeypatch.setattr(context, "_lock", asyncio.Lock())
    monkeypatch.setattr(context.time, "monotonic", lambda: clock[0])
    async def fetch():
        calls.append(True)
        await asyncio.sleep(0)
        if len(calls) > 1:
            raise httpx.ReadTimeout("Not exposed to the UI")
        return context.parse_weather(response(), NOW)
    monkeypatch.setattr(context, "fetch_weather", fetch)
    async def run():
        first, second = await asyncio.gather(context.context_layers(), context.context_layers())
        assert len(calls) == 1 and first == second
        clock[0] = 902
        stale = (await context.context_layers())["weather"]
        assert stale["status"] == "stale" and stale["points"] == first["weather"]["points"]
        assert "Not exposed" not in str(stale)
        await context.context_layers()
        assert len(calls) == 2
        context._cache = None
        clock[0] = 1000
        unavailable = (await context.context_layers())["weather"]
        assert unavailable["status"] == "unavailable" and unavailable["points"] == []
    asyncio.run(run())


def test_authenticated_route_has_no_location_or_upstream_url_input(monkeypatch):
    from fastapi.testclient import TestClient
    sys.path.insert(0, str(Path(__file__).parents[1] / "app"))
    bridge_spec = importlib.util.spec_from_file_location("context_test_bridge", VIEWER / "server.py")
    bridge = importlib.util.module_from_spec(bridge_spec)
    bridge_spec.loader.exec_module(bridge)
    calls = []
    async def layers():
        calls.append(True)
        return {"weather": context.parse_weather(response(), NOW), "cameras": copy.deepcopy(context.CAMERAS)}
    monkeypatch.setattr(context, "context_layers", layers)
    client = TestClient(bridge.app)
    assert client.get("/api/territorial/context").status_code == 401 and not calls
    result = client.get("/api/territorial/context?url=http://localhost&latitude=0", headers={"x-prisma-user": "operator", "cookie": "session=local"})
    assert result.status_code == 200 and len(calls) == 1
    assert result.json()["weather"]["points"][0]["lat"] == context.POINTS[0][1]
