"""Bounded table responses and reuse of immutable, authenticated publications."""
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import create_app
from app.gods_eye_view.cloud import CloudRuntime
from app.gods_eye_view.api import runtime_for
from app.gods_eye_view.sensors import readings_page
from test_gods_eye_view_access import admin_login, local_settings
from test_gods_eye_view_source_reads import reader


def sample():
    return {"version": "one", "evidence": [{"text": "not part of a sensor page"}], "sensors": [
        {"id": f"event-{index:04}", "sensor_id": f"station-{index:04}", "sensor_type": "river_level",
         "observed_at": f"2026-10-05T12:{index % 60:02}:00Z", "locality": "Kennedy", "municipality": "Bogotá",
         "department": "Bogotá D.C.", "status": "critical" if index % 2 else "normal", "value": index,
         "unit": "m", "private_metadata": "not a table field"} for index in range(3200)]}


def test_sensor_page_bounds_transfer_and_preserves_search_deduplication_and_order():
    data = sample()
    data["sensors"].append({**data["sensors"][0], "id": "old", "observed_at": "2026-10-01T00:00:00Z"})
    original = deepcopy(data)
    first = readings_page(data)
    assert first["total"] == 3200 and len(first["items"]) == 20
    assert len(json.dumps(first)) < 15000 and "evidence" not in first
    assert all("private_metadata" not in row for row in first["items"])
    second = readings_page(data, page=2)
    assert not {row["id"] for row in first["items"]}.intersection(row["id"] for row in second["items"])
    assert readings_page(data, query="BOGOTA", status="critical")["total"] == 1600
    assert readings_page(data, query="River Level")["total"] == 3200
    assert readings_page(data, family="rainfall")["items"] == []
    assert readings_page(data, query="station-0000", page=999)["page"] == 1
    assert readings_page(data, query="station-0000")["items"][0]["id"] == "event-0000"
    assert readings_page(data, order="asc")["items"][0]["id"] == "event-0000"
    assert data == original


def test_sensor_page_utc_offsets_and_equal_time_ids():
    rows = sample()["sensors"][:2]
    rows[0]["observed_at"] = "2026-10-05T07:00:00-05:00"
    rows[1]["observed_at"] = "2026-10-05T12:00:00Z"
    for order in ("asc", "desc"):
        assert [row["id"] for row in readings_page({"sensors": rows}, order=order)["items"]] == [row["id"] for row in rows]


def test_sensor_route_requires_admin_and_validates_filters(monkeypatch, tmp_path):
    app = create_app(local_settings(tmp_path))
    runtime = runtime_for(app)
    monkeypatch.setattr(runtime, "snapshot", AsyncMock(return_value=sample()))
    with TestClient(app) as client:
        url = "/api/admin/gods-eye-view/sensors/readings"
        assert client.get(url).status_code == 401
        admin_login(client)
        result = client.get(url, params={"limit": 10, "page": 2, "q": "bogota"})
        assert result.status_code == 200 and len(result.json()["items"]) == 10 and result.json()["page"] == 2
        for params in ({"limit": 101}, {"page": 0}, {"family": "unknown"}, {"status": "unknown"}, {"q": "x" * 201}):
            assert client.get(url, params=params).status_code == 422


def test_snapshot_cache_rechecks_pointer_isolates_callers_and_rejects_incomplete_version(monkeypatch, tmp_path):
    docs = {"pointer": {"version": "one", "snapshot_key": "04_gold/prisma/snapshots/one.json"}, "snapshot": sample()}
    calls = []
    def read(namespace, bucket, key):
        calls.append(key)
        document = docs["pointer"] if key.endswith("current.json") else docs["snapshot"]
        return SimpleNamespace(data=SimpleNamespace(content=json.dumps(document).encode()))
    settings = SimpleNamespace(autonomous_runtime_file=str(tmp_path / "runtime.json"), objectstorage_namespace="ns", bucket_name="bucket")
    runtime = CloudRuntime(settings, lambda: SimpleNamespace(object_storage=SimpleNamespace(get_object=read)))
    monkeypatch.setattr(runtime, "_doc", lambda _: {})
    with ThreadPoolExecutor(max_workers=4) as workers:
        results = list(workers.map(lambda _: runtime._snapshot(), range(4)))
    assert calls.count("04_gold/prisma/current.json") == 4
    assert calls.count("04_gold/prisma/snapshots/one.json") == 1
    results[0]["sensors"].clear()
    assert len(runtime._snapshot()["sensors"]) == 3200
    docs["pointer"] = {"version": "two", "snapshot_key": "04_gold/prisma/snapshots/two.json"}
    with pytest.raises(HTTPException, match="Incomplete"):
        runtime._snapshot()
    docs["snapshot"] = {**docs["snapshot"], "version": "two"}
    assert runtime._snapshot()["version"] == "two"


def test_sensor_configuration_uses_one_connection_and_still_reads_cancelled_state(monkeypatch):
    documents = {"configuration": {"revision": 1}, "checkpoint_reset": {
        "revision": 2, "operation_id": "stopped", "sensor_type": "all", "status": "cancelled"}}
    runtime, calls = reader(monkeypatch, documents)
    result = asyncio.run(runtime.sensors())
    assert result["reset"]["status"] == "cancelled"
    assert calls == ["connect", ("configuration", "status_sensors", "checkpoint_sensors", "checkpoint_reset"), "close"]
