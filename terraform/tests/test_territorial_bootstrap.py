"""Offline native AIDP contract checks; no local test claims remote readiness."""
import ast
import hashlib
from contextlib import nullcontext
import io
import json
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "hooks"))
import territorial_bootstrap as bootstrap
import post_apply


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("governance_exists", [False, True])
def test_control_writer_is_separate_and_legacy_reader_is_preserved(monkeypatch, tmp_path, existing, governance_exists):
    api, connection = Api(), MagicMock()
    connection.cursor.return_value.fetchone.return_value = (0,)
    monkeypatch.setitem(sys.modules, "oracledb", SimpleNamespace(connect=lambda **_: nullcontext(connection)))
    monkeypatch.setattr(bootstrap, "install_schema", lambda _: None)
    if existing:
        api.resources["/credentials"] = [{"key": name, "displayName": name, "type": "SECRET_TOKEN", "lifeCycleState": "ACTIVE"}
                                        for name in ("PrismaWriterRuntime", "PrismaReaderRuntime")]
    if governance_exists:
        api.resources.setdefault("/credentials", []).append({"key": "governance", "displayName": "AidpDataGovernanceExtension",
            "credentialType": "SECRET_TOKEN", "lifeCycleState": "ACTIVE"})
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as wallet:
        wallet.writestr("tnsnames.ora", "test")
    key = tmp_path / "fake-key.pem"
    key.write_text("test fixture only", encoding="utf-8")
    passwords = MagicMock(return_value="generated-fixture")
    bootstrap.database_users(api, archive.getvalue(), "wallet-fixture", "admin-fixture",
        {"region": "test", "tenancy": "test", "user": "test", "fingerprint": "test", "key_file": str(key)},
        {"compartment_ocid": "test", "agent_model_id": "test"}, wallet_dsn=lambda _: "test",
        validate_wallet=lambda value: value, generate_password=passwords)
    statements = [entry.args[0] for entry in connection.cursor.return_value.execute.call_args_list]
    assert not any("GRANT SELECT" in sql or "PRISMA_READER" in sql for sql in statements)
    assert passwords.call_count == (0 if existing else 1)
    if existing:
        assert all(method == "GET" for method, *_ in api.calls)
        assert not any(sql.startswith(("CREATE USER", "ALTER USER")) for sql in statements)
    else:
        writer = next(payload for method, path, payload, _ in api.calls if method == "POST" and path == "/credentials" and payload["displayName"] == "AidpControlStore")
        fields = {item["secretKey"] for item in writer["credentialDetails"]["secretTokenPair"]}
        assert not {"private_key", "tenancy", "user", "fingerprint"}.intersection(fields)
        assert {"db_user", "db_password", "dsn", "wallet", "wallet_password"} <= fields


def stream_runtime(values=None):
    return {"namespace": "namespace", "bucket": "gold", "pipeline_revision": "a" * 64,
            "oci_credential_name": "AidpRuntime", "oci_identity_sha256": "b" * 64, **(values or {})}


class Api:
    def __init__(self, *_args, **kwargs):
        self.calls, self.contents, self.resources = [], {}, {}
        self.deployment_id = "deployment-test"
        self.deploy_state = "ACTIVE"
        self.run_state = "SUCCESS"
        self.job_timeout_default = 0
        self.base = "https://datalake.us-chicago-1.oci.oraclecloud.com/20240831/dataLakes/platform"
        self.api_version = kwargs.get("api_version", "20240831")
        self.resource_segment = kwargs.get("resource_segment", "dataLakes")

    def request(self, method, path, *, payload=None, params=None, headers=None):
        self.calls.append((method, path, payload, headers))
        response = lambda body: SimpleNamespace(body=body, headers={}, status_code=200)
        if "/notebook/api/actions/export/contents/" in path:
            return response(self.contents[path.replace("/actions/export", "")])
        if "/notebook/api/contents/" in path:
            if method == "POST":
                return response({"path": "/Workspace/medallon/prisma/Untitled.ipynb"})
            if method == "PATCH":
                return response({})
            if method == "PUT":
                self.contents[path] = payload
            if path not in self.contents:
                raise post_apply.ApiRequestError(method, path, 404, "test")
            return response(self.contents[path])
        if method == "GET" and path == "/asyncOperations/operation":
            return response({"status": "SUCCEEDED"})
        if path.endswith(("/actions/deploy", "/actions/redeploy")):
            self.resources[path.rsplit("/actions/", 1)[0]] = [{"key": "deployment", "lifecycleState": "DEPLOYING", **payload}]
            return SimpleNamespace(body={}, headers={"aidp-async-operation-key": "operation"})
        if path.endswith("/deployments/deployment"):
            return response({**self.resources[path.rsplit("/", 1)[0]][0], "key": "deployment", "lifecycleState": self.deploy_state,
                "endpointUrl": "https://gateway.aidp.us-chicago-1.oci.oraclecloud.com/agentendpoint/managed/chat",
                "sessionRetentionConfig": {"retentionPeriodInDays": 7, "unused": None}})
        if path.endswith("/jobRuns") and method == "POST":
            return response({"key": "run-one"})
        if path.endswith("/jobRuns/run-one"):
            return response({"state": {"status": self.run_state}})
        if path.endswith("/taskRuns"):
            assert params["limit"] == 100, "Native task-run API rejects limit=1000"
            return response({"items": [{"taskKey": "social_network", "state": {"status": self.run_state}}]})
        if method == "PUT" and "/jobs/" in path:
            assert "timeoutSeconds" not in payload or payload["timeoutSeconds"] >= 60
            self.resources[path].update(payload)
            if "timeoutSeconds" not in payload:
                self.resources[path]["timeoutSeconds"] = self.job_timeout_default
            return response(self.resources[path])
        if method == "PUT" and "/agents/" in path:
            self.resources[path].update(payload)
            return response(self.resources[path])
        if method == "POST":
            state_field = "lifeCycleState" if path == "/credentials" else "lifecycleState"
            value = {**payload, "key": "key-" + (payload.get("displayName") or payload["name"]), state_field: "ACTIVE"}
            self.resources.setdefault(path, []).append(value)
            self.resources[path + "/" + value["key"]] = value
            return response(value)
        if method == "GET":
            return response(self.resources.get(path, []))
        raise AssertionError((method, path))


@pytest.mark.parametrize("existing", [None, "AidpRuntime", "AidpDataGovernanceExtension", "TerritorialWriterRuntime", "PrismaWriterRuntime"])
def test_shared_api_credential_is_created_once_or_adopted_without_rotation(tmp_path, existing):
    api = Api()
    if existing:
        api.resources["/credentials"] = [{"key": "existing", "displayName": existing, "type": "SECRET_TOKEN", "lifeCycleState": "ACTIVE"}]
    key = tmp_path / "fixture.pem"
    adopted = existing in {"AidpRuntime", "AidpDataGovernanceExtension"}
    if not adopted:
        key.write_text("fixture only", encoding="utf-8")
    config = {"tenancy": "tenancy", "user": "user", "fingerprint": "fingerprint", "region": "region", "key_file": str(key)}
    first = bootstrap.ensure_oci_credential(api, config)
    assert bootstrap.ensure_oci_credential(api, config) == first
    assert first["displayName"] == (existing if adopted else "AidpRuntime")
    writes = [payload for method, path, payload, _ in api.calls if method != "GET"]
    assert len(writes) == (0 if adopted else 1)
    if writes:
        keys = {item["secretKey"] for item in writes[0]["credentialDetails"]["secretTokenPair"]}
        assert keys == {"tenancy", "user", "fingerprint", "region", "private_key"}


def test_stable_shared_sources_cannot_be_overwritten_while_other_workflow_runs(monkeypatch):
    api, bundle = Api(), bootstrap.runtime_archive()
    config = {"streaming_mode": "persistent"}
    social = bootstrap.install_job(api, "ws", "social-compute", stream_runtime(config), bundle, ensure_folder=lambda *_: None)
    sensors = bootstrap.install_job(api, "ws", "sensor-compute", stream_runtime(config), bundle, ensure_folder=lambda *_: None, workflow="sensors")
    request = api.request
    def running(method, path, **kwargs):
        if path.endswith("/jobRuns") and (kwargs.get("params") or {}).get("jobKey") == sensors:
            return SimpleNamespace(body=[{"key": "running", "jobKey": sensors, "state": {"status": "RUNNING"}}], headers={})
        return request(method, path, **kwargs)
    monkeypatch.setattr(api, "request", running)
    previous, count = dict(api.contents), len(api.calls)
    with pytest.raises(RuntimeError, match="Stop both managed"):
        bootstrap.install_job(api, "ws", "social-compute", stream_runtime({**config, "bucket": "changed"}), bundle, ensure_folder=lambda *_: None)
    assert api.contents == previous and all(method == "GET" for method, *_ in api.calls[count:])
    assert bootstrap.install_job(api, "ws", "social-compute", stream_runtime(config), bundle, ensure_folder=lambda *_: None) == social


