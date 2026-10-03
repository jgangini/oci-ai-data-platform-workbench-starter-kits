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
            return response({"items": [{"taskKey": "prisma_tick", "state": {"status": self.run_state}}]})
        if method == "PUT" and "/jobs/" in path:
            self.resources[path].update(payload)
            return response(self.resources[path])
        if method == "POST":
            value = {**payload, "key": "key-" + (payload.get("displayName") or payload["name"]), "lifecycleState": "ACTIVE"}
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


def test_initial_job_requires_native_run_and_task_success_and_revision_token():
    api = Api()
    assert bootstrap.run_initial_job(api, "ws", "job", "revision") == "run-one"
    method, path, payload, headers = api.calls[0]
    assert (method, path, payload) == ("POST", "/workspaces/ws/jobRuns", {"jobKey": "job", "parameters": []})
    assert len(headers["opc-retry-token"]) == 64
    assert api.calls[-1][1] == "/workspaces/ws/taskRuns"
    api.run_state = "FAILED"
    with pytest.raises(RuntimeError, match="no readiness claimed"):
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
               "medallion_bucket_names": {"gold": "gold"}, "agent_model_id": "model",
               "compartment_ocid": "compartment", "ai_data_platform_id": "platform"}
    published, runtime_documents = [], []
    database = SimpleNamespace(commit=lambda: None)
    monkeypatch.setitem(sys.modules, "oracledb", SimpleNamespace(connect=lambda **_: nullcontext(database)))
    monkeypatch.setattr(bootstrap.tempfile, "TemporaryDirectory", lambda **_: nullcontext(str(tmp_path)))
    monkeypatch.setattr(bootstrap, "database_users", lambda *_, **__: None)
    monkeypatch.setattr(bootstrap, "install_job", lambda *_, **__: "job")
    monkeypatch.setattr(bootstrap, "read_document", lambda *_: {"revision": 0})
    monkeypatch.setattr(bootstrap, "write_document", lambda _db, _name, data, _revision: runtime_documents.append(data))
    def agent(api, *_):
        assert api.api_version == "20260430"
        return {"state": "ACTIVE", "revision": "bundle", "endpoint": "native"}
    monkeypatch.setattr(bootstrap, "publish_agent", agent)
    def check(phase, result):
        assert not published
        if failed_phase == phase:
            raise RuntimeError("PRISMA " + phase + " failed")
        return result
    monkeypatch.setattr(bootstrap, "run_initial_job", lambda *_: check("job", "run"))
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
        assert not published
    else:
        result = bootstrap.bootstrap_prisma(*arguments, deadline=bootstrap.time.monotonic() + 100, **helpers)
        assert result["prisma_snapshot_version"] == "gold-version"
        assert published[0][:3] == ("ns", "gold", ".control/prisma/agent.json")
    assert runtime_documents[0]["bucket"] == "gold"
    assert runtime_documents[0]["workbench_base"] == arguments[0].base
