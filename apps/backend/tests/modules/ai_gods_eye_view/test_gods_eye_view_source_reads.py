"""Source controls reuse one Object Storage handle without stale or partial responses."""
import asyncio
from contextlib import contextmanager

import pytest
from fastapi import HTTPException

from app.modules.ai_gods_eye_view.runtime.cloud import CloudRuntime
from app.modules.ai_gods_eye_view.runtime.core import PLATFORMS, default_source
from copy import deepcopy


def reader(monkeypatch, documents):
    runtime = object.__new__(CloudRuntime)
    calls = []

    class Connection:
        def get_json(self, key):
            name = key.removeprefix("docs/").removesuffix(".json")
            calls.append(name)
            return deepcopy(documents.get(name)), "etag"

    @contextmanager
    def connect():
        calls.append("connect")
        try:
            yield Connection()
        finally:
            calls.append("close")

    monkeypatch.setattr(runtime, "_connect", connect)
    return runtime, calls


def test_sources_share_one_connection_and_read_fresh_controls_on_each_request(monkeypatch):
    documents = {
        "configuration": {"revision": 3, "sources": {name: default_source(name) for name in PLATFORMS},
            "social_schedule": {"start_at": "2026-10-05T12:00:00Z", "interval_minutes": 7, "config_version": 2}},
        "simulation": {"revision": 1, "status": "paused", "elapsed_seconds": 25},
        "status_x": {"revision": 2, "status": "paused", "last_received_count": 17},
        "status_pipeline": {"revision": 4, "version": "gold-one", "pending_count": 3},
        "status_synthetic": {"revision": 5, "landing_count": 8},
        "checkpoint_reset": {"revision": 6, "status": "pending", "stage": "history", "operation_id": "operation"},
    }
    runtime, calls = reader(monkeypatch, documents)
    first = asyncio.run(runtime.sources())
    expected_reads = ["connect", * ("configuration", *("status_" + name for name in PLATFORMS),
        "simulation", "status_pipeline", "status_synthetic", "checkpoint_reset"), "close"]
    assert calls[0] == "connect" and calls[-1] == "close"
    assert sorted(calls[1:-1]) == sorted(expected_reads[1:-1])
    assert first["sources"][0]["last_received_count"] == 17
    assert first["simulation"]["elapsed_seconds"] == 25
    assert first["social_schedule"]["interval_minutes"] == 7
    assert first["pipeline"]["version"] == "gold-one"
    assert first["capture_summary"]["landing_count"] == 8
    assert first["synthetic_reset"]["status"] == "pending"

    documents["status_x"].update(revision=3, last_received_count=19)
    documents["status_pipeline"].update(revision=5, version="gold-two")
    documents["checkpoint_reset"].update(revision=7, status="completed")
    second = asyncio.run(runtime.sources())
    assert calls.count("connect") == calls.count("close") == 2
    assert sorted(calls[len(expected_reads) + 1:-1]) == sorted(expected_reads[1:-1])
    assert second["sources"][0]["last_received_count"] == 19
    assert second["pipeline"]["version"] == "gold-two"
    assert second["synthetic_reset"]["status"] == "completed"
    assert first["synthetic_reset"]["status"] == "pending"

    documents["checkpoint_reset"].update(sensor_type="rainfall", status="pending")
    assert asyncio.run(runtime.sources())["synthetic_reset"] == {}


def test_corrupt_document_closes_shared_connection_and_rejects_partial_sources(monkeypatch):
    runtime, calls = reader(monkeypatch, {"status_facebook": {"revision": None}})
    with pytest.raises(HTTPException) as error:
        asyncio.run(runtime.sources())
    assert error.value.status_code == 503
    assert isinstance(error.value.__cause__, ValueError)
    assert calls[0] == "connect" and calls[-1] == "close"
    assert calls.count("connect") == calls.count("close") == 1
    assert "status_facebook" in calls
    assert set(calls[1:-1]) <= {"configuration", *("status_" + name for name in PLATFORMS),
        "simulation", "status_pipeline", "status_synthetic", "checkpoint_reset"}
