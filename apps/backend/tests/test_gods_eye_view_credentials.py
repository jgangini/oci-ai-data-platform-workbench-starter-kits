import asyncio
import copy
import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.gods_eye_view.cloud import CloudRuntime
from app.gods_eye_view.core import PLATFORMS, default_source, source_migration, aidp_credential_name
from app.gods_eye_view.local import LocalGodsEyeViewRuntime
from app.gods_eye_view.pipeline import _source_token


PREVIOUS_QUERY = "(Bogotá OR Bogota OR #Bogota) (inundación OR inundacion OR incendio OR deslizamiento OR derrumbe OR lluvia) -is:retweet"
QUERY = "#bogota #inundacion\n#colombia #incendio\n#desastre"


def test_source_defaults_migrate_only_unchanged_values():
    for platform in PLATFORMS:
        source = default_source(platform)
        assert source["secret_ref"] == f"gods-eye-view-{platform}" and source["query"] == QUERY
        legacy = {**source, "secret_ref": f"prisma-{platform}", "query": PREVIOUS_QUERY if platform == "x" else ""}
        assert source_migration(platform, legacy) == {"query": QUERY, "secret_ref": source["secret_ref"]}
        assert source_migration(platform, {**legacy, "query": "#Kennedy", "credential_configured": True}) == {}


def test_local_migration_preserves_real_credentials_until_explicit_rotation(tmp_path):
    runtime = LocalGodsEyeViewRuntime(tmp_path)
    runtime.store.update_source("x", {"secret_ref": "prisma-x", "query": "#custom", "credential_configured": False})
    runtime.credentials.put("prisma-x", "old-test-token")
    runtime.store.update_source("facebook", {"secret_ref": "prisma-facebook", "query": ""})
    restarted = LocalGodsEyeViewRuntime(tmp_path)
    preserved = restarted.store.source("x")
    assert preserved["secret_ref"] == "prisma-x" and preserved["credential_configured"] is True
    assert preserved["query"] == "#custom" and restarted.credentials.get("prisma-x") == "old-test-token"
    migrated = restarted.store.source("facebook")
    assert migrated["secret_ref"] == "gods-eye-view-facebook" and migrated["query"] == QUERY
    rotated = asyncio.run(restarted.update_source("x", {"bearer_token": "new-test-token"}))
    assert rotated["secret_ref"] == "gods-eye-view-x" and rotated["credential_configured"] is True
    assert restarted.credentials.get(rotated["secret_ref"]) == "new-test-token"
    assert b"new-test-token" not in (tmp_path / "prisma.sqlite3").read_bytes()


def test_cloud_reference_is_persisted_and_rotation_reuses_existing_credential():
    documents = {"configuration": {"revision": 1, "sources": {
        "x": {**default_source("x"), "secret_ref": "PrismaSource_x", "credential_configured": True, "query": "#custom"},
        "facebook": {**default_source("facebook"), "secret_ref": "prisma-facebook", "query": ""},
    }}}
    calls = []
    class Client:
        def _list(self, path, **kwargs):
            calls.append(("GET", path, kwargs))
            return [{"displayName": "PrismaSource_x", "key": "legacy"}]
        def _request(self, method, path, **kwargs):
            calls.append((method, path, kwargs))
            return {"key": "credential"}
    runtime = object.__new__(CloudRuntime)
    runtime.aidp_factory = Client
    runtime._doc = lambda name: copy.deepcopy(documents.get(name, {"revision": 0}))
    runtime._documents = lambda names: {name: runtime._doc(name) for name in names}
    def change(name, mutate):
        documents[name] = {**mutate(runtime._doc(name)), "revision": documents.get(name, {}).get("revision", 0) + 1}
        return copy.deepcopy(documents[name])
    runtime._change = change
    sources = runtime._sources()["sources"]
    assert sources[0]["secret_ref"] == "PrismaSource_x" and sources[0]["query"] == "#custom"
    migrated = documents["configuration"]["sources"]["facebook"]
    assert migrated["secret_ref"] == "gods-eye-view-facebook" and migrated["query"] == QUERY
    assert migrated["query_version"]
    revision = documents["configuration"]["revision"]
    runtime._sources()
    assert documents["configuration"]["revision"] == revision  # Idempotent migration.
    rotated = runtime._update("x", {"bearer_token": "new-test-token"})
    assert rotated["secret_ref"] == "PrismaSource_x"
    assert calls[-1][:2] == ("PUT", "/credentials/legacy")
    assert calls[-1][2]["payload"]["displayName"] == "PrismaSource_x"
    assert "new-test-token" not in json.dumps(documents)


