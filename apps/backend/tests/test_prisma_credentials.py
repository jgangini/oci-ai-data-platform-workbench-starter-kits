import asyncio
import copy
import json

from app.prisma.cloud import CloudRuntime
from app.prisma.core import PLATFORMS, default_source, source_migration
from app.prisma.local import LocalPrismaRuntime


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
    runtime = LocalPrismaRuntime(tmp_path)
    runtime.store.update_source("x", {"secret_ref": "prisma-x", "query": "#custom", "credential_configured": False})
    runtime.credentials.put("prisma-x", "old-test-token")
    runtime.store.update_source("facebook", {"secret_ref": "prisma-facebook", "query": ""})
    restarted = LocalPrismaRuntime(tmp_path)
    preserved = restarted.store.source("x")
    assert preserved["secret_ref"] == "prisma-x" and preserved["credential_configured"] is True
    assert preserved["query"] == "#custom" and restarted.credentials.get("prisma-x") == "old-test-token"
    migrated = restarted.store.source("facebook")
    assert migrated["secret_ref"] == "gods-eye-view-facebook" and migrated["query"] == QUERY
    rotated = asyncio.run(restarted.update_source("x", {"bearer_token": "new-test-token"}))
    assert rotated["secret_ref"] == "gods-eye-view-x" and rotated["credential_configured"] is True
    assert restarted.credentials.get(rotated["secret_ref"]) == "new-test-token"
    assert b"new-test-token" not in (tmp_path / "prisma.sqlite3").read_bytes()


def test_cloud_reference_is_persisted_and_rotation_uses_actual_new_credential_name():
    documents = {"configuration": {"revision": 1, "sources": {
        "x": {**default_source("x"), "secret_ref": "PrismaSource_x", "credential_configured": True, "query": "#custom"},
        "facebook": {**default_source("facebook"), "secret_ref": "prisma-facebook", "query": ""},
    }}}
    calls = []
    class Client:
        def _list(self, path, **kwargs):
            calls.append(("GET", path, kwargs))
            return []
        def _request(self, method, path, **kwargs):
            calls.append((method, path, kwargs))
            return {"key": "credential"}
    runtime = object.__new__(CloudRuntime)
    runtime.aidp_factory = Client
    runtime._doc = lambda name: copy.deepcopy(documents.get(name, {"revision": 0}))
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
    assert rotated["secret_ref"] == "gods-eye-view-x"
    assert calls[-1][1] == "/credentials" and calls[-1][2]["payload"]["displayName"] == rotated["secret_ref"]
    assert "new-test-token" not in json.dumps(documents)
