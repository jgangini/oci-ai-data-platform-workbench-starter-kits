"""Execute the emitted business functions, without credentials, Spark jobs or models."""
import ast
import builtins
import copy
import io
import json
from pathlib import Path
import symtable
import sys
from types import ModuleType, SimpleNamespace
import zipfile

import pytest

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "terraform/hooks"))
sys.path.insert(0, str(ROOT / "apps/backend"))
from gods_eye_sources import workflow_source, source_fragments
from app.gods_eye_view import core, sensors, synthetic_reset

CONFIG = {"writer_credential_name": "AidpControlStore", "pipeline_revision": "a" * 64,
          "oci_credential_name": "AidpRuntime", "oci_identity_sha256": "b" * 64,
          "namespace": "fixture", "bucket": "gold", "analytics_store": "gold"}


@pytest.fixture(scope="module")
def bundle():
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        for path in (ROOT / "apps/backend/app/gods_eye_view").glob("*.py"):
            archive.writestr("gods_eye_view/" + path.name, path.read_bytes())
    return data.getvalue()


def emitted(bundle, module, monkeypatch):
    result = ModuleType("standalone_" + module)
    monkeypatch.setitem(sys.modules, result.__name__, result)
    exec(compile(workflow_source(module, CONFIG, bundle), module + ".py", "exec"), result.__dict__)
    return result


@pytest.mark.parametrize("module", ["sensor_pipeline", "pipeline"])
def test_standalone_has_no_project_loader_duplicate_defs_or_unbound_globals(bundle, module):
    source = workflow_source(module, CONFIG, bundle)
    tree = ast.parse(source)
    assert "import oracledb" not in source and "import sqlite3" not in source
    assert "database_connection" not in source and "wallet_password" not in source
    assert not any(isinstance(node, ast.ImportFrom) and (node.level or (node.module or "").startswith(("gods_eye_view", "app."))) for node in ast.walk(tree))
    assert not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"exec", "eval", "compile", "globals"} for node in ast.walk(tree))
    names = [node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))]
    assert len(names) == len(set(names))
    table = symtable.symtable(source, module + ".py", "exec")
    known = {symbol.get_name() for symbol in table.get_symbols() if symbol.is_assigned() or symbol.is_imported()} | set(vars(builtins)) | {"__name__", "aidputils"}
    def check(scope):
        assert not {symbol.get_name() for symbol in scope.get_symbols() if symbol.is_referenced() and symbol.is_global()} - known
        for child in scope.get_children():
            check(child)
    check(table)
    assert "runtime-root" not in source and "sys.path" not in source
    assert "generate_batch" not in source and "def install_schema" not in source


@pytest.mark.parametrize("module", ["sensor_pipeline", "pipeline"])
def test_native_startup_uses_injected_utilities_without_reading_secrets(bundle, module, monkeypatch, capsys):
    runtime = emitted(bundle, module, monkeypatch)
    monkeypatch.setitem(sys.modules, "aidputils", None)
    monkeypatch.setitem(sys.modules, "pyspark.sql", SimpleNamespace(
        SparkSession=SimpleNamespace(builder=SimpleNamespace(getOrCreate=lambda: object()))))
    runtime.aidputils = SimpleNamespace(secrets=SimpleNamespace(get=lambda *_: pytest.fail("Runtime check must not read secrets")))
    monkeypatch.setattr(sys, "argv", [module + ".py", "--check-runtime"])
    runtime.main()
    assert json.loads(capsys.readouterr().out)["status"] == "ready"
    del runtime.aidputils
    with pytest.raises(NameError, match="aidputils"):
        runtime.main()


