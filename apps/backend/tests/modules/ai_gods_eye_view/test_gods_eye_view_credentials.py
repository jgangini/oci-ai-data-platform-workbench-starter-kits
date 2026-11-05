import asyncio
import copy
import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.modules.ai_gods_eye_view.runtime.cloud import CloudRuntime
from app.modules.ai_gods_eye_view.runtime.core import PLATFORMS, default_source, aidp_credential_name
from app.modules.ai_gods_eye_view.runtime.local import LocalGodsEyeViewRuntime
from app.modules.ai_gods_eye_view.runtime.pipeline import _source_token


PREVIOUS_QUERY = "(BogotÃ¡ OR Bogota OR #Bogota) (inundaciÃ³n OR inundacion OR incendio OR deslizamiento OR derrumbe OR lluvia) -is:retweet"
QUERY = "#bogota #inundacion\n#colombia #incendio\n#desastre"








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
    from app.modules.ai_gods_eye_view.runtime import pipeline
    from app.modules.ai_gods_eye_view.runtime import control_store as database
    from app.modules.ai_gods_eye_view.runtime.control_store import ObjectControlStore
    from tests.modules.ai_gods_eye_view.test_gods_eye_view_control_store import Objects, ready
    objects = Objects()
    store = ObjectControlStore(objects, "namespace", "gold")
    ready(store)
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
    assert database.read_document(store, "runtime")["control_new_install"] is True


@pytest.mark.parametrize("workflow", ["social", "sensors"])
def test_stream_entrypoints_fail_before_spark_when_current_installation_receipt_is_missing(monkeypatch, workflow):
    from app.modules.ai_gods_eye_view.runtime import pipeline, sensor_pipeline, landing
    from tests.modules.ai_gods_eye_view.test_gods_eye_view_control_store import Objects
    monkeypatch.setitem(__import__("sys").modules, "oracledb", None)
    monkeypatch.setattr(landing, "ensure_volumes", lambda *_: pytest.fail("No Spark mutation before current installation receipt"))
    monkeypatch.setattr(pipeline, "DeltaLake", lambda *_: pytest.fail("No Spark mutation before current installation receipt"))
    config = {"namespace": "namespace", "bucket": "gold", "analytics_store": "gold"}
    arguments = {"objects": Objects()}
    if workflow == "social":
        arguments.update(client=object(), classifier=object())
    with pytest.raises(RuntimeError, match="installation receipt"):
        (pipeline if workflow == "social" else sensor_pipeline).run(None, None, config, **arguments)


@pytest.mark.parametrize("workflow", ["social", "sensors"])
def test_both_streams_use_only_shared_oci_credentials_without_database_fallback(monkeypatch, workflow):
    from app.modules.ai_gods_eye_view.runtime import pipeline, sensor_pipeline, runtime_secrets
    names = []
    def failed_auth(_get, region, name, identity):
        names.append(name)
        raise PermissionError("Shared OCI credential unavailable")
    monkeypatch.setattr(pipeline, "runtime_auth", failed_auth)
    monkeypatch.setattr(runtime_secrets, "runtime_auth", failed_auth)
    with pytest.raises(PermissionError):
        (pipeline if workflow == "social" else sensor_pipeline).run(None, None, {"region": "us-chicago-1"})
    assert names == ["AidpRuntime"]


def test_explicit_oci_credential_failure_never_tries_legacy_name():
    from app.modules.ai_gods_eye_view.runtime.runtime_secrets import runtime_auth
    calls = []

    def secret_get(*, name, key):
        calls.append((name, key))
        raise PermissionError("Credential unavailable")

    with pytest.raises(PermissionError):
        runtime_auth(secret_get, "us-chicago-1", "AidpRuntime")
    assert calls == [("AidpRuntime", "tenancy")]


def test_source_defaults_use_current_references():
    for platform in PLATFORMS:
        source = default_source(platform)
        assert source["secret_ref"] == f"gods-eye-view-{platform}"
        assert source["query"] == QUERY
