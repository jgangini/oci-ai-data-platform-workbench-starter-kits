"""Credential storage, admin boundary and worker reload checks use only local fakes."""
import importlib.util
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag
from fastapi import HTTPException
from fastapi.testclient import TestClient

VIEWER = Path(__file__).parents[2] / "prisma-viewer"
sys.path.insert(0, str(VIEWER))
from native import provider_runtime as runtime
from test_prisma_bridge import bridge

HEADERS = {"cookie": "admin-session=fixture-value"}


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setenv("GEV_PROVIDER_SETTINGS_DIR", str(tmp_path / "private-provider-settings"))
    runtime.metadata()
    return runtime.directory()


def envelope(provider="openai", revision="environment", **values):
    return runtime.seal({"provider_id": provider, "expected_revision": revision, "values": values}, runtime.private_key())


def test_metadata_and_encrypted_store_never_reveal_credentials(store, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "private-original")
    metadata = runtime.metadata()
    assert metadata["revision"] == "environment" and len(metadata["providers"]) == 8
    assert "private-original" not in json.dumps(metadata)
    assert "PRIVATE KEY" not in metadata["public_key"]
    revision = runtime.save("openai", envelope(OPENAI_API_KEY="private-replacement"))
    assert revision != "environment"
    assert "private-replacement" not in (store / "settings.json").read_text()
    assert runtime.environment(runtime.stored())["OPENAI_API_KEY"] == "private-replacement"
    assert runtime.environment(runtime.stored())["GEV_PROVIDER_REVISION"] == revision
    assert runtime.save("openai", envelope(revision=revision, OPENAI_API_KEY="private-replacement")) == revision
    if os.name != "nt":
        assert store.stat().st_mode & 0o777 == 0o700
        assert all((store / name).stat().st_mode & 0o777 == 0o600 for name in ("settings.json", "key.pem"))


def test_testing_is_ephemeral_and_clear_disables_environment_fallback(store, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "original")
    tested = runtime.test_environment("openai", envelope(OPENAI_API_KEY="draft-only"))
    assert tested["OPENAI_API_KEY"] == "draft-only" and not (store / "settings.json").exists()
    runtime.save("openai", envelope(OPENAI_API_KEY=None))
    assert runtime.environment(runtime.stored())["OPENAI_API_KEY"] == ""


@pytest.mark.parametrize("values", [{"GOOGLE_MAPS_API_KEY": "wrong-provider"}, {"OPENAI_API_KEY": "line\nbreak"},
                                      {"OPENAI_API_KEY": 123}, {"OPENAI_API_KEY": "x" * 513}, {"NODE_OPTIONS": "--inspect"}])
def test_only_bounded_provider_credentials_can_be_saved(store, values):
    with pytest.raises(ValueError):
        runtime.save("openai", envelope(**values))
    assert not (store / "settings.json").exists()


def test_ciphertext_integrity_and_provider_binding(store):
    sealed = envelope(OPENAI_API_KEY="secret")
    sealed["ciphertext_b64"] = "AA=="
    with pytest.raises(InvalidTag):
        runtime.save("openai", sealed)
    with pytest.raises(ValueError):
        runtime.save("tomtom", envelope(OPENAI_API_KEY="secret"))
    assert not (store / "settings.json").exists()


def test_concurrent_saves_compare_revision_before_merging(store):
    requests = [envelope(OPENAI_API_KEY=value) for value in ("first", "second")]
    def attempt(sealed):
        try:
            return runtime.save("openai", sealed)
        except runtime.RevisionConflict:
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, requests))
    assert results.count("conflict") == 1
    assert runtime.environment(runtime.stored())["OPENAI_API_KEY"] in {"first", "second"}


def test_existing_encrypted_store_cannot_regenerate_a_missing_key(store):
    runtime.save("openai", envelope(OPENAI_API_KEY="saved"))
    (store / "key.pem").unlink()
    with pytest.raises(ValueError, match="missing"):
        runtime.metadata()
    assert not (store / "key.pem").exists()


def test_failed_atomic_save_preserves_previous_encrypted_document(store, monkeypatch):
    revision = runtime.save("openai", envelope(OPENAI_API_KEY="previous"))
    failed = envelope(revision=revision, OPENAI_API_KEY="replacement")
    monkeypatch.setattr(runtime.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("read only")))
    with pytest.raises(OSError):
        runtime.save("openai", failed)
    assert runtime.environment(runtime.stored())["OPENAI_API_KEY"] == "previous"
    assert runtime.stored()["revision"] == revision
    assert not list(store.glob(".provider-*"))


def test_corrupt_storage_fails_closed_and_worker_mismatch_is_pending(store, monkeypatch):
    revision = runtime.save("openai", envelope(OPENAI_API_KEY="saved"))
    monkeypatch.setattr(bridge.native_proxy, "native_json", lambda path: {"provider_revision": "environment"})
    response = bridge.provider_metadata()
    assert response["revision"] == revision and response["runtime_status"] == "pending"
    document = json.loads((store / "settings.json").read_text())
    document["ciphertext_b64"] = "AA=="
    (store / "settings.json").write_text(json.dumps(document))
    with pytest.raises(HTTPException) as failed:
        bridge.provider_metadata()
    assert failed.value.status_code == 503


def test_private_routes_require_current_admin_before_storage_or_probe(monkeypatch):
    monkeypatch.setattr(runtime, "metadata", lambda: pytest.fail("Unauthenticated storage access"))
    async def denied(request, method, path, payload=None):
        assert path == "/api/admin/session" and method == "GET"
        raise HTTPException(403, "Administrator required")
    monkeypatch.setattr(bridge, "admin_request", denied)
    with TestClient(bridge.app) as client:
        assert client.get("/internal/provider-parameters").status_code == 401
        assert client.get("/internal/provider-parameters", headers=HEADERS).status_code == 403
        assert client.put("/internal/provider-parameters/openai", headers=HEADERS, json={}).status_code == 403