def test_fragment_preserves_multiline_literals_comments_and_decorators(bundle):
    source = source_fragments(bundle, "x", "XFailure THIRD_PARTY")
    assert "@dataclass\nclass XFailure" in source.replace("\r\n", "\n")
    original = ast.parse((ROOT / "apps/backend/app/gods_eye_view/x.py").read_text(encoding="utf-8"))
    expected = next(node.value.value for node in original.body if isinstance(node, ast.Assign) and node.targets[0].id == "THIRD_PARTY")
    actual = next(node.value.value for node in ast.parse(source).body if isinstance(node, ast.Assign) and node.targets[0].id == "THIRD_PARTY")
    assert actual == expected
    assert "# These are simulation thresholds" in source_fragments(bundle, "sensors", "SENSOR_TYPES")
    with pytest.raises(ValueError, match="export"):
        source_fragments(bundle, "core", ["missing_export"])


def test_only_nonsecret_deployment_fields_are_embedded(bundle):
    # ponytail: a non-JSON sentinel fails immediately if credentials reach serialization.
    private_value = object()
    config = {**CONFIG, "private_key": private_value, "db_password": private_value}
    source = workflow_source("pipeline", config, bundle)
    assert config["private_key"] is private_value and config["db_password"] is private_value
    assert "AidpControlStore" not in source and "AidpRuntime" in source
    with pytest.raises(ValueError, match="explicit"):
        workflow_source("pipeline", {"pipeline_revision": "a"}, bundle)


def test_emitted_social_program_appends_posts_to_shared_object_journal(bundle, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "apps/backend/tests"))
    from test_gods_eye_view_control_store import Objects
    runtime = emitted(bundle, "pipeline", monkeypatch)
    store = runtime.ObjectControlStore(Objects(), "namespace", "gold")
    runtime.write_document(store, "runtime", {"analytics_store": "gold", "control_new_install": True}, 0)
    runtime.upsert_posts(store, [{"source_id": "one", "platform": "x", "created_at": "2026-10-05T00:00:00Z",
                                 "mode": "Synthetic"}], "captured", ingested_at="2026-10-05T00:00:00Z")
    head, _ = store.get_json("posts/head.json")
    node, _ = store.get_json("posts/nodes/" + head["node"] + ".json")
    assert node["event"]["records"][0]["id"] == "x:one"
    assert node["event"]["records"][0]["listing_published_at"] == "2026-10-05T00:00:00.000000+00:00"
    runtime.write_document(store, "checkpoint_reset", {"operation_id": "reset", "status": "pending", "ready": True}, 0)
    assert runtime.purge_synthetic_posts(store, "reset") is None