@pytest.mark.parametrize("missing", [False, True])
def test_matching_manifest_does_not_hide_live_source_drift(monkeypatch, missing):
    api, bundle = Api(), bootstrap.runtime_archive()
    config = stream_runtime({"streaming_mode": "persistent"})
    job = bootstrap.install_job(api, "ws", "compute", config, bundle, ensure_folder=lambda *_: None)
    entry = api.resources["/workspaces/ws/jobs/" + job]["tasks"][0]["filePath"]
    endpoint = next(path for path, item in api.contents.items() if item["path"] == entry)
    expected = api.contents[endpoint]["content"]
    request, active = api.request, True
    def running(method, path, **kwargs):
        if path.endswith("/jobRuns"):
            return SimpleNamespace(body=[{"key": "active", "jobKey": job, "state": {"status": "RUNNING"}}] if active else [], headers={})
        return request(method, path, **kwargs)
    monkeypatch.setattr(api, "request", running)
    # A live workflow with identical source is an idempotent reinstall.
    assert bootstrap.install_job(api, "ws", "compute", config, bundle, ensure_folder=lambda *_: None) == job
    if missing:
        del api.contents[endpoint]
    else:
        api.contents[endpoint]["content"] += "\n# external change\n"
    previous, count = dict(api.contents), len(api.calls)
    with pytest.raises(RuntimeError, match="Stop both managed"):
        bootstrap.publish_runtime_sources(api, "ws", config, bundle, ensure_folder=lambda *_: None)
    assert api.contents == previous and all(method == "GET" for method, *_ in api.calls[count:])
    active = False
    bootstrap.publish_runtime_sources(api, "ws", config, bundle, ensure_folder=lambda *_: None)
    assert api.contents[endpoint]["content"] == expected


@pytest.mark.parametrize("legacy_path", ["/Workspace/medallon/prisma/prisma_tick_version.ipynb", "/Workspace/territorial/releases/old/social_network.py"])
def test_first_canonical_sources_do_not_interrupt_legacy_streams(monkeypatch, legacy_path):
    api = Api()
    old = {"key": "legacy", "name": "prisma_bogota_tick", "tasks": [{"filePath": legacy_path}]}
    api.resources["/workspaces/ws/jobs"] = [old]
    api.resources["/workspaces/ws/jobs/legacy"] = old
    request = api.request
    def running(method, path, **kwargs):
        if path.endswith("/jobRuns"):
            pytest.fail("Legacy sources must not be stopped or polled for canonical publication")
        return request(method, path, **kwargs)
    monkeypatch.setattr(api, "request", running)
    root = bootstrap.publish_runtime_sources(api, "ws", stream_runtime({}), bootstrap.runtime_archive(), ensure_folder=lambda *_: None)
    assert root == "/Workspace/medallion/gods_eye_view"
    assert not any(method != "GET" and "/jobs" in path for method, path, *_ in api.calls)


@pytest.fixture(autouse=True)
def offline_deadline(monkeypatch):
    monkeypatch.setattr(bootstrap, "_deadline", 0)
    monkeypatch.setattr(bootstrap.time, "sleep", lambda _: None)


