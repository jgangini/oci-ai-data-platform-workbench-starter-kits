"""Offline native AIDP contract checks; no local test claims remote readiness."""
import ast
from contextlib import nullcontext
import io
import json
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "hooks"))
import prisma_bootstrap as bootstrap
import post_apply


class Api:
    def __init__(self, *_args, **kwargs):
        self.calls, self.contents, self.resources = [], {}, {}
        self.deployment_id = "deployment-test"
        self.deploy_state = "ACTIVE"
        self.run_state = "SUCCESS"
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
        if path.endswith("/actions/deploy"):
            self.resources[path.removesuffix("/actions/deploy")] = [{"key": "deployment", "lifecycleState": "DEPLOYING"}]
            return SimpleNamespace(body={}, headers={"aidp-async-operation-key": "operation"})
        if path.endswith("/deployments/deployment"):
            return response({"key": "deployment", "lifecycleState": self.deploy_state,
                "endpointUrl": "https://gateway.aidp.us-chicago-1.oci.oraclecloud.com/agentendpoint/managed/chat",
                "sessionRetentionConfig": {"retentionPeriodInDays": 7, "unused": None}})
        if path.endswith("/jobRuns") and method == "POST":
            return response({"key": "run-one"})
        if path.endswith("/jobRuns/run-one"):
            return response({"state": {"status": self.run_state}})
        if path.endswith("/taskRuns"):
            assert params["limit"] == 100, "Native task-run API rejects limit=1000"
            return response({"items": [{"taskKey": "prisma_tick", "state": {"status": self.run_state}}]})
        if method == "PUT" and "/jobs/" in path:
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


@pytest.fixture(autouse=True)
def offline_deadline(monkeypatch):
    monkeypatch.setattr(bootstrap, "_deadline", 0)
    monkeypatch.setattr(bootstrap.time, "sleep", lambda _: None)


def test_bundle_is_deterministic_compilable_and_contains_classification():
    bundle = bootstrap.runtime_archive()
    assert bundle == bootstrap.runtime_archive()
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        assert "prisma/classification.py" in archive.namelist()
        assert "prisma/scheduling.py" in archive.namelist()
        assert "prisma/pipeline.py" in archive.namelist()
        assert all("local" not in name and "api" not in name for name in archive.namelist())
        for name in archive.namelist():
            ast.parse(archive.read(name), filename=name)
    ast.parse(bootstrap.bundle_prelude(bundle))


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
    api.resources["/credentials"] = [{"key": "existing", "displayName": "PrismaReaderRuntime", "lifeCycleState": "CREATING"}]

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


def test_agent_publish_waits_async_and_detail_active_without_deleting_prior_release():
    api = Api()
    api.resources["/workspaces/ws/agents"] = [{"displayName": "prior-agent", "key": "prior", "lifecycleState": "ACTIVE"}]
    result = bootstrap.publish_agent(api, "ws", bootstrap.runtime_archive(), "us-chicago-1")
    assert result["state"] == "ACTIVE" and result["endpoint"].endswith("/chat")
    assert any(path == "/asyncOperations/operation" for _, path, _, _ in api.calls)
    assert not any(method == "DELETE" for method, _, _, _ in api.calls)
    assert api.resources["/workspaces/ws/agents"][0]["key"] == "prior"
    source = next(value["content"] for path, value in api.contents.items() if "agent_" in path)
    ast.parse(source)


def test_failed_agent_deployment_never_returns_ready():
    api = Api()
    api.deploy_state = "FAILED"
    with pytest.raises(RuntimeError, match="prior deployment preserved"):
        bootstrap.publish_agent(api, "ws", bootstrap.runtime_archive(), "us-chicago-1")
    assert not any(method == "DELETE" for method, _, _, _ in api.calls)


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


def test_job_uses_native_notebook_create_rename_export_and_preserves_live_schedule(monkeypatch):
    api = Api()
    bundle = bootstrap.runtime_archive()
    job = bootstrap.install_job(api, "ws", "compute", {"bucket": "gold"}, bundle, ensure_folder=lambda *_: None)
    detail = api.resources["/workspaces/ws/jobs/" + job]
    detail["schedule"] = {**detail["schedule"], "pauseStatus": "UNPAUSED"}
    assert bootstrap.install_job(api, "ws", "compute", {"bucket": "gold"}, bundle, ensure_folder=lambda *_: None) == job
    assert detail["schedule"]["pauseStatus"] == "UNPAUSED"
    assert any(method == "PATCH" and "/notebook/api/contents/" in path for method, path, _, _ in api.calls)
    assert any("/actions/export/contents/" in path for _, path, _, _ in api.calls)
    assert len([1 for method, path, _, _ in api.calls if method == "POST" and path.endswith("/jobs")]) == 1
    assert len([1 for method, path, _, _ in api.calls if method == "PUT" and "/jobs/" in path]) == 1