def test_sensor_decode_preserves_validation_provenance_and_reset_barrier(bundle, monkeypatch):
    runtime = emitted(bundle, "sensor_pipeline", monkeypatch)
    record = sensors.generate_batch(1791209100, sensor_count=100)[0]
    row = SimpleNamespace(value=json.dumps(record), source_object="/Volumes/test/prisma_ingest/landing/sensors/" + record["sensor_type"] + "/batch.txt")
    assert runtime.decode_batch([row], 1791209100)[0]["payload"] == json.dumps(sensors.validate_record(record), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    bad = SimpleNamespace(value=row.value, source_object="/sensors/not_a_family/batch.txt")
    with pytest.raises(ValueError, match="Landing family"):
        runtime.decode_batch([bad], 1791209100)
    calls = []
    query = SimpleNamespace(stop=lambda: calls.append("stop"), awaitTermination=lambda: calls.append("join"))
    monkeypatch.setattr(runtime, "read_document", lambda *_: {"sensor_type": "all", "status": "cancelled", "ready": True})
    assert runtime.reset_barrier(None, None, {}, query) == (query, False)
    assert calls == []


@pytest.mark.parametrize("kind,expected", [(None, "social"), ("river_level", "sensor"), ("all", "sensor")])
def test_social_reset_dispatch_keeps_distinct_executors(bundle, monkeypatch, kind, expected):
    runtime = emitted(bundle, "pipeline", monkeypatch)
    doc = {"status": "pending", "ready": True, "operation_id": "fixture", "sensor_type": kind,
           "sensor_drained_operation_id": "fixture", "sensor_drained_revision": CONFIG["pipeline_revision"]}
    monkeypatch.setattr(runtime, "read_document", lambda *_: doc)
    monkeypatch.setattr(runtime, "mutate_document", lambda _db, _name, change: change({}))
    calls = []
    for label in ("social", "sensor"):
        monkeypatch.setattr(runtime, "execute_" + label + "_reset", lambda *_, label=label: calls.append(label) or {"version": "gold-fixture"})
    assert runtime.process_reset(None, None, SimpleNamespace(pending_count=lambda: 0), CONFIG, 0)["version"] == "gold-fixture"
    assert calls == [expected]
    # The two similarly named encoders intentionally have different NaN policies.
    assert b"NaN" in runtime.encode_publication({"value": float("nan")})
    with pytest.raises(ValueError):
        runtime.encode_reset_publication({"value": float("nan")})


def test_social_publication_and_historical_pruning_match_original_business_functions(bundle, monkeypatch):
    runtime = emitted(bundle, "pipeline", monkeypatch)
    events = [core.normalize_event(row) for row in core.simulation_events(0)]
    expected = core.build_snapshot(events, {}, "version", "date", now=1791209100, sensors=[])
    assert runtime.build_snapshot(events, {}, "version", "date", now=1791209100, sensors=[]) == expected
    assert runtime.prune_publication(expected) == synthetic_reset.prune_publication(expected)
    writes, docs = [], {}
    monkeypatch.setattr(runtime, "read_document", lambda _db, key: copy.deepcopy(docs.get(key, {})))
    def mutate(_db, key, change):
        docs[key] = change(copy.deepcopy(docs.get(key, {})))
        return docs[key]
    monkeypatch.setattr(runtime, "mutate_document", mutate)
    monkeypatch.setattr(runtime, "publish", lambda *_: pytest.fail("Gold runtime must not write analytical ADB copies"))
    lake = SimpleNamespace(stage_snapshot=lambda value: value, put=lambda layer, rows: writes.append((layer, rows)))
    objects = SimpleNamespace(put_object=lambda _namespace, _bucket, key, data, **_: writes.append((key, json.loads(data))))
    snapshot = runtime.publish_snapshot(None, objects, lake, CONFIG, [], {}, {}, 1791209100)
    assert [key for key, _ in writes] == ["gold", "04_gold/prisma/snapshots/" + snapshot["version"] + ".json", "04_gold/prisma/current.json"]
    assert writes[-1][1]["version"] == snapshot["version"]


def test_enrichment_journals_valid_post_before_advancing_and_retains_failed_remainder(bundle, monkeypatch):
    runtime = emitted(bundle, "pipeline", monkeypatch)
    docs, writes = {}, []
    monkeypatch.setattr(runtime, "read_document", lambda _db, key: copy.deepcopy(docs.get(key, {})))
    def mutate(_db, key, change):
        docs[key] = change(copy.deepcopy(docs.get(key, {})))
        writes.append((key, copy.deepcopy(docs[key])))
        return docs[key]
    monkeypatch.setattr(runtime, "mutate_document", mutate)
    monkeypatch.setattr(runtime, "upsert_posts", lambda *_args, **_kwargs: None)
    events = [{"id": "a"}, {"id": "b"}]
    saved = []
    lake = SimpleNamespace(pending=lambda _: events, pending_count=lambda: len(events) - len(saved), put=lambda layer, rows: saved.extend(rows))
    def classify(rows):
        if rows[0]["id"] == "b":
            raise ValueError("invalid provider response")
        return rows
    result = runtime.enrich_pending(None, lake, classify, 100, 7)
    assert saved == [{"id": "a"}]
    assert writes[0][1]["prepared"] == [{"id": "a"}]
    assert docs["checkpoint_enrichment"]["pending_ids"] == ["b"]
    assert result["last_error"] == "ValueError" and result["pending_count"] == 1