def test_public_alias_uses_same_aidp_name_for_creation_update_and_spark_read():
    calls, existing = [], []
    class Client:
        def _list(self, path, **kwargs):
            assert kwargs["params"] == {"displayName": "gods_eye_view_x"}
            return existing
        def _request(self, method, path, **kwargs):
            calls.append((method, path, kwargs["payload"]))
    runtime = object.__new__(CloudRuntime)
    runtime.aidp_factory = Client
    assert runtime._credential("x", "test-only") == "gods-eye-view-x"
    assert calls[-1][:2] == ("POST", "/credentials")
    assert calls[-1][2]["displayName"] == "gods_eye_view_x"
    existing.append({"displayName": "gods_eye_view_x", "key": "existing"})
    assert runtime._credential("x", "rotated-test-only") == "gods-eye-view-x"
    assert calls[-1][:2] == ("PUT", "/credentials/existing")
    def secret_get(*, name, key):
        assert (name, key) == ("gods_eye_view_x", "bearer_token")
        return "test-only"
    assert _source_token(secret_get, {**default_source("x"), "credential_configured": True}) == "test-only"
    existing.append({"displayName": "gods_eye_view_x", "key": "duplicate"})
    with pytest.raises(HTTPException) as caught:
        runtime._credential("x", "unused")
    assert caught.value.status_code == 409 and len(calls) == 2


@pytest.mark.parametrize("reference", ["gods-eye-view-facebook", "../PrismaWriterRuntime", "PrismaSource_x\n", "custom-name", None])
def test_credential_mapping_rejects_wrong_platform_alias_or_invalid_identifier(reference):
    with pytest.raises(ValueError):
        aidp_credential_name("x", reference)


@pytest.mark.parametrize("writer", [None, "TerritorialWriterRuntime", "AidpControlStore"])
def test_social_runtime_and_callback_use_object_controls_even_with_stale_writer_config(monkeypatch, writer):
    from app.gods_eye_view import pipeline, database
    from app.gods_eye_view.control_store import ObjectControlStore
    from test_gods_eye_view_control_store import Objects, ready
    objects = Objects()
    store = ObjectControlStore(objects, "namespace", "gold")
    ready(store, synthetic_reset_version=2)
    monkeypatch.setitem(__import__("sys").modules, "oracledb", None)
    monkeypatch.setattr(pipeline, "process_reset", lambda *_: None)
    monkeypatch.setattr(pipeline, "_tick", lambda *_: {"version": "test"})
    seen = []
    monkeypatch.setattr(pipeline, "upsert_posts", lambda connection, *_args, **_kwargs: seen.append(connection))
    lake = SimpleNamespace()
    config = {"namespace": "namespace", "bucket": "gold", "analytics_store": "gold", "writer_credential_name": writer}
    pipeline.run(None, lambda **_: pytest.fail("No credential needed for injected clients"), config,
                 objects=objects, lake=lake, client=object(), classifier=object())
    lake.on_ingested([], "test-batch")
    assert len(seen) == 1 and isinstance(seen[0], ObjectControlStore)
    assert seen[0].objects is objects and seen[0].bucket == "gold"
    assert database.read_document(store, "runtime")["control_migration_complete"] is True


@pytest.mark.parametrize("workflow", ["social", "sensors"])
def test_stream_entrypoints_fail_before_spark_when_object_migration_is_missing(monkeypatch, workflow):
    from app.gods_eye_view import pipeline, sensor_pipeline, landing
    from test_gods_eye_view_control_store import Objects
    monkeypatch.setitem(__import__("sys").modules, "oracledb", None)
    monkeypatch.setattr(landing, "ensure_volumes", lambda *_: pytest.fail("No Spark mutation before migration"))
    monkeypatch.setattr(pipeline, "DeltaLake", lambda *_: pytest.fail("No Spark mutation before migration"))
    config = {"namespace": "namespace", "bucket": "gold", "analytics_store": "gold"}
    arguments = {"objects": Objects()}
    if workflow == "social":
        arguments.update(client=object(), classifier=object())
    with pytest.raises(RuntimeError, match="migration"):
        (pipeline if workflow == "social" else sensor_pipeline).run(None, None, config, **arguments)


@pytest.mark.parametrize("workflow", ["social", "sensors"])
def test_both_streams_use_only_shared_oci_credentials_without_database_fallback(monkeypatch, workflow):
    from app.gods_eye_view import pipeline, sensor_pipeline, runtime_secrets
    names = []
    def failed_auth(_get, region, name, identity):
        names.append(name)
        raise PermissionError("Shared OCI credential unavailable")
    monkeypatch.setattr(pipeline, "runtime_auth", failed_auth)
    monkeypatch.setattr(runtime_secrets, "runtime_auth", failed_auth)
    monkeypatch.setattr(runtime_secrets, "database_connection", lambda *_: pytest.fail("No database credential"))
    with pytest.raises(PermissionError):
        (pipeline if workflow == "social" else sensor_pipeline).run(None, None, {"region": "us-chicago-1"})
    assert names == ["AidpRuntime"]


def test_explicit_oci_credential_failure_never_tries_legacy_name():
    from app.gods_eye_view.runtime_secrets import runtime_auth
    calls = []

    def secret_get(*, name, key):
        calls.append((name, key))
        raise PermissionError("Credential unavailable")

    with pytest.raises(PermissionError):
        runtime_auth(secret_get, "us-chicago-1", "AidpRuntime")
    assert calls == [("AidpRuntime", "tenancy")]