@pytest.mark.parametrize("second_task,state", [("prisma_tick", "SUCCESS"), ("prisma_tick", "FAILED"), ("other_task", "SUCCESS")])
def test_initial_job_accepts_successful_native_task_attempts_but_never_hides_failure(monkeypatch, second_task, state):
    api = Api()
    job = bootstrap.install_job(api, "ws", "compute", {}, bootstrap.runtime_archive(), ensure_folder=lambda *_: None)
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
    if second_task == "prisma_tick" and state == "SUCCESS":
        assert bootstrap.run_initial_job(api, "ws", job, "same-notebook") == "run-one"
    else:
        with pytest.raises(RuntimeError, match="initial task failed; no readiness claimed"):
            bootstrap.run_initial_job(api, "ws", job, "same-notebook")


@pytest.mark.parametrize("etag", [None, "native-version"])
def test_existing_job_reconciles_without_required_etag_and_defaults_null_schedule(monkeypatch, etag):
    api, bundle = Api(), bootstrap.runtime_archive()
    job = bootstrap.install_job(api, "ws", "compute", {}, bundle, ensure_folder=lambda *_: None)
    path = "/workspaces/ws/jobs/" + job
    api.resources[path].update(schedule=None, timeoutSeconds=42)
    request = api.request

    def with_etag(method, resource, **kwargs):
        response = request(method, resource, **kwargs)
        if method == "GET" and resource == path and etag:
            response.headers["etag"] = etag
        return response

    monkeypatch.setattr(api, "request", with_etag)
    assert bootstrap.install_job(api, "ws", "compute", {}, bundle, ensure_folder=lambda *_: None) == job
    updates = [call for call in api.calls if call[:2] == ("PUT", path)]
    assert len(updates) == 2
    assert updates[-1][3] == ({"If-Match": etag} if etag else None)
    assert api.resources[path]["schedule"]["pauseStatus"] == "PAUSED"
    assert api.resources[path]["timeoutSeconds"] == 600