def test_bundle_is_deterministic_compilable_and_contains_classification(tmp_path):
    bundle = bootstrap.runtime_archive()
    assert bundle == bootstrap.runtime_archive()
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        assert "territorial/classification.py" in archive.namelist()
        assert "territorial/scheduling.py" in archive.namelist()
        assert "territorial/pipeline.py" in archive.namelist()
        assert {"territorial/sensors.py", "territorial/sensor_pipeline.py"} <= set(archive.namelist())
        assert all(name.startswith("territorial/") for name in archive.namelist())
        assert all("local" not in name and "api" not in name for name in archive.namelist())
        for name in archive.namelist():
            ast.parse(archive.read(name), filename=name)
    import subprocess
    path = tmp_path / "runtime.zip"
    path.write_bytes(bundle)
    result = subprocess.run([sys.executable, "-I", "-c",
        "import sys; sys.path.insert(0, sys.argv[1]); from territorial.pipeline import run; "
        "from territorial.gold_reader import query; assert callable(run) and callable(query)", str(path)],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_credential_summary_casing_waits_for_active_and_reuses_existing(monkeypatch):
    api, pauses = Api(), []
    request = api.request

    def create_pending(method, path, **kwargs):
        response = request(method, path, **kwargs)
        if method == "POST" and path == "/credentials":
            response.body["lifeCycleState"] = "CREATING"
        return response

    def complete(_seconds=5):
        if not _seconds:
            return
        pauses.append(True)
        assert len(pauses) == 1, "ACTIVE credential must stop polling"
        api.resources["/credentials"][0]["lifeCycleState"] = "ACTIVE"

    monkeypatch.setattr(api, "request", create_pending)
    monkeypatch.setattr(bootstrap, "pause", complete)
    bootstrap.credential(api, "PrismaReaderRuntime", {"db_user": "PRISMA_READER"})
    assert len(pauses) == 1
    bootstrap.credential(api, "PrismaReaderRuntime", {"db_user": "must_not_rotate"})
    writes = [call for call in api.calls if call[0] != "GET"]
    assert len(writes) == 1 and writes[0][:2] == ("POST", "/credentials")
    assert writes[0][2]["credentialDetails"]["secretTokenPair"] == [{"secretKey": "db_user", "secretValue": "PRISMA_READER"}]


def test_credential_summary_casing_filters_deleted_and_rejects_deleting():
    api = Api()
    api.resources["/credentials"] = [{"key": "old", "displayName": "PrismaReaderRuntime", "lifeCycleState": "DELETED"}]
    bootstrap.credential(api, "PrismaReaderRuntime", {"db_user": "PRISMA_READER"})
    assert bootstrap.named(api, "/credentials", "PrismaReaderRuntime")["key"] != "old"
    api.resources["/credentials"][-1]["lifeCycleState"] = "DELETING"
    with pytest.raises(RuntimeError, match="terminal state DELETING"):
        bootstrap.ensure(api, "/credentials", "PrismaReaderRuntime", {}, ready=True)


def test_existing_credential_readiness_waits_without_recreating(monkeypatch):
    api, pauses = Api(), []
    api.resources["/credentials"] = [{"key": "existing", "displayName": "PrismaReaderRuntime", "type": "SECRET_TOKEN", "lifeCycleState": "CREATING"}]

    def complete(_seconds=5):
        if not _seconds:
            return
        pauses.append(True)
        assert len(pauses) == 1
        api.resources["/credentials"][0]["lifeCycleState"] = "ACTIVE"

    monkeypatch.setattr(bootstrap, "pause", complete)
    bootstrap.credential(api, "PrismaReaderRuntime", {"db_user": "must_not_rotate"})
    assert len(pauses) == 1
    assert bootstrap.ensure(api, "/credentials", "PrismaReaderRuntime", None, ready=True)["key"] == "existing"
    assert all(method == "GET" for method, *_ in api.calls)
    api.resources["/credentials"] = []
    with pytest.raises(RuntimeError, match="disappeared"):
        bootstrap.ensure(api, "/credentials", "PrismaReaderRuntime", None, ready=True)
    assert all(method == "GET" for method, *_ in api.calls)


@pytest.mark.parametrize("values", [None, {"db_user": "must_not_rotate"}])
@pytest.mark.parametrize("credential_type", ["SERVICE_ACCOUNT", "VAULT_REFERENCE", None])
def test_runtime_credential_rejects_incompatible_existing_type_without_writes(values, credential_type):
    api = Api()
    api.resources["/credentials"] = [{"key": "existing", "displayName": "PrismaReaderRuntime",
                                    "type": credential_type, "lifeCycleState": "ACTIVE"}]
    with pytest.raises(RuntimeError, match="incompatible type"):
        bootstrap.credential(api, "PrismaReaderRuntime", values)
    assert all(method == "GET" for method, *_ in api.calls)


def test_runtime_credential_reuses_unique_token_and_rejects_duplicates_without_rotation():
    api = Api()
    existing = {"key": "existing", "displayName": "PrismaReaderRuntime", "credentialType": "SECRET_TOKEN", "lifeCycleState": "ACTIVE"}
    api.resources["/credentials"] = [existing]
    assert bootstrap.credential(api, "PrismaReaderRuntime", None) is existing
    api.resources["/credentials"].append({**existing, "key": "duplicate"})
    with pytest.raises(RuntimeError, match="Duplicate managed"):
        bootstrap.credential(api, "PrismaReaderRuntime", {"db_user": "must_not_rotate"})
    assert all(method == "GET" for method, *_ in api.calls)


def agent_runtime():
    return {"region": "us-chicago-1", "model_id": "governance-model", "compartment_id": "compartment",
        "oci_credential_name": "AidpRuntime", "oci_identity_sha256": "a" * 64,
        "catalog": "oci_medallion", "gold_query_compute_id": "gold-query-compute"}


def publish_agent(api, runtime=None):
    runtime, bundle = runtime or agent_runtime(), bootstrap.runtime_archive()
    bootstrap.publish_runtime_sources(api, "ws", stream_runtime(runtime), bundle, ensure_folder=lambda *_: None)
    return bootstrap.publish_agent(api, "ws", bundle, runtime["region"], runtime)


def test_agent_publish_waits_async_and_detail_active_without_deleting_prior_release():
    api = Api()
    api.resources["/workspaces/ws/agents"] = [{"displayName": "prior-agent", "key": "prior", "lifecycleState": "ACTIVE"}]
    result = publish_agent(api)
    assert result["state"] == "ACTIVE" and result["endpoint"].endswith("/chat")
    assert any(path == "/asyncOperations/operation" for _, path, _, _ in api.calls)
    assert not any(method == "DELETE" for method, _, _, _ in api.calls)
    assert api.resources["/workspaces/ws/agents"][0]["key"] == "prior"
    source = next(value["content"] for value in api.contents.values() if value["path"] == "/Workspace/medallion/gods_eye_view/40_report/ai_gods_eye_view.py")
    ast.parse(source)
    assert "sys.path" not in source and "manifest.json" not in source and "gold_query_compute_id" in source
    compute = next(payload for method, path, payload, _ in api.calls if method == "POST" and path.endswith("/clusters"))
    assert compute["displayName"] == "aidp_gods_eye_view_agent_compute"


def test_failed_agent_deployment_never_returns_ready():
    api = Api()
    api.deploy_state = "FAILED"
    with pytest.raises(RuntimeError, match="prior deployment preserved"):
        publish_agent(api)
    assert not any(method == "DELETE" for method, _, _, _ in api.calls)


def test_published_agent_runs_as_one_relocated_native_file(tmp_path):
    import subprocess
    api = Api()
    publish_agent(api)
    entries = [item for item in api.contents.values() if "/40_report/" in item["path"]]
    assert {item["name"] for item in entries} == {"ai_gods_eye_view.py", "requirements.txt"}
    entry = tmp_path / "ai_gods_eye_view.py"
    entry.write_text(next(item["content"] for item in entries if item["name"] == entry.name), encoding="utf-8")
    result = subprocess.run([sys.executable, "-I", "-c",
        "import runpy,sys; namespace=runpy.run_path(sys.argv[1]); "
        "assert namespace['RUNTIME_CONFIG']['oci_credential_name']=='AidpRuntime'; "
        "assert namespace['parse_bbox']('1,2,3,4')==(1,2,3,4)", str(entry)],
        cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_agent_publication_updates_one_agent_and_deployment_on_stable_paths():
    api = Api()
    runtime = {**agent_runtime(),
        "unrelated_runtime_field": "must-not-enter-agent-source"}
    first = publish_agent(api, runtime)
    repeated = publish_agent(api, runtime)
    second = publish_agent(api, {**runtime, "model_id": "updated-model"})
    assert first == repeated and first["revision"] != second["revision"]
    assert first["agent_key"] == second["agent_key"] and first["deployment_key"] == second["deployment_key"]
    entries = [payload["content"] for method, _, payload, _ in api.calls if method == "PUT" and isinstance(payload, dict) and payload.get("path") == "/Workspace/medallion/gods_eye_view/40_report/ai_gods_eye_view.py"]
    assert len(entries) == 2
    configs = [ast.literal_eval(ast.parse(source).body[-1].value) for source in entries]
    assert {value["model_id"] for value in configs} == {"governance-model", "updated-model"}
    assert all(value["oci_credential_name"] == "AidpRuntime" and value["oci_identity_sha256"] == "a" * 64 for value in configs)
    assert all(value["gold_query_compute_id"] == "gold-query-compute" and "reader_credential_name" not in value for value in configs)
    assert all("must-not-enter-agent-source" not in source for source in entries)
    assert not any(method in {"PUT", "DELETE"} and path.startswith("/credentials") for method, path, *_ in api.calls)
    assert len([1 for method, path, *_ in api.calls if method == "POST" and path.endswith("/agents")]) == 1
    assert len([1 for method, path, *_ in api.calls if method == "POST" and path.endswith("/actions/redeploy")]) == 1


def test_agent_requires_gold_configuration_before_publishing():
    api = Api()
    with pytest.raises(RuntimeError, match="Gold agent configuration incomplete"):
        bootstrap.publish_agent(api, "ws", bootstrap.runtime_archive(), "us-chicago-1", {"reader_credential_name": "PrismaReaderRuntime"})
    assert api.calls == []


@pytest.mark.parametrize("explicit", [False, True])
def test_final_agent_name_adopts_previous_canonical_id_without_new_agent(explicit):
    api, runtime = Api(), agent_runtime()
    previous = publish_agent(api, runtime)
    detail = api.resources["/workspaces/ws/agents/" + previous["agent_key"]]
    detail["displayName"] = "territorial_assistant"
    if explicit:
        runtime["agent_id"] = previous["agent_key"]
    updated = publish_agent(api, runtime)
    assert updated["agent_key"] == previous["agent_key"]
    assert updated["deployment_key"] == previous["deployment_key"]
    assert detail["displayName"] == "ai_gods_eye_view"
    assert detail["entryFilePath"] == "/Workspace/medallion/gods_eye_view/40_report/ai_gods_eye_view.py"
    assert len([1 for method, path, *_ in api.calls if method == "POST" and path.endswith("/agents")]) == 1


def test_explicit_agent_id_does_not_fall_back_to_creating_another():
    api = Api()
    api.resources["/workspaces/ws/agents/foreign"] = {"key": "foreign", "displayName": "foreign", "state": "ACTIVE"}
    with pytest.raises(RuntimeError, match="agent identity differs"):
        publish_agent(api, {**agent_runtime(), "agent_id": "foreign"})
    assert not any(method == "POST" and path.endswith("/agents") for method, path, *_ in api.calls)


def test_hidden_compute_is_reused_by_scoped_async_resource_key():
    api = Api()
    api.resources["/asyncOperations"] = [
        {"resourceDisplayName": "prisma_agent_compute", "resourceName": "other.ignored", "actionType": "CREATE_CLUSTER"},
        {"resourceDisplayName": "prisma_agent_compute", "resourceName": "ws.hidden", "actionType": "CREATE_CLUSTER", "status": "SUCCEEDED"},
    ]
    api.resources["/workspaces/ws/clusters/hidden"] = {"displayName": "prisma_agent_compute", "key": "hidden", "type": "AI_COMPUTE", "state": "ACTIVE"}
    result = bootstrap.ensure(api, "/workspaces/ws/clusters", "prisma_agent_compute", {}, ready=True)
    assert result["key"] == "hidden"
    assert not any(method == "POST" for method, _, _, _ in api.calls)


@pytest.mark.parametrize("name", ["territorial_agent_compute", "prisma_agent_compute"])
def test_explicit_hidden_agent_compute_keeps_identity_without_creating_another(name):
    api = Api()
    api.resources["/workspaces/ws/clusters/hidden"] = {"displayName": name, "key": "hidden", "type": "AI_COMPUTE", "state": "ACTIVE"}
    published = publish_agent(api, {**agent_runtime(), "agent_compute_id": "hidden"})
    detail = api.resources["/workspaces/ws/agents/" + published["agent_key"]]
    assert detail["computeKey"] == "hidden"
    assert all(method == "GET" for method, path, *_ in api.calls if "/clusters" in path)
    assert not any(path.endswith("/clusters") or path == "/asyncOperations" for _, path, *_ in api.calls)


def test_renamed_hidden_compute_is_discovered_from_legacy_create_without_duplicates():
    api = Api()
    api.resources["/asyncOperations"] = [{"resourceDisplayName": "prisma_agent_compute", "resourceName": "ws.hidden",
        "actionType": "CREATE_CLUSTER", "status": "SUCCEEDED"}]
    api.resources["/workspaces/ws/clusters/hidden"] = {"displayName": "territorial_agent_compute", "key": "hidden", "type": "AI_COMPUTE", "state": "ACTIVE"}
    assert bootstrap.install_agent_compute(api, "ws")["key"] == "hidden"
    assert all(method == "GET" for method, *_ in api.calls)


def test_both_agent_compute_names_are_ambiguous_without_an_explicit_id():
    api = Api()
    api.resources["/workspaces/ws/clusters"] = [{"displayName": name, "key": name, "type": "AI_COMPUTE", "state": "ACTIVE"}
        for name in ("territorial_agent_compute", "prisma_agent_compute")]
    with pytest.raises(RuntimeError, match="Duplicate"):
        bootstrap.install_agent_compute(api, "ws")
    assert all(method == "GET" for method, *_ in api.calls)


@pytest.mark.parametrize("key", [None, 0, False, " "])
def test_invalid_explicit_agent_compute_id_cannot_create_a_compute(key):
    api = Api()
    with pytest.raises(RuntimeError, match="identity is invalid"):
        bootstrap.install_agent_compute(api, "ws", key)
    assert api.calls == []


@pytest.mark.parametrize("change", [{"displayName": "foreign"}, {"type": "USER"}, {"key": "other"}, {"state": "FAILED"}])
def test_explicit_compute_never_falls_back_after_identity_or_readiness_failure(change):
    api = Api()
    api.resources["/workspaces/ws/clusters/hidden"] = {"displayName": "territorial_agent_compute", "key": "hidden", "type": "AI_COMPUTE", "state": "ACTIVE", **change}
    with pytest.raises(RuntimeError, match="compute"):
        bootstrap.install_agent_compute(api, "ws", "hidden")
    assert [call[:2] for call in api.calls] == [("GET", "/workspaces/ws/clusters/hidden")]


@pytest.mark.parametrize("names", [["AidpControlStore"], ["TerritorialWriterRuntime"], ["PrismaWriterRuntime"], []])
def test_control_store_adopts_one_name_without_secret_reads_or_rotation(names):
    api = Api()
    api.resources["/credentials"] = [{"displayName": name, "key": name, "type": "SECRET_TOKEN", "lifeCycleState": "ACTIVE"} for name in names]
    assert bootstrap.database_credential_name(api) == (names[0] if names else "AidpControlStore")
    assert all(method == "GET" and path == "/credentials" for method, path, *_ in api.calls)


def test_multiple_control_store_aliases_are_rejected_without_rotation():
    api = Api()
    api.resources["/credentials"] = [{"displayName": name, "key": name, "type": "SECRET_TOKEN", "lifeCycleState": "ACTIVE"}
        for name in ("AidpControlStore", "PrismaWriterRuntime")]
    with pytest.raises(RuntimeError, match="Ambiguous"):
        bootstrap.database_credential_name(api)
    assert all(method == "GET" for method, *_ in api.calls)


@pytest.mark.parametrize("status,deleted", [("SUCCEEDED", True), ("IN_PROGRESS", False)])
def test_hidden_compute_latest_delete_is_never_reused(status, deleted):
    api = Api()
    api.resources["/asyncOperations"] = [
        {"resourceDisplayName": "managed", "resourceName": "ws.hidden", "actionType": "CREATE_CLUSTER", "timeStarted": "2026-10-01"},
        {"resourceDisplayName": "managed", "resourceName": "ws.hidden", "actionType": "DELETE_CLUSTER", "timeStarted": "2026-10-02", "status": status},
    ]
    if deleted:
        assert bootstrap.named(api, "/workspaces/ws/clusters", "managed") is None
    else:
        with pytest.raises(RuntimeError, match="being deleted"):
            bootstrap.named(api, "/workspaces/ws/clusters", "managed")


def test_deployment_helpers_reject_duplicates_and_mismatched_endpoint_retention():
    api = Api()
    api.resources["/deployments"] = [{"key": "deleted", "state": "DELETED"}, {"key": "live", "lifecycleState": "ACTIVE"}]
    assert bootstrap.agent_deployments(api, "/deployments") == [{"key": "live", "lifecycleState": "ACTIVE"}]
    api.resources["/deployments"].append({"key": "duplicate", "state": "ACTIVE"})
    with pytest.raises(RuntimeError, match="Duplicate"):
        bootstrap.agent_deployments(api, "/deployments")
    detail = {"endpointUrl": "https://gateway.aidp.us-chicago-1.oci.oraclecloud.com/agentendpoint/managed/", "sessionRetentionConfig": {"retentionPeriodInDays": 7, "unused": None}}
    assert bootstrap.deployment_endpoint(detail, "us-chicago-1").endswith("/managed/chat")
    with pytest.raises(RuntimeError, match="endpoint"):
        bootstrap.deployment_endpoint(detail, "eu-frankfurt-1")
    with pytest.raises(RuntimeError, match="retention"):
        bootstrap.deployment_endpoint({**detail, "sessionRetentionConfig": {"retentionPeriodInDays": 1}}, "us-chicago-1")


@pytest.mark.parametrize("failed_state", ["FAILED", "INTERNAL_ERROR", "UPSTREAM_FAILED", "UPSTREAM_CANCELED", "EXCLUDED", "BLOCKED"])
@pytest.mark.parametrize("failed_resource", ["job", "task"])
def test_initial_job_requires_native_run_and_task_success_and_revision_token(monkeypatch, failed_state, failed_resource):
    api = Api()
    api.resources["/workspaces/ws/jobs/job"] = {"tasks": [{"type": "NOTEBOOK_TASK",
        "notebookPath": "/Workspace/medallon/prisma/prisma_tick_aaaaaaaaaaaa.ipynb"}]}
    assert bootstrap.run_initial_job(api, "ws", "job", "revision") == "run-one"
    method, path, payload, headers = api.calls[1]
    assert (method, path, payload) == ("POST", "/workspaces/ws/jobRuns", {"jobKey": "job", "parameters": []})
    assert len(headers["opc-retry-token"]) == 64
    assert api.calls[-1][1] == "/workspaces/ws/taskRuns"
    request = api.request

    def failed_response(method, path, **kwargs):
        response = request(method, path, **kwargs)
        if failed_resource == "job" and path.endswith("/jobRuns/run-one"):
            response.body["state"]["status"] = failed_state
        elif failed_resource == "task" and path.endswith("/taskRuns"):
            response.body["items"][0]["state"]["status"] = failed_state
        return response

    def no_terminal_wait(seconds=5):
        assert seconds == 0, "Terminal state must fail without polling"

    monkeypatch.setattr(api, "request", failed_response)
    monkeypatch.setattr(bootstrap, "pause", no_terminal_wait)
    with pytest.raises(RuntimeError, match=f"initial {failed_resource} failed; no readiness claimed"):
        bootstrap.run_initial_job(api, "ws", "job", "next-revision")


def test_job_publishes_readable_python_files_and_preserves_live_schedule():
    api = Api()
    bundle = bootstrap.runtime_archive()
    job = bootstrap.install_job(api, "ws", "compute", stream_runtime({"bucket": "gold"}), bundle, ensure_folder=lambda *_: None)
    detail = api.resources["/workspaces/ws/jobs/" + job]
    detail["schedule"] = {**detail["schedule"], "pauseStatus": "UNPAUSED"}
    assert bootstrap.install_job(api, "ws", "compute", stream_runtime({"bucket": "gold"}), bundle, ensure_folder=lambda *_: None) == job
    assert detail["schedule"]["pauseStatus"] == "UNPAUSED"
    assert not any(method == "PATCH" and "/notebook/api/contents/" in path for method, path, _, _ in api.calls)
    assert not any("/actions/export/contents/" in path for _, path, _, _ in api.calls)
    task = detail["tasks"][0]
    assert detail["name"] == "wf_ai_gods_eye_view_social_network"
    assert task["type"] == "PYTHON_TASK" and task["source"] == "WORKSPACE"
    assert task["taskKey"] == "social_network" and task["filePath"].endswith("/social_network.py")
    root = bootstrap.WORKSPACE_ROOT
    assert task["filePath"] == root + "/10_bronze/social_network.py"
    files = {value["path"].removeprefix(root + "/"): value["content"] for value in api.contents.values()}
    assert all(value["format"] == "text" and value["type"] == "file" for value in api.contents.values())
    assert set(files) == {"10_bronze/social_network.py", "10_bronze/sensor_stream.py", "manifest.json",
                          "README.md", "20_silver/README.md", "30_gold/README.md"}
    for path in ("10_bronze/social_network.py", "10_bronze/sensor_stream.py"):
        source = files[path]
        assert "--runtime-root" not in source and "manifest.json" not in source and "sys.path" not in source
        assert not any(isinstance(node, ast.ImportFrom) and (node.level or (node.module or "").startswith("territorial"))
                       for node in ast.walk(ast.parse(source)))
    manifest = json.loads(files["manifest.json"])
    assert manifest["bundle_sha256"] == hashlib.sha256(bundle).hexdigest()
    assert ast.literal_eval(task["commandLineArguments"]) == [task["filePath"]]
    assert manifest["files"] == {name: hashlib.sha256(value.encode()).hexdigest() for name, value in files.items() if name != "manifest.json"}
    assert len([1 for method, path, _, _ in api.calls if method == "POST" and path.endswith("/jobs")]) == 1
    assert len([1 for method, path, _, _ in api.calls if method == "PUT" and "/jobs/" in path]) == 1


@pytest.mark.parametrize("workflow,legacy,canonical", [
    ("social", "prisma_bogota_tick", "wf_ai_gods_eye_view_social_network"),
    ("sensors", "prisma_colombia_sensors", "wf_ai_gods_eye_view_sensor_stream"),
    ("social", "territorial_social_network", "wf_ai_gods_eye_view_social_network"),
    ("sensors", "territorial_sensor_stream", "wf_ai_gods_eye_view_sensor_stream"),
])
@pytest.mark.parametrize("explicit_key", [False, True])
def test_python_workflow_adopts_the_existing_job_key_without_creating_another(workflow, legacy, canonical, explicit_key):
    api = Api()
    old = {"key": "existing-job", "name": legacy, "path": "/Workspace/medallon/prisma",
           "schedule": {"pauseStatus": "PAUSED"}, "tasks": [{"type": "NOTEBOOK_TASK"}]}
    api.resources["/workspaces/ws/jobs"] = [old]
    api.resources["/workspaces/ws/jobs/existing-job"] = old
    config = {"streaming_mode": "persistent"}
    if explicit_key:
        config["sensor_job_key" if workflow == "sensors" else "job_key"] = "existing-job"
    result = bootstrap.install_job(api, "ws", "same-compute", stream_runtime(config), bootstrap.runtime_archive(),
                                   ensure_folder=lambda *_: None, workflow=workflow)
    assert result == "existing-job" and old["name"] == canonical
    assert old["tasks"][0]["type"] == "PYTHON_TASK"
    assert not any(method == "POST" and path.endswith("/jobs") for method, path, *_ in api.calls)
    assert bootstrap.install_job(api, "ws", "same-compute", stream_runtime(config), bootstrap.runtime_archive(),
                                 ensure_folder=lambda *_: None, workflow=workflow) == result


@pytest.mark.parametrize("failure", ["duplicate", "wrong_key", "wrong_name", "deleting"])
def test_python_workflow_identity_ambiguity_fails_before_any_write(failure):
    api = Api()
    old = {"key": "existing", "name": "prisma_bogota_tick"}
    api.resources["/workspaces/ws/jobs"] = [old]
    api.resources["/workspaces/ws/jobs/existing"] = old
    config = {}
    if failure == "duplicate":
        api.resources["/workspaces/ws/jobs"].append({"key": "other", "name": "wf_ai_gods_eye_view_social_network"})
    elif failure == "wrong_key":
        config["job_key"] = "other"
    elif failure == "wrong_name":
        config["job_key"] = "existing"
        old["name"] = "unrelated_workflow"
    else:
        old["lifecycleState"] = "DELETING"
    with pytest.raises(RuntimeError, match="[Ww]orkflow"):
        bootstrap.install_job(api, "ws", "compute", stream_runtime(config), bootstrap.runtime_archive(), ensure_folder=lambda *_: None)
    assert all(method == "GET" for method, *_ in api.calls)


@pytest.mark.parametrize("field,value", [
    ("source", "GIT_PROVIDER"), ("taskKey", "sensor_stream"),
    ("filePath", "/Workspace/medallion/gods_eye_view/releases/invalid/social_network.py"),
    ("commandLineArguments", "--runtime-root /Workspace/another-release"),
    ("commandLineArguments", "--runtime-root /Workspace/medallion/gods_eye_view/releases/" + "a" * 64 + " --check-runtime"),
])
def test_initial_python_acceptance_rejects_unmanaged_or_different_execution(field, value):
    api = Api()
    root = "/Workspace/medallion/gods_eye_view"
    task = {"type": "PYTHON_TASK", "taskKey": "social_network", "source": "WORKSPACE",
            "filePath": root + "/10_bronze/social_network.py", "commandLineArguments": json.dumps([root + "/10_bronze/social_network.py"])}
    api.resources["/workspaces/ws/jobs/job"] = {"tasks": [{**task, field: value}]}
    with pytest.raises(RuntimeError, match="one managed source"):
        bootstrap.run_initial_job(api, "ws", "job", "revision")
    assert all(method == "GET" for method, *_ in api.calls)


def test_persistent_task_is_explicit_and_reverts_to_finite_without_changing_job_identity():
    api, bundle = Api(), bootstrap.runtime_archive()
    job = bootstrap.install_job(api, "ws", "compute", stream_runtime({}), bundle, ensure_folder=lambda *_: None)
    detail = api.resources["/workspaces/ws/jobs/" + job]
    assert detail["tasks"][0]["isStreaming"] is False and detail["timeoutSeconds"] == 600
    same = bootstrap.install_job(api, "ws", "compute", stream_runtime({"streaming_mode": "persistent"}), bundle, ensure_folder=lambda *_: None)
    assert same == job and detail["maxConcurrentRuns"] == 1 and detail["queue"] == {"isEnabled": False}
    assert detail["tasks"][0]["isStreaming"] is True and detail["timeoutSeconds"] == 0
    assert "maxRetries" not in detail["tasks"][0]
    assert detail["schedule"]["pauseStatus"] == "PAUSED"
    with pytest.raises(RuntimeError, match="finite job"):
        bootstrap.run_initial_job(api, "ws", job, "persistent-bundle")
    assert not any(call[0] == "POST" and call[1].endswith("/jobRuns") for call in api.calls)
    bootstrap.install_job(api, "ws", "compute", stream_runtime({}), bundle, ensure_folder=lambda *_: None)
    assert detail["tasks"][0]["isStreaming"] is False and detail["timeoutSeconds"] == 600
    assert len([call for call in api.calls if call[0] == "POST" and call[1].endswith("/jobs")]) == 1


@pytest.mark.parametrize("server_default", [None, 0, 600])
def test_persistent_job_omits_timeout_and_requires_unlimited_roundtrip(server_default):
    api, bundle = Api(), bootstrap.runtime_archive()
    job = bootstrap.install_job(api, "ws", "compute", stream_runtime({}), bundle, ensure_folder=lambda *_: None)
    path = "/workspaces/ws/jobs/" + job
    api.job_timeout_default = server_default
    config = {"streaming_mode": "persistent"}
    if server_default == 600:
        with pytest.raises(RuntimeError, match="did not round-trip"):
            bootstrap.install_job(api, "ws", "compute", stream_runtime(config), bundle, ensure_folder=lambda *_: None)
    else:
        assert bootstrap.install_job(api, "ws", "compute", stream_runtime(config), bundle, ensure_folder=lambda *_: None) == job
        assert api.resources[path].get("timeoutSeconds") == server_default
        api.resources[path]["timeoutSeconds"] = 600
        assert bootstrap.install_job(api, "ws", "compute", stream_runtime(config), bundle, ensure_folder=lambda *_: None) == job
        assert api.resources[path].get("timeoutSeconds") == server_default
    updates = [call[2] for call in api.calls if call[:2] == ("PUT", path)]
    assert len(updates) == (2 if server_default == 600 else 3)
    assert updates[0]["timeoutSeconds"] == 600
    assert all("timeoutSeconds" not in payload for payload in updates[1:])


@pytest.mark.parametrize("second_task,state", [("social_network", "SUCCESS"), ("social_network", "FAILED"), ("other_task", "SUCCESS")])
def test_initial_job_accepts_successful_native_task_attempts_but_never_hides_failure(monkeypatch, second_task, state):
    api = Api()
    job = bootstrap.install_job(api, "ws", "compute", stream_runtime({}), bootstrap.runtime_archive(), ensure_folder=lambda *_: None)
    original = api.request

    def request(method, path, **kwargs):
        response = original(method, path, **kwargs)
        if path.endswith("/taskRuns"):
            response.body["items"].append({"key": "second-native-attempt", "taskKey": second_task, "state": {"status": state}})
        return response

    def no_wait(seconds=5):
        assert seconds == 0, "Completed native task attempts must not wait indefinitely"

    monkeypatch.setattr(api, "request", request)
    monkeypatch.setattr(bootstrap, "pause", no_wait)
    if second_task == "social_network" and state == "SUCCESS":
        assert bootstrap.run_initial_job(api, "ws", job, "same-notebook") == "run-one"
    else:
        with pytest.raises(RuntimeError, match="initial task failed; no readiness claimed"):
            bootstrap.run_initial_job(api, "ws", job, "same-notebook")


@pytest.mark.parametrize("etag", [None, "native-version"])
def test_existing_job_reconciles_without_required_etag_and_defaults_null_schedule(monkeypatch, etag):
    api, bundle = Api(), bootstrap.runtime_archive()
    job = bootstrap.install_job(api, "ws", "compute", stream_runtime({}), bundle, ensure_folder=lambda *_: None)
    path = "/workspaces/ws/jobs/" + job
    api.resources[path].update(schedule=None, timeoutSeconds=42)
    request = api.request

    def with_etag(method, resource, **kwargs):
        response = request(method, resource, **kwargs)
        if method == "GET" and resource == path and etag:
            response.headers["etag"] = etag
        return response

    monkeypatch.setattr(api, "request", with_etag)
    assert bootstrap.install_job(api, "ws", "compute", stream_runtime({}), bundle, ensure_folder=lambda *_: None) == job
    updates = [call for call in api.calls if call[:2] == ("PUT", path)]
    assert len(updates) == 2
    assert updates[-1][3] == ({"If-Match": etag} if etag else None)
    assert api.resources[path]["schedule"]["pauseStatus"] == "PAUSED"
    assert api.resources[path]["timeoutSeconds"] == 600


def test_python_publication_updates_embedded_config_and_execution_identity():
    api, bundle = Api(), bootstrap.runtime_archive()
    job = bootstrap.install_job(api, "ws", "compute", stream_runtime({"bucket": "gold"}), bundle, ensure_folder=lambda *_: None)
    task = api.resources["/workspaces/ws/jobs/" + job]["tasks"][0]
    paths = set(api.contents)
    bootstrap.run_initial_job(api, "ws", job, "same-bundle")
    first_token = next(call[3]["opc-retry-token"] for call in reversed(api.calls) if call[:2] == ("POST", "/workspaces/ws/jobRuns"))
    bootstrap.run_initial_job(api, "ws", job, "same-bundle")
    assert next(call[3]["opc-retry-token"] for call in reversed(api.calls) if call[:2] == ("POST", "/workspaces/ws/jobRuns")) == first_token
    assert bootstrap.install_job(api, "ws", "compute", stream_runtime({"bucket": "updated"}), bundle, ensure_folder=lambda *_: None) == job
    assert set(api.contents) == paths
    source = next(item["content"] for item in api.contents.values() if item["path"] == task["filePath"])
    config = next(ast.literal_eval(node.value) for node in ast.parse(source).body
                  if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "RUNTIME_CONFIG" for target in node.targets))
    assert config["bucket"] == "updated" and "writer_credential_name" not in config
    bootstrap.run_initial_job(api, "ws", job, "same-bundle")
    assert next(call[3]["opc-retry-token"] for call in reversed(api.calls) if call[:2] == ("POST", "/workspaces/ws/jobRuns")) != first_token


@pytest.mark.parametrize("initial_status", [None, "INSTALL_ON_RESTART", "INSTALLED"])
def test_cluster_libraries_install_once_then_reuse_without_restart(monkeypatch, initial_status):
    api, state = Api(), {"status": initial_status, "path": None}
    original = api.request
    base = "/workspaces/ws/clusters/compute"

    def request(method, path, **kwargs):
        if path == base + "/libraries":
            api.calls.append((method, path, kwargs.get("payload"), kwargs.get("headers")))
            state["path"] = next(value["path"] for value in api.contents.values() if value["name"] == "requirements.txt")
            if method == "PATCH":
                assert kwargs["payload"] == {"items": [{"operation": "INSTALL", "type": "WORKSPACE_FILE", "path": state["path"]}]}
                state["status"] = "INSTALL_ON_RESTART"
            return SimpleNamespace(body={"items": [{"path": state["path"], "status": state["status"]}] if state["status"] else []}, headers={})
        if path == base + "/actions/restart":
            assert kwargs["payload"] == {}
            state["status"] = "INSTALLED"
            api.calls.append((method, path, kwargs["payload"], None))
            return SimpleNamespace(body={}, headers={})
        return original(method, path, **kwargs)

    api.resources[base] = {"state": "ACTIVE", "attachedSessions": [], "attachedNotebooks": []}
    api.resources["/workspaces/ws/jobs"] = [{"key": "social-job"}]
    api.resources["/workspaces/ws/jobs/social-job"] = {"tasks": [{"cluster": {"clusterKey": "social-compute"}}]}
    api.resources["/workspaces/ws/jobRuns"] = [{"key": "social-run", "jobKey": "social-job", "state": {"status": "RUNNING"}}]
    monkeypatch.setattr(api, "request", request)
    installed = bootstrap.install_cluster_libraries(api, "ws", "compute", ensure_folder=lambda *_: None)
    assert installed.endswith("/requirements.txt")
    assert bootstrap.install_cluster_libraries(api, "ws", "compute", ensure_folder=lambda *_: None) == installed
    assert len([call for call in api.calls if call[:2] == ("PATCH", base + "/libraries")]) == int(initial_status is None)
    assert len([call for call in api.calls if call[:2] == ("POST", base + "/actions/restart")]) == int(initial_status != "INSTALLED")
    assert next(iter(api.contents.values()))["content"] == "httpx==0.28.1\n"


@pytest.mark.parametrize("drift", [None, "location", "type", "schema"])
def test_native_volumes_are_scoped_idempotent_and_reject_drift(monkeypatch, drift):
    api = Api()
    api.resources["/catalogs"] = [{"key": "catalog-key", "displayName": "catalog"}]
    config = {"catalog": "catalog", "namespace": "ns", "landing_bucket": "landing", "landing_prefix": "01_landing/prisma/raw/"}
    original = api.request

    def request(method, path, **kwargs):
        if method == "GET" and path == "/schemas":
            assert kwargs["params"] == {"catalogKey": "catalog-key"}
        if method == "GET" and path == "/volumes":
            assert kwargs["params"] == {"catalogKey": "catalog-key", "schemaKey": "key-prisma_ingest"}
        return original(method, path, **kwargs)

    monkeypatch.setattr(api, "request", request)
    bootstrap.install_volumes(api, config)
    bootstrap.install_volumes(api, config)
    writes = [call for call in api.calls if call[0] != "GET"]
    assert len(writes) == 3
    assert writes[1][2] == {"displayName": "landing", "catalogName": "catalog", "schemaName": "prisma_ingest",
        "volumeType": "EXTERNAL", "storageLocation": "oci://landing@ns/01_landing/prisma/raw/"}
    assert writes[2][2] == {"displayName": "checkpoints", "catalogName": "catalog", "schemaName": "prisma_ingest", "volumeType": "MANAGED"}
    if drift:
        target, field, value = {"location": ("/volumes/key-landing", "storageLocation", "oci://elsewhere@ns/"),
            "type": ("/volumes/key-checkpoints", "volumeType", "EXTERNAL"),
            "schema": ("/schemas/key-prisma_ingest", "catalogName", "foreign")}[drift]
        api.resources[target][field] = value
        with pytest.raises(RuntimeError, match="differs|does not match"):
            bootstrap.install_volumes(api, config)
        assert [call for call in api.calls if call[0] != "GET"] == writes


@pytest.mark.parametrize("busy", ["session", "schedule", "continuous", "run"])
def test_cluster_libraries_refuse_to_restart_busy_shared_compute(busy):
    api = Api()
    api.resources["/workspaces/ws/clusters/compute"] = {"state": "ACTIVE", "attachedSessions": ["session"] if busy == "session" else []}
    api.resources["/workspaces/ws/jobs"] = [{"key": "existing"}]
    api.resources["/workspaces/ws/jobs/existing"] = {"tasks": [{"cluster": {"clusterKey": "compute"}}],
        "schedule": None if busy == "continuous" else {"pauseStatus": "UNPAUSED" if busy == "schedule" else "PAUSED"},
        "continuous": {"pauseStatus": "UNPAUSED" if busy == "continuous" else "PAUSED"}}
    api.resources["/workspaces/ws/jobRuns"] = [{"state": {"status": "RUNNING"}}] if busy == "run" else []
    with pytest.raises(RuntimeError, match="session|Pause jobs|active"):
        bootstrap.install_cluster_libraries(api, "ws", "compute", ensure_folder=lambda *_: None)
    assert not any(path.endswith("/libraries") and method == "PATCH" or path.endswith("/actions/restart") for method, path, *_ in api.calls)


def test_cluster_idle_does_not_block_on_other_compute_continuous_job():
    api = Api()
    api.resources["/workspaces/ws/clusters/compute"] = {"state": "ACTIVE"}
    api.resources["/workspaces/ws/jobs"] = [{"key": "other"}]
    api.resources["/workspaces/ws/jobs/other"] = {"jobClusters": [{"clusterKey": "other-compute"}],
        "tasks": [{"cluster": {"clusterKey": "other-compute"}}], "schedule": None, "continuous": {"pauseStatus": "UNPAUSED"}}
    api.resources["/workspaces/ws/jobRuns"] = [{"key": "running-social", "jobKey": "other", "state": {"status": "RUNNING"}}]
    bootstrap.cluster_idle(api, "ws", "compute")
    assert all(method == "GET" for method, *_ in api.calls)


@pytest.mark.parametrize("job_key", [None, "missing-job", "sensor-job"])
def test_cluster_maintenance_blocks_unknown_or_own_active_run(job_key):
    api = Api()
    api.resources["/workspaces/ws/clusters/sensor-compute"] = {"state": "ACTIVE"}
    api.resources["/workspaces/ws/jobs"] = [{"key": "sensor-job"}]
    api.resources["/workspaces/ws/jobs/sensor-job"] = {"tasks": [{"cluster": {"clusterKey": "sensor-compute"}}]}
    api.resources["/workspaces/ws/jobRuns"] = [{"key": "run", "jobKey": job_key, "state": {"status": "RUNNING"}}]
    with pytest.raises(RuntimeError, match="active run"):
        bootstrap.cluster_idle(api, "ws", "sensor-compute")
    assert all(method == "GET" for method, *_ in api.calls)


def test_failed_cluster_library_does_not_restart_or_retry_install():
    api = Api()
    digest = bootstrap.hashlib.sha256(bootstrap.PIPELINE_REQUIREMENTS.encode()).hexdigest()[:12]
    api.resources["/workspaces/ws/clusters/compute/libraries"] = [{"path": f"/Workspace/medallon/prisma/dependencies_{digest}/requirements.txt", "status": "FAILED"}]
    with pytest.raises(RuntimeError, match="not usable: FAILED"):
        bootstrap.install_cluster_libraries(api, "ws", "compute", ensure_folder=lambda *_: None)
    assert not any(method == "PATCH" or path.endswith("/actions/restart") for method, path, *_ in api.calls)


def test_cluster_library_waits_for_restart_and_installed_readiness(monkeypatch):
    api, waits = Api(), []
    digest = bootstrap.hashlib.sha256(bootstrap.PIPELINE_REQUIREMENTS.encode()).hexdigest()[:12]
    base = "/workspaces/ws/clusters/compute"
    library = {"path": f"/Workspace/medallon/prisma/dependencies_{digest}/requirements.txt", "status": "INSTALLING"}
    api.resources[base] = {"state": "ACTIVE"}
    api.resources[base + "/libraries"] = [library]
    original = api.request

    def request(method, path, **kwargs):
        if path.endswith("/actions/restart"):
            api.calls.append((method, path, kwargs["payload"], None))
            api.resources[base]["state"] = "RESTARTING"
            return SimpleNamespace(body={}, headers={})
        return original(method, path, **kwargs)

    def advance(seconds=5):
        if seconds:
            waits.append(seconds)
            assert len(waits) <= 2
            library["status"] = "INSTALL_ON_RESTART" if len(waits) == 1 else "INSTALLED"
            api.resources[base]["state"] = "ACTIVE"

    monkeypatch.setattr(api, "request", request)
    monkeypatch.setattr(bootstrap, "pause", advance)
    bootstrap.install_cluster_libraries(api, "ws", "compute", ensure_folder=lambda *_: None)
    assert len(waits) == 2
    assert not any(method == "PATCH" for method, *_ in api.calls)


def test_publication_requires_matching_version_and_safe_snapshot_key():
    documents = {
        "04_gold/prisma/current.json": {"version": "gold-one", "snapshot_key": "04_gold/prisma/snapshots/gold-one.json"},
        "04_gold/prisma/snapshots/gold-one.json": {"version": "gold-one", "incidents": [], "evidence": []},
    }
    storage = SimpleNamespace(get_object=lambda _namespace, _bucket, key: SimpleNamespace(
        data=SimpleNamespace(content=json.dumps(documents[key]).encode())))
    assert bootstrap.validate_publication(storage, {"namespace": "ns", "bucket": "gold"}) == "gold-one"
    documents["04_gold/prisma/snapshots/gold-one.json"]["version"] = "stale"
    with pytest.raises(RuntimeError, match="incomplete"):
        bootstrap.validate_publication(storage, {"namespace": "ns", "bucket": "gold"})
    documents["04_gold/prisma/current.json"]["snapshot_key"] = "04_gold/prisma/snapshots/../secret"
    with pytest.raises(RuntimeError, match="pointer is invalid"):
        bootstrap.validate_publication(storage, {"namespace": "ns", "bucket": "gold"})


def test_shared_deadline_stops_waits(monkeypatch):
    monkeypatch.setattr(bootstrap.time, "monotonic", lambda: 100)
    monkeypatch.setattr(bootstrap, "_deadline", 101)
    with pytest.raises(RuntimeError, match="shared post-apply deadline"):
        bootstrap.pause(5)


def test_two_dedicated_always_on_clusters_reuse_exact_config_and_reject_drift():
    api = Api()
    keys = [bootstrap.install_stream_compute(api, "ws", name) for name in ("social_stream_compute", "sensor_stream_compute")]
    assert len(set(keys)) == 2
    created = [payload for method, path, payload, _ in api.calls if method == "POST" and path.endswith("/clusters")]
    assert len(created) == 2
    assert all(item["type"] == "USER" and item["workerConfig"]["minWorkerCount"] == item["workerConfig"]["maxWorkerCount"] == 1 for item in created)
    assert all("autoTerminationMinutes" not in item for item in created)
    assert bootstrap.install_stream_compute(api, "ws", "social_stream_compute") == keys[0]
    assert len([call for call in api.calls if call[0] == "POST" and call[1].endswith("/clusters")]) == 2
    detail = api.resources["/workspaces/ws/clusters/" + keys[0]]
    detail["autoTerminationMinutes"] = 10
    with pytest.raises(RuntimeError, match="always-on"):
        bootstrap.install_stream_compute(api, "ws", "social_stream_compute")
    detail["autoTerminationMinutes"] = 0
    detail["workerConfig"]["maxWorkerCount"] = 10
    with pytest.raises(RuntimeError, match="fixed-size"):
        bootstrap.install_stream_compute(api, "ws", "social_stream_compute")
    assert not any(method in {"PUT", "PATCH", "DELETE"} for method, *_ in api.calls)


@pytest.mark.parametrize("canonical,legacy", [
    ("aidp_gods_eye_view_social_compute", "social_stream_compute"),
    ("aidp_gods_eye_view_sensor_compute", "sensor_stream_compute"),
    ("aidp_gods_eye_view_query_compute", "territorial_query_compute"),
])
@pytest.mark.parametrize("explicit", [False, True])
def test_final_compute_names_keep_prior_ids_without_creating_duplicates(canonical, legacy, explicit):
    api = Api()
    key = bootstrap.install_stream_compute(api, "ws", canonical)
    api.resources["/workspaces/ws/clusters/" + key]["displayName"] = legacy
    if explicit:
        api.resources["/workspaces/ws/clusters"] = []
    assert bootstrap.install_stream_compute(api, "ws", canonical, key if explicit else "") == key
    assert len([1 for method, path, *_ in api.calls if method == "POST" and path.endswith("/clusters")]) == 1
    assert not any(method in {"PUT", "PATCH", "DELETE"} for method, *_ in api.calls)


@pytest.mark.parametrize("outcome", ["ACTIVE", "FAILED"])
def test_stopped_dedicated_stream_compute_starts_once_and_waits_for_active(monkeypatch, outcome):
    api = Api()
    key = bootstrap.install_stream_compute(api, "ws", "sensor_stream_compute")
    path = "/workspaces/ws/clusters/" + key
    detail = api.resources[path]
    detail["state"] = "STOPPED"
    original, states = api.request, iter(["STARTING", outcome] * 2)
    started, pending = [], [False]
    def request(method, target, **kwargs):
        if (method, target) == ("POST", path + "/actions/start"):
            assert kwargs["payload"] == {} and kwargs["headers"]["If-Match"] == "compute-etag"
            assert len(kwargs["headers"]["opc-retry-token"]) == 32
            started.append(kwargs["headers"]["opc-retry-token"])
            pending[0] = True
            return SimpleNamespace(body={}, headers={})
        response = original(method, target, **kwargs)
        if (method, target) == ("GET", path):
            response.headers["etag"] = "compute-etag"
            if pending[0]:
                detail["state"] = next(states)
                pending[0] = detail["state"] == "STARTING"
        return response
    monkeypatch.setattr(api, "request", request)
    if outcome == "ACTIVE":
        assert bootstrap.install_stream_compute(api, "ws", "sensor_stream_compute") == key
        assert detail["state"] == "ACTIVE"
        detail["state"] = "STOPPED"
        assert bootstrap.install_stream_compute(api, "ws", "sensor_stream_compute") == key
        assert detail["state"] == "ACTIVE"
    else:
        with pytest.raises(RuntimeError, match="failed to become active: FAILED"):
            bootstrap.install_stream_compute(api, "ws", "sensor_stream_compute")
    assert len(started) == len(set(started)) == (2 if outcome == "ACTIVE" else 1)
    assert len([call for call in api.calls if call[0] == "POST" and call[1].endswith("/clusters")]) == 1


def test_sensor_job_owns_distinct_python_entry_and_compute_with_no_gold_writer():
    api, bundle = Api(), bootstrap.runtime_archive()
    config = {"streaming_mode": "persistent"}
    social = bootstrap.install_job(api, "ws", "social-compute", stream_runtime(config), bundle, ensure_folder=lambda *_: None)
    sensor = bootstrap.install_job(api, "ws", "sensor-compute", stream_runtime(config), bundle, ensure_folder=lambda *_: None, workflow="sensors")
    assert social != sensor
    detail = api.resources["/workspaces/ws/jobs/" + sensor]
    task = detail["tasks"][0]
    assert task["taskKey"] == "sensor_stream" and task["cluster"] == {"clusterKey": "sensor-compute"}
    assert task["isStreaming"] is True and detail["timeoutSeconds"] == 0 and detail["maxConcurrentRuns"] == 1
    source = next(value["content"] for value in api.contents.values() if value["path"] == task["filePath"])
    assert detail["name"] == "wf_ai_gods_eye_view_sensor_stream" and task["filePath"].endswith("/sensor_stream.py")
    assert "def run_sensor_stream(" in source and "def run_social_network(" not in source
    assert "def publish(" not in source and "def publish_snapshot(" not in source
    api.resources["/workspaces/ws/jobRuns"] = [{"key": "active-sensors", "jobKey": sensor, "state": {"status": "RUNNING"}}]
    assert bootstrap.start_stream_job(api, "ws", sensor, "sensor_stream") == "active-sensors"
    with pytest.raises(RuntimeError, match="Stop the managed"):
        bootstrap.install_job(api, "ws", "other-compute", stream_runtime(config), bundle, ensure_folder=lambda *_: None, workflow="sensors")
    assert not any(method == "POST" and path.endswith("/jobRuns") for method, path, *_ in api.calls)


@pytest.mark.parametrize("state", ["RUNNING", "FAILED", "SUCCESS"])
def test_permanent_readiness_requires_running_current_heartbeats_and_same_publication(monkeypatch, state):
    class Streams(Api):
        def request(self, method, path, **options):
            if path.endswith("/taskRuns"):
                task = "sensor_stream" if options["params"]["jobRunKey"] == "sensor-run" else "prisma_tick"
                return SimpleNamespace(body={"items": [{"taskKey": task, "state": {"status": state}}]}, headers={})
            if "/jobRuns/" in path:
                return SimpleNamespace(body={"state": {"status": state}}, headers={})
            return super().request(method, path, **options)
    revision = "current-code"
    reads = []
    def read(_connection, name):
        reads.append(name)
        if len(reads) <= 2:
            return {"pipeline_revision": "old-code", "status": "running"}
        return {"pipeline_revision": revision, "status": "running" if name == "status_sensorstream" else "ready", "version": "gold-current"}
    monkeypatch.setattr(bootstrap, "read_document", read)
    validated = []
    monkeypatch.setattr(bootstrap, "validate_publication", lambda *_: validated.append(True) or "gold-current")
    runtime = {"workspace_key": "ws", "pipeline_revision": revision}
    runs = [("social-run", "prisma_tick"), ("sensor-run", "sensor_stream")]
    if state == "RUNNING":
        assert bootstrap.wait_stream_jobs(Streams(), object(), runtime, runs, object()) == "gold-current"
        assert len(reads) == 4 and validated == [True]
    else:
        with pytest.raises(RuntimeError, match="stopped or failed"):
            bootstrap.wait_stream_jobs(Streams(), object(), runtime, runs, object())
        assert validated == []


def test_existing_api_preserves_explicit_revision_retry_token(monkeypatch):
    api = post_apply.AidpApi("us-chicago-1", "platform", None, "deployment")
    seen = []
    def send(_method, _url, **kwargs):
        seen.append(dict(kwargs["headers"]))
        return SimpleNamespace(status_code=503 if len(seen) == 1 else 201, content=b"{}", json=lambda: {}, headers={})
    monkeypatch.setattr(api.session, "request", send)
    monkeypatch.setattr(post_apply, "_sleep", lambda _: None)
    api.request("POST", "/workspaces/ws/clusters/compute/actions/start", payload={}, headers={"opc-retry-token": "a" * 32})
    assert len(seen) == 2 and all(headers["opc-retry-token"] == "a" * 32 for headers in seen)


@pytest.mark.parametrize("previous", [None, "migrated", "unmigrated", "job", "agent", "credential", "objects", "denied"])
def test_object_controls_require_proven_fresh_install_or_verified_migration(monkeypatch, previous):
    monkeypatch.syspath_prepend(str(bootstrap.ROOT / "apps/backend/tests"))
    from test_territorial_control_store import Objects, ready
    storage, api = Objects(), Api()
    runtime = {"namespace": "namespace", "bucket": "gold", "landing_bucket": "landing",
               "landing_prefix": "01_landing/prisma/raw/", "analytics_store": "gold"}
    connection = bootstrap.ObjectControlStore(storage, "namespace", "gold")
    if previous == "migrated":
        ready(connection)
    elif previous == "unmigrated":
        bootstrap.write_document(connection, "runtime", {"analytics_store": "gold"}, 0)
    elif previous == "job":
        api.resources["/workspaces/ws/jobs"] = [{"name": "prisma_bogota_tick"}]
    elif previous == "agent":
        api.resources["/agents"] = [{"displayName": "prisma_bogota_agent_legacy"}]
    elif previous == "credential":
        api.resources["/credentials"] = [{"displayName": "PrismaWriterRuntime"}]
    def listed(*_, **__):
        if previous == "denied":
            raise PermissionError("Cannot prove an empty deployment")
        return SimpleNamespace(data=SimpleNamespace(objects=[object()] if previous == "objects" else [], next_start_with=None))
    storage.list_objects = listed
    before = list(storage.calls)
    monkeypatch.setitem(sys.modules, "oracledb", None)
    if previous in {None, "migrated"}:
        result = bootstrap.initialize_controls(api, api, storage, runtime, "ws")
        result.require_ready()
        document = bootstrap.read_document(result, "runtime")
        assert document.get("control_new_install") is (True if previous is None else None)
        if previous is None:
            assert storage.calls[-1][1]["if_none_match"] == "*"
    else:
        with pytest.raises((RuntimeError, PermissionError)):
            bootstrap.initialize_controls(api, api, storage, runtime, "ws")
        assert storage.calls == before
    assert all(call[0] == "GET" for call in api.calls)


@pytest.mark.parametrize("failed_phase", ["job", "snapshot", None])
def test_bootstrap_publishes_agent_pointer_only_after_native_acceptance(monkeypatch, tmp_path, failed_phase):
    outputs = {"objectstorage_namespace": "ns", "bucket_name": "landing",
               "medallion_bucket_names": {"gold": "gold", "landing": "landing"}, "agent_model_id": "model",
               "compartment_ocid": "compartment", "ai_data_platform_id": "platform"}
    published, runtime_documents, credential_apis = [], [], []
    database = SimpleNamespace(commit=lambda: None)
    monkeypatch.setitem(sys.modules, "oracledb", None)
    monkeypatch.setattr(bootstrap.tempfile, "TemporaryDirectory", lambda **_: nullcontext(str(tmp_path)))
    def initialize_controls(api, agent_api, storage, runtime, workspace):
        assert (agent_api.api_version, agent_api.resource_segment) == ("20260430", "aiDataPlatforms")
        credential_apis.append(agent_api)
        return database
    def install_job(api, _workspace, compute, config, _bundle, **options):
        assert (api.api_version, api.resource_segment) == ("20240831", "dataLakes")
        assert config["streaming_mode"] == "persistent" and len(config["pipeline_revision"]) == 64
        workflow = options.get("workflow", "social")
        assert compute == ("aidp_gods_eye_view_sensor_compute" if workflow == "sensors" else "aidp_gods_eye_view_social_compute")
        return "job-" + workflow
    monkeypatch.setattr(bootstrap, "database_users", lambda *_args, **_kwargs: pytest.fail("No database provisioning"))
    monkeypatch.setattr(bootstrap, "initialize_controls", initialize_controls)
    monkeypatch.setattr(bootstrap, "ensure_oci_credential", lambda *_: {"displayName": "AidpRuntime"})
    monkeypatch.setattr(bootstrap, "install_volumes", lambda *_: None)
    monkeypatch.setattr(bootstrap, "install_stream_compute", lambda _api, _workspace, name: name)
    def install_libraries(api, *_, **__):
        assert (api.api_version, api.resource_segment) == ("20260430", "aiDataPlatforms")
    monkeypatch.setattr(bootstrap, "install_cluster_libraries", install_libraries)
    monkeypatch.setattr(bootstrap, "install_job", install_job)
    monkeypatch.setattr(bootstrap, "read_document", lambda *_: {"revision": 0})
    monkeypatch.setattr(bootstrap, "write_document", lambda _db, _name, data, _revision: runtime_documents.append(data))
    def agent(api, *_):
        assert api is credential_apis[0]
        return {"state": "ACTIVE", "revision": "bundle", "endpoint": "native", "agent_key": "agent"}
    monkeypatch.setattr(bootstrap, "publish_agent", agent)
    def check(phase, result):
        assert all(item[2] != ".control/prisma/agent.json" for item in published)
        assert published[0][:4] == ("ns", "landing", "01_landing/prisma/raw/.keep", b"")
        assert published[1][:4] == ("ns", "landing", "01_landing/prisma/raw/sensors/.keep", b"")
        if failed_phase == phase:
            raise RuntimeError("Territorial " + phase + " failed")
        return result
    def initial_job(api, _workspace, job, task):
        assert (api.api_version, api.resource_segment) == ("20240831", "dataLakes")
        assert (job, task) in {("job-social", "social_network"), ("job-sensors", "sensor_stream")}
        return check("job", "run")
    monkeypatch.setattr(bootstrap, "start_stream_job", initial_job)
    monkeypatch.setattr(bootstrap, "wait_stream_jobs", lambda *_: check("snapshot", "gold-version"))
    storage = SimpleNamespace(put_object=lambda *args, **_: published.append(args))
    wallet = io.BytesIO()
    with zipfile.ZipFile(wallet, "w") as archive:
        archive.writestr("tnsnames.ora", "db_low = ()")
    arguments = (Api(), {"region": "us-chicago-1", "deployment_id": "deployment"}, outputs,
                 {"tenancy": "test-tenancy", "user": "test-user", "fingerprint": "test-id"}, None, storage, wallet.getvalue(), "test-wallet", "test-admin",
                 {"workspace_key": "ws", "shared_compute_key": "compute", "catalog_name": "catalog"})
    helpers = dict(wallet_dsn=post_apply._wallet_dsn, validate_wallet=post_apply._validate_wallet,
                   generate_password=post_apply._generated_database_password, ensure_folder=post_apply.ensure_workspace_folder)
    if failed_phase:
        with pytest.raises(RuntimeError, match=failed_phase + " failed"):
            bootstrap.bootstrap_territorial(*arguments, deadline=bootstrap.time.monotonic() + 100, **helpers)
        assert all(item[2] != ".control/prisma/agent.json" for item in published)
    else:
        result = bootstrap.bootstrap_territorial(*arguments, deadline=bootstrap.time.monotonic() + 100, **helpers)
        assert result["prisma_snapshot_version"] == "gold-version"
        assert published[-1][:3] == ("ns", "gold", ".control/prisma/agent.json")
    assert runtime_documents[0]["bucket"] == "gold"
    assert runtime_documents[0]["analytics_store"] == "gold"
    assert "writer_credential_name" not in runtime_documents[0] and "reader_credential_name" not in runtime_documents[0]
    assert runtime_documents[0]["agent_compute_id"] == "key-aidp_gods_eye_view_agent_compute"
    assert runtime_documents[0]["workbench_base"] == arguments[0].base
    assert runtime_documents[0]["landing_bucket"] == "landing"
    assert runtime_documents[0]["landing_volume_path"] == "/Volumes/catalog/prisma_ingest/landing"
    assert runtime_documents[0]["checkpoint_volume_path"] == "/Volumes/catalog/prisma_ingest/checkpoints/bronze-v1"
    assert runtime_documents[0]["sensor_landing_prefix"] == "01_landing/prisma/raw/sensors/"
    assert runtime_documents[0]["sensor_landing_volume_path"] == "/Volumes/catalog/prisma_ingest/landing/sensors"
    assert runtime_documents[0]["sensor_checkpoint_volume_path"] == "/Volumes/catalog/prisma_ingest/checkpoints/sensors-v1"
    assert runtime_documents[0]["job_key"] == "job-social"
    assert runtime_documents[0]["sensor_job_key"] == "job-sensors"