def test_browser_proxy_cannot_reach_internal_provider_routes_even_with_admin_cookie(monkeypatch):
    async def forbidden(*_args, **_kwargs):
        pytest.fail("Browser proxy request reached the private admin callback")
    monkeypatch.setattr(bridge, "admin_request", forbidden)
    headers = {**HEADERS, "x-prisma-user": "operator"}
    with TestClient(bridge.app) as client:
        assert client.get("/internal/provider-parameters", headers=headers).status_code == 404
        assert client.put("/internal/provider-parameters/openai", headers=headers, json={}).status_code == 404
        assert client.post("/internal/provider-parameters/openai/test", headers=headers, json={}).status_code == 404


def test_bridge_applies_sealed_save_and_reports_revision_without_secret(store, monkeypatch):
    async def accepted(request, method, path, payload=None):
        assert path == "/api/admin/session"
        return {"username": "operator"}
    monkeypatch.setattr(bridge, "admin_request", accepted)
    monkeypatch.setattr(bridge.native_proxy, "native_json", lambda path: {"provider_revision": runtime.stored()["revision"]})
    with TestClient(bridge.app) as client:
        response = client.put("/internal/provider-parameters/openai", headers=HEADERS, json=envelope(OPENAI_API_KEY="private-value"))
        assert response.status_code == 200 and response.json()["runtime_status"] == "applied"
        assert "private-value" not in response.text and response.headers["cache-control"] == "no-store"
        assert client.put("/internal/provider-parameters/openai", headers=HEADERS, json=envelope(OPENAI_API_KEY="stale")).status_code == 409
        assert client.put("/internal/provider-parameters/openai", headers=HEADERS, content=b"x" * 49153).status_code == 413


@pytest.mark.parametrize("failure", ["none", "corrupt", "stat", "read"])
def test_supervisor_restarts_only_native_worker_and_uses_current_environment(monkeypatch, capsys, failure):
    sys.path.insert(0, str(VIEWER / "native"))
    spec = importlib.util.spec_from_file_location("provider_supervisor", VIEWER / "native" / "entrypoint.py")
    supervisor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(supervisor)
    calls, handlers, sleeps = [], {}, []
    class Child:
        def __init__(self, command, **kwargs):
            self.command, self.kwargs, self.stopped = command, kwargs, False
            calls.append(self)
        def terminate(self): self.stopped = True
        def kill(self): self.stopped = True
        def wait(self, **kwargs): return 0
        def poll(self): return 0 if self.stopped else None
    failed_signatures = {"none": [], "corrupt": ["corrupt"], "stat": [PermissionError("private credential error")],
                         "read": ["new", "new"]}[failure]
    signatures = iter([None, None, *failed_signatures, "new"])
    failed_documents = [None] * {"none": 0, "corrupt": 1, "stat": 0, "read": 2}[failure]
    documents = iter([{"revision": "environment", "values": {}}, *failed_documents,
                      {"revision": "new", "values": {"OPENAI_API_KEY": "replacement"}}])
    def document():
        value = next(documents)
        if value is None:
            if failure == "read":
                raise PermissionError("private credential error")
            raise ValueError("private credential error")
        return value
    monkeypatch.setattr(supervisor, "stored", document)
    def signature():
        value = next(signatures)
        if isinstance(value, OSError):
            raise value
        return value
    monkeypatch.setattr(supervisor, "file_signature", signature)
    monkeypatch.setattr(supervisor.subprocess, "Popen", Child)
    monkeypatch.setattr(supervisor.signal, "signal", lambda number, handler: handlers.setdefault(number, handler))
    def advance(_seconds):
        sleeps.append(True)
        if 1 < len(sleeps) < len(failed_signatures) + 2:
            assert len(calls) == 2 and all(not child.stopped for child in calls)
        if len(sleeps) == len(failed_signatures) + 2:
            assert len(calls) == 3 and not calls[1].stopped
            handlers[supervisor.signal.SIGTERM](None, None)
    monkeypatch.setattr(supervisor.time, "sleep", advance)
    assert supervisor.supervise([["node"], ["bridge"]]) == 0
    assert [child.command for child in calls] == [["node"], ["bridge"], ["node"]]
    assert calls[2].kwargs["env"]["OPENAI_API_KEY"] == "replacement"
    assert calls[2].kwargs["env"]["GEV_PROVIDER_REVISION"] == "new"
    assert calls[1].kwargs["env"] is None
    logs = capsys.readouterr().err
    assert "private credential error" not in logs
    assert logs.count("Provider settings reload failed") == int(failure in {"corrupt", "read"})


def test_corrupt_provider_document_prevents_cold_worker_start(monkeypatch):
    sys.path.insert(0, str(VIEWER / "native"))
    spec = importlib.util.spec_from_file_location("provider_supervisor_cold", VIEWER / "native" / "entrypoint.py")
    supervisor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(supervisor)
    monkeypatch.setattr(supervisor, "file_signature", lambda: "corrupt")
    monkeypatch.setattr(supervisor, "stored", lambda: (_ for _ in ()).throw(InvalidTag()))
    monkeypatch.setattr(supervisor.subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("Corrupt settings started a worker"))
    with pytest.raises(InvalidTag):
        supervisor.supervise([["node"], ["bridge"]])