def test_notebook_uses_native_aidputils_and_versions_the_complete_content(monkeypatch, tmp_path):
    api, bundle, calls = Api(), bootstrap.runtime_archive(), []
    config = {"bucket": "gold"}
    secret_get = lambda **_: None
    spark = object()
    monkeypatch.setitem(sys.modules, "aidputils", None)
    monkeypatch.setitem(sys.modules, "prisma.pipeline", SimpleNamespace(run=lambda *args: calls.append(args)))
    monkeypatch.setattr(bootstrap.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(sys, "path", list(sys.path))
    job = bootstrap.install_job(api, "ws", "compute", config, bundle, ensure_folder=lambda *_: None)
    notebooks = {path: value for path, value in api.contents.items() if value["type"] == "notebook"}
    cells = next(iter(notebooks.values()))["content"]["cells"]
    assert len(cells) == 1 and "%pip" not in "".join(cells[0]["source"])
    cell = cells[0]
    exec("".join(cell["source"]), {"spark": spark, "oidlUtils": SimpleNamespace(),
        "aidputils": SimpleNamespace(secrets=SimpleNamespace(get=secret_get))})
    assert calls == [(spark, secret_get, config)]
    assert bootstrap.install_job(api, "ws", "compute", {"bucket": "updated"}, bundle, ensure_folder=lambda *_: None) == job
    assert len({path for path, value in api.contents.items() if value["type"] == "notebook"}) == 2
    assert all(api.contents[path] == value for path, value in notebooks.items())
    bootstrap.run_initial_job(api, "ws", job, "same-bundle")
    first_token = next(call[3]["opc-retry-token"] for call in reversed(api.calls) if call[:2] == ("POST", "/workspaces/ws/jobRuns"))
    bootstrap.run_initial_job(api, "ws", job, "same-bundle")
    assert next(call[3]["opc-retry-token"] for call in reversed(api.calls) if call[:2] == ("POST", "/workspaces/ws/jobRuns")) == first_token
    bootstrap.install_job(api, "ws", "compute", {"bucket": "wrapper-only-update"}, bundle, ensure_folder=lambda *_: None)
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
    monkeypatch.setattr(api, "request", request)
    installed = bootstrap.install_cluster_libraries(api, "ws", "compute", ensure_folder=lambda *_: None)
    assert installed.endswith("/requirements.txt")
    assert bootstrap.install_cluster_libraries(api, "ws", "compute", ensure_folder=lambda *_: None) == installed
    assert len([call for call in api.calls if call[:2] == ("PATCH", base + "/libraries")]) == int(initial_status is None)
    assert len([call for call in api.calls if call[:2] == ("POST", base + "/actions/restart")]) == int(initial_status != "INSTALLED")
    assert next(iter(api.contents.values()))["content"] == "httpx==0.28.1\noracledb==3.4.2\n"


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
    bootstrap.cluster_idle(api, "ws", "compute")
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


def test_existing_api_preserves_explicit_revision_retry_token(monkeypatch):
    api = post_apply.AidpApi("us-chicago-1", "platform", None, "deployment")
    seen = {}
    def send(_method, _path, headers, *_args):
        seen.update(headers)
        return SimpleNamespace(status_code=201, content=b"{}", json=lambda: {}, headers={})
    monkeypatch.setattr(api, "_send", send)
    api.request("POST", "/workspaces/ws/jobRuns", payload={"jobKey": "job"}, headers={"opc-retry-token": "a" * 64})
    assert seen["opc-retry-token"] == "a" * 64


@pytest.mark.parametrize("failed_phase", ["job", "snapshot", None])
def test_bootstrap_publishes_agent_pointer_only_after_native_acceptance(monkeypatch, tmp_path, failed_phase):
    outputs = {"objectstorage_namespace": "ns", "bucket_name": "landing",
               "medallion_bucket_names": {"gold": "gold", "landing": "landing"}, "agent_model_id": "model",
               "compartment_ocid": "compartment", "ai_data_platform_id": "platform"}
    published, runtime_documents, credential_apis = [], [], []
    database = SimpleNamespace(commit=lambda: None)
    monkeypatch.setitem(sys.modules, "oracledb", SimpleNamespace(connect=lambda **_: nullcontext(database)))
    monkeypatch.setattr(bootstrap.tempfile, "TemporaryDirectory", lambda **_: nullcontext(str(tmp_path)))
    def database_users(api, *_, **__):
        assert (api.api_version, api.resource_segment) == ("20260430", "aiDataPlatforms")
        credential_apis.append(api)
    def install_job(api, *_, **__):
        assert (api.api_version, api.resource_segment) == ("20240831", "dataLakes")
        return "job"
    monkeypatch.setattr(bootstrap, "database_users", database_users)
    monkeypatch.setattr(bootstrap, "install_volumes", lambda *_: None)
    def install_libraries(api, *_, **__):
        assert (api.api_version, api.resource_segment) == ("20260430", "aiDataPlatforms")
    monkeypatch.setattr(bootstrap, "install_cluster_libraries", install_libraries)
    monkeypatch.setattr(bootstrap, "install_job", install_job)
    monkeypatch.setattr(bootstrap, "read_document", lambda *_: {"revision": 0})
    monkeypatch.setattr(bootstrap, "write_document", lambda _db, _name, data, _revision: runtime_documents.append(data))
    def agent(api, *_):
        assert api is credential_apis[0]
        return {"state": "ACTIVE", "revision": "bundle", "endpoint": "native"}
    monkeypatch.setattr(bootstrap, "publish_agent", agent)
    def check(phase, result):
        assert all(item[2] != ".control/prisma/agent.json" for item in published)
        assert published[0][:4] == ("ns", "landing", "01_landing/prisma/raw/.keep", b"")
        if failed_phase == phase:
            raise RuntimeError("PRISMA " + phase + " failed")
        return result
    def initial_job(api, *_):
        assert (api.api_version, api.resource_segment) == ("20240831", "dataLakes")
        return check("job", "run")
    monkeypatch.setattr(bootstrap, "run_initial_job", initial_job)
    monkeypatch.setattr(bootstrap, "validate_publication", lambda *_: check("snapshot", "gold-version"))
    storage = SimpleNamespace(put_object=lambda *args, **_: published.append(args))
    wallet = io.BytesIO()
    with zipfile.ZipFile(wallet, "w") as archive:
        archive.writestr("tnsnames.ora", "db_low = ()")
    arguments = (Api(), {"region": "us-chicago-1", "deployment_id": "deployment"}, outputs,
                 {}, None, storage, wallet.getvalue(), "test-wallet", "test-admin",
                 {"workspace_key": "ws", "shared_compute_key": "compute", "catalog_name": "catalog"})
    helpers = dict(wallet_dsn=post_apply._wallet_dsn, validate_wallet=post_apply._validate_wallet,
                   generate_password=post_apply._generated_database_password, ensure_folder=post_apply.ensure_workspace_folder)
    if failed_phase:
        with pytest.raises(RuntimeError, match=failed_phase + " failed"):
            bootstrap.bootstrap_prisma(*arguments, deadline=bootstrap.time.monotonic() + 100, **helpers)
        assert all(item[2] != ".control/prisma/agent.json" for item in published)
    else:
        result = bootstrap.bootstrap_prisma(*arguments, deadline=bootstrap.time.monotonic() + 100, **helpers)
        assert result["prisma_snapshot_version"] == "gold-version"
        assert published[-1][:3] == ("ns", "gold", ".control/prisma/agent.json")
    assert runtime_documents[0]["bucket"] == "gold"
    assert runtime_documents[0]["workbench_base"] == arguments[0].base
    assert runtime_documents[0]["landing_bucket"] == "landing"
    assert runtime_documents[0]["landing_volume_path"] == "/Volumes/catalog/prisma_ingest/landing"
    assert runtime_documents[0]["checkpoint_volume_path"] == "/Volumes/catalog/prisma_ingest/checkpoints/bronze-v1"
