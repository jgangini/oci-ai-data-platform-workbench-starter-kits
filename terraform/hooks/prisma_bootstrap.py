"""Additive PRISMA bootstrap after the existing encrypted operator delivery."""
from __future__ import annotations

import base64
import ast
import hashlib
import io
import json
import re
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from urllib.parse import quote, urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps/backend"))
from app.prisma.database import install_schema, read_document, write_document

SESSION_RETENTION = {"retentionPeriodInDays": 7}
_deadline = 0.0


def pause(seconds=5):
    if _deadline and time.monotonic() + seconds >= _deadline:
        raise RuntimeError("PRISMA bootstrap reached the shared post-apply deadline")
    if seconds:
        time.sleep(seconds)


def operation(api, response):
    key = response.headers.get("aidp-async-operation-key")
    while key:
        pause(0)
        status = api.request("GET", "/asyncOperations/" + quote(key, safe="")).body.get("status")
        if status == "SUCCEEDED":
            return
        if status not in {"IN_PROGRESS", "ACCEPTED"}:
            raise RuntimeError("PRISMA asynchronous operation failed or returned an unknown state")
        pause()


def items(api, path, params=None):
    result, page = [], None
    while True:
        pause(0)
        response = api.request("GET", path, params={**(params or {}), **({"page": page} if page else {})})
        body = response.body
        result.extend(body if isinstance(body, list) else body.get("items", []))
        page = response.headers.get("opc-next-page")
        if not page:
            return result


def named(api, path, name):
    matches = [item for item in items(api, path) if (item.get("displayName") or item.get("name")) == name
               and (item.get("lifecycleState") or item.get("state")) != "DELETED"]
    if len(matches) > 1:
        raise RuntimeError("Duplicate managed PRISMA resource")
    if matches:
        return matches[0]
    if path.endswith("/clusters"):
        return hidden_compute(api, path, name)
    return None


def hidden_compute(api, path, name):
    # AIDP can expose new AI Compute only by key before it appears in the cluster list.
    workspace_prefix = path.split("/")[2] + "."
    operations = [item for item in items(api, "/asyncOperations", {"resourceType": "AI_COMPUTE"})
                  if item.get("resourceDisplayName") == name
                  and item.get("actionType") in {"CREATE_CLUSTER", "DELETE_CLUSTER"}
                  and str(item.get("resourceName", "")).startswith(workspace_prefix)]
    if not operations:
        return None
    latest = max(operations, key=lambda item: str(item.get("timeStarted") or ""))
    if latest.get("actionType") == "DELETE_CLUSTER":
        if latest.get("status") in {"SUCCESS", "SUCCEEDED"}:
            return None
        raise RuntimeError("PRISMA managed compute is being deleted")
    key = latest["resourceName"][len(workspace_prefix):]
    return hidden_compute_detail(api, path, name, key, latest.get("status"))


def hidden_compute_detail(api, path, name, key, status):
    try:
        detail = api.request("GET", path + "/" + quote(key, safe="")).body
    except RuntimeError as exc:
        if getattr(exc, "status_code", None) != 404:
            raise
        if status in {"FAILED", "ERROR", "CANCELED", "CANCELLED"}:
            raise RuntimeError("PRISMA compute creation failed without a resource") from None
        return {"key": key, "displayName": name, "type": "AI_COMPUTE", "state": "CREATING"}
    if ((detail.get("displayName") or detail.get("name")) != name
            or (detail.get("type") or detail.get("sourceApi")) != "AI_COMPUTE"):
        raise RuntimeError("PRISMA compute operation points to an incompatible resource")
    return detail


def ensure(api, path, name, payload, *, ready=False):
    current = named(api, path, name)
    if not current:
        operation(api, api.request("POST", path, payload=payload))
    while True:
        current = named(api, path, name)
        if current:
            state = str(current.get("lifecycleState") or current.get("state") or "").upper()
            if state in {"FAILED", "INACTIVE", "DELETED", "DELETING", "CANCELED", "CANCELLED"} or state.endswith("_FAILED"):
                raise RuntimeError("Managed PRISMA resource entered terminal state " + state)
            if not ready or state in {"ACTIVE", "STOPPED"}:
                return current
        pause()


def credential(api, name, values):
    if named(api, "/credentials", name):
        return
    ensure(api, "/credentials", name, {"displayName": name, "type": "SECRET_TOKEN",
        "credentialDescription": "PRISMA managed runtime; never return secret values",
        "credentialDetails": {"credentialType": "SECRET_TOKEN", "secretTokenPair":
            [{"secretKey": key, "secretValue": value} for key, value in values.items()]}}, ready=True)


def database_users(api, wallet, wallet_password, admin_password, config, outputs, *, wallet_dsn, validate_wallet, generate_password):
    import oracledb
    with tempfile.TemporaryDirectory(prefix="prisma-bootstrap-") as directory:
        with zipfile.ZipFile(io.BytesIO(validate_wallet(wallet))) as archive:
            archive.extractall(directory)
        dsn = wallet_dsn(Path(directory))
        with oracledb.connect(user="ADMIN", password=admin_password, dsn=dsn, config_dir=directory,
                             wallet_location=directory, wallet_password=wallet_password) as connection:
            install_schema(connection)
            cursor = connection.cursor()
            for user, name, reader in (("PRISMA_WRITER", "PrismaWriterRuntime", False), ("PRISMA_READER", "PrismaReaderRuntime", True)):
                if named(api, "/credentials", name):
                    continue
                generated_password = generate_password()
                cursor.execute("SELECT COUNT(*) FROM ALL_USERS WHERE USERNAME=:name", name=user)
                verb = "ALTER" if cursor.fetchone()[0] else "CREATE"
                cursor.execute(f'{verb} USER {user} IDENTIFIED BY "{generated_password}"')
                cursor.execute(f"GRANT CREATE SESSION TO {user}")
                if reader:
                    for view in ("PRISMA_V_SNAPSHOTS", "PRISMA_V_INCIDENTS", "PRISMA_V_EVIDENCE"):
                        cursor.execute(f"GRANT SELECT ON ADMIN.{view} TO {user}")
                else:
                    cursor.execute(f"GRANT EXECUTE ON ADMIN.PRISMA_CONTROL TO {user}")
                values = {"db_user": user, "db_password": generated_password, "dsn": dsn,
                    "wallet": base64.b64encode(wallet).decode(), "wallet_password": wallet_password,
                    "region": config["region"], "compartment_id": outputs["compartment_ocid"], "model_id": outputs["agent_model_id"]}
                if not reader:
                    values.update({key: str(config[key]) for key in ("tenancy", "user", "fingerprint")})
                    values["private_key"] = Path(config["key_file"]).read_text(encoding="utf-8")
                credential(api, name, values)
            connection.commit()


def runtime_archive():
    buffer = io.BytesIO()
    names = ("__init__.py", "core.py", "media.py", "area.py", "x.py", "database.py", "runtime_secrets.py", "classification.py", "scheduling.py", "capture.py", "landing.py", "pipeline.py", "agent.py")
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in names:
            info = zipfile.ZipInfo("prisma/" + name, date_time=(2026, 1, 1, 0, 0, 0))
            source = (ROOT / "apps/backend/app/prisma" / name).read_bytes()
            ast.parse(source, filename=name)
            archive.writestr(info, source)
    return buffer.getvalue()


def bundle_prelude(bundle):
    encoded = base64.b64encode(bundle).decode()
    digest = hashlib.sha256(bundle).hexdigest()
    return f'''import base64, hashlib, os, sys, tempfile
_prisma_bundle = base64.b64decode({encoded!r})
assert hashlib.sha256(_prisma_bundle).hexdigest() == {digest!r}
_prisma_path = os.path.join(tempfile.gettempdir(), 'prisma-{digest[:16]}.zip')
with open(_prisma_path, 'wb') as _prisma_file:
    _prisma_file.write(_prisma_bundle)
if _prisma_path not in sys.path:
    sys.path.insert(0, _prisma_path)
'''


def upload(api, workspace, path, content, kind="file"):
    if kind == "notebook":
        from app.aidp import AidpClient
    endpoint = f"/workspaces/{workspace}/notebook/api/contents/{quote(path, safe='')}"
    try:
        existing = api.request("GET", endpoint, params={"content": "1"}).body
    except RuntimeError as exc:
        if getattr(exc, "status_code", None) != 404:
            raise
        existing = None
    if existing:
        if (AidpClient._notebook_matches(existing.get("content"), content) if kind == "notebook" else existing.get("content") == content):
            return
        raise RuntimeError("PRISMA versioned workspace path already has different content")
    if kind == "notebook":
        parent = path.rsplit("/", 1)[0]
        created = api.request("POST", f"/workspaces/{workspace}/notebook/api/contents/{quote(parent, safe='')}",
                              payload={"copy_from": None, "ext": ".ipynb", "type": "notebook"}).body
        if not created.get("path"):
            raise RuntimeError("PRISMA notebook creation did not return a path")
        api.request("PATCH", f"/workspaces/{workspace}/notebook/api/contents/{quote(created['path'], safe='')}", payload={"path": path})
    payload = {"name": path.rsplit("/", 1)[-1], "path": path, "type": kind,
               "format": "json" if kind == "notebook" else "text", "content": content}
    api.request("PUT", endpoint, payload=payload)
    if kind == "notebook":
        exported = f"/workspaces/{workspace}/notebook/api/actions/export/contents/{quote(path, safe='')}"
        received = api.request("POST", exported, payload={"format": "ipynb"}).body
        # The native exporter can add metadata and normalize source lists to strings.
        matches = AidpClient._notebook_matches(received.get("content"), content)
    else:
        received = api.request("GET", endpoint, params={"content": "1"}).body
        matches = received.get("content") == content
    if not matches:
        raise RuntimeError("PRISMA workspace content round-trip mismatch")


def install_job(api, workspace, compute, config, bundle, *, ensure_folder):
    from app.aidp import AidpClient
    root = "/Workspace/medallon/prisma"
    ensure_folder(api, workspace, root)
    source = bundle_prelude(bundle) + f"\nfrom prisma.pipeline import run\nrun(spark, oidlUtils.secrets.get, {config!r})\n"
    cells = [{"cell_type": "code", "metadata": {}, "execution_count": None, "outputs": [], "source": value.splitlines(keepends=True)}
             for value in ("%pip install oracledb==3.4.2 httpx==0.28.1 oci==2.160.3\n", source)]
    notebook = {"nbformat": 4, "nbformat_minor": 5, "metadata": {"language_info": {"name": "python"}}, "cells": cells}
    path = root + "/prisma_tick_" + hashlib.sha256(bundle).hexdigest()[:12] + ".ipynb"
    upload(api, workspace, path, notebook, "notebook")
    payload = {"name": "prisma_bogota_tick", "path": root, "description": "Finite PRISMA collection and publication tick",
        "maxConcurrentRuns": 1, "queue": {"isEnabled": False}, "timeoutSeconds": 600,
        "schedule": {"quartzCronExpression": "0 * * * * ?", "timezoneId": "UTC", "pauseStatus": "PAUSED"},
        "jobClusters": [{"clusterKey": compute}], "tasks": [{"type": "NOTEBOOK_TASK", "taskKey": "prisma_tick",
        "dependsOn": [], "runIf": "ALL_SUCCESS", "maxRetries": 0, "isRetryOnTimeout": False,
        "notebookPath": path, "cluster": {"clusterKey": compute}, "parameters": []}]}
    jobs_path = f"/workspaces/{workspace}/jobs"
    current = named(api, jobs_path, payload["name"])
    job = current or ensure(api, jobs_path, payload["name"], {
        field: payload[field] for field in ("name", "path", "description", "maxConcurrentRuns")
    })
    key = str(job["key"])
    detail = api.request("GET", f"/workspaces/{workspace}/jobs/{key}")
    # Preserve a live operator's schedule when updating only the versioned notebook.
    payload["schedule"] = detail.body.get("schedule", payload["schedule"])
    def matches(body):
        return (all(AidpClient._notebook_matches(body.get(field), value) for field, value in payload.items() if field != "tasks")
                and AidpClient._job_tasks_match(body.get("tasks"), payload["tasks"], compute))
    if not matches(detail.body):
        if current and not detail.headers.get("etag"):
            raise RuntimeError("PRISMA job changed and cannot be updated without an ETag")
        operation(api, api.request("PUT", f"/workspaces/{workspace}/jobs/{key}", payload=payload,
                                  headers={"If-Match": detail.headers["etag"]} if detail.headers.get("etag") else None))
        if not matches(api.request("GET", f"/workspaces/{workspace}/jobs/{key}").body):
            raise RuntimeError("PRISMA job definition did not round-trip")
    return key


def agent_deployments(api, base):
    deployments = [item for item in items(api, base) if (item.get("lifecycleState") or item.get("state")) != "DELETED"]
    if len(deployments) > 1:
        raise RuntimeError("Duplicate PRISMA agent deployments")
    return deployments


def deployment_endpoint(detail, region):
    endpoint = str(detail.get("endpointUrl") or "").rstrip("/")
    if not endpoint.endswith("/chat"):
        endpoint += "/chat"
    url = urlsplit(endpoint)
    if (url.scheme != "https" or url.netloc != f"gateway.aidp.{region}.oci.oraclecloud.com"
        or url.query or url.fragment or not re.fullmatch(r"/agentendpoint/[A-Za-z0-9_.-]+/chat", url.path)):
        raise RuntimeError("PRISMA agent endpoint did not round-trip")
    retention = {key: value for key, value in detail.get("sessionRetentionConfig", {}).items() if value is not None}
    if retention != SESSION_RETENTION:
        raise RuntimeError("PRISMA agent retention did not round-trip")
    return endpoint


def wait_agent_deployment(api, base, region):
    while True:
        for item in agent_deployments(api, base):
            detail = api.request("GET", base + "/" + quote(str(item["key"]), safe="")).body
            state = str(detail.get("lifecycleState") or detail.get("state") or "").upper()
            if state in {"FAILED", "INACTIVE", "DELETING", "DELETED"} or state.endswith("_FAILED"):
                raise RuntimeError("PRISMA agent deployment failed; prior deployment preserved")
            if state == "ACTIVE":
                return {"deployment_key": item["key"], "endpoint": deployment_endpoint(detail, region)}
        pause(10)


def publish_agent(api, workspace, bundle, region):
    root = "/Workspace/medallon/prisma"
    digest = hashlib.sha256(bundle).hexdigest()[:12]
    entry = root + f"/agent_{digest}.py"
    dependencies = root + "/requirements_" + digest + ".txt"
    upload(api, workspace, dependencies, "oracledb==3.4.2\n")
    upload(api, workspace, entry, bundle_prelude(bundle) + (ROOT / "apps/backend/app/prisma/agent.py").read_text(encoding="utf-8").replace("from __future__ import annotations\n", ""))
    compute = ensure(api, f"/workspaces/{workspace}/clusters", "prisma_agent_compute", {
        "type": "AI_COMPUTE", "displayName": "prisma_agent_compute", "description": "PRISMA conversational agent",
        "driverConfig": {"driverShapeConfig": {"ocpus": 1, "memoryInGBs": 16}}, "replicaConfig": {"minReplica": 1, "maxReplica": 1}}, ready=True)
    if str(compute.get("type") or compute.get("sourceApi")) != "AI_COMPUTE":
        raise RuntimeError("Existing PRISMA agent compute is not AI_COMPUTE")
    name = "prisma_bogota_" + digest
    agent = ensure(api, f"/workspaces/{workspace}/agents", name, {
        "displayName": name, "description": "PRISMA evidence-grounded Bogotá assistant", "type": "CODE", "pathInfo": "/Workspace",
        "entryFilePath": entry, "dependenciesFilePath": dependencies, "computeKey": compute["key"],
        "sessionConfig": {"variables": {}, "sessionRetentionConfig": SESSION_RETENTION}})
    detail = api.request("GET", f"/workspaces/{workspace}/agents/{quote(str(agent['key']), safe='')}").body
    if (detail.get("type") != "CODE" or str(detail.get("entryFilePath", "")).lstrip("/") != entry.lstrip("/")
        or detail.get("computeKey") != compute["key"]):
        raise RuntimeError("PRISMA agent definition did not round-trip")
    base = f"/workspaces/{workspace}/agents/{agent['key']}/deployments"
    if not agent_deployments(api, base):
        operation(api, api.request("POST", base + "/actions/deploy", payload={"displayName": name + "_deployment",
            "description": "PRISMA deployment " + digest, "agentKey": agent["key"], "agentComputeKey": compute["key"],
            "sessionRetentionConfig": SESSION_RETENTION}))
    return {"state": "ACTIVE", "agent_key": agent["key"], "revision": digest,
            **wait_agent_deployment(api, base, region)}


def run_initial_job(api, workspace, job, revision):
    base = f"/workspaces/{workspace}/jobRuns"
    token = hashlib.sha256(f"{api.deployment_id}:prisma:{job}:{revision}".encode()).hexdigest()
    response = api.request("POST", base, payload={"jobKey": job, "parameters": []},
                           headers={"opc-retry-token": token})
    operation(api, response)
    key = str((response.body or {}).get("key") or "")
    if not key:
        raise RuntimeError("PRISMA initial job run did not return a key")

    def status(value):
        state = value.get("state") or {}
        result = state.get("status") if isinstance(state, dict) else state
        return str(result or value.get("status") or "").upper()

    while True:
        pause(0)
        state = status(api.request("GET", base + "/" + quote(key, safe="")).body)
        if state in {"SUCCESS", "SUCCEEDED"}:
            tasks = items(api, f"/workspaces/{workspace}/taskRuns", {
                "jobRunKey": key, "sortBy": "timeCreated", "sortOrder": "ASC", "limit": 1000,
            })
            if len(tasks) == 1 and tasks[0].get("taskKey") == "prisma_tick" and status(tasks[0]) in {"SUCCESS", "SUCCEEDED"}:
                return key
            if any(status(task) in {"FAILED", "ERROR", "CANCELED", "CANCELLED", "TIMED_OUT", "SKIPPED"} for task in tasks):
                raise RuntimeError("PRISMA initial task failed; no readiness claimed")
        elif state in {"FAILED", "ERROR", "CANCELED", "CANCELLED", "TIMED_OUT", "SKIPPED", "BLOCKED"}:
            raise RuntimeError("PRISMA initial job failed; no readiness claimed")
        pause(10)


def validate_publication(storage, runtime):
    def read(key):
        response = storage.get_object(runtime["namespace"], runtime["bucket"], key)
        content = response.data.content
        if len(content) > 10 * 1024 * 1024:
            raise RuntimeError("PRISMA publication exceeds its bounded bootstrap check")
        return json.loads(content)
    pointer = read("04_gold/prisma/current.json")
    key = str(pointer.get("snapshot_key") or "")
    if not key.startswith("04_gold/prisma/snapshots/") or ".." in key or not pointer.get("version"):
        raise RuntimeError("PRISMA initial publication pointer is invalid")
    snapshot = read(key)
    if snapshot.get("version") != pointer["version"]:
        raise RuntimeError("PRISMA initial publication is incomplete")
    return pointer["version"]


def bootstrap_prisma(api, context, outputs, config, signer, storage, wallet, wallet_password, admin_password, reconciled,
                     *, deadline, wallet_dsn, validate_wallet, generate_password, ensure_folder):
    global _deadline
    _deadline = deadline
    import oracledb
    agent_api = api.__class__(context["region"], outputs["ai_data_platform_id"], signer, context["deployment_id"],
                             api_version="20260430", resource_segment="aiDataPlatforms")
    database_users(agent_api, wallet, wallet_password, admin_password, {**config, "region": context["region"]}, outputs,
                   wallet_dsn=wallet_dsn, validate_wallet=validate_wallet, generate_password=generate_password)
    bundle = runtime_archive()
    runtime = {"namespace": outputs["objectstorage_namespace"], "bucket": outputs["medallion_bucket_names"]["gold"], "workbench_base": api.base,
        "region": context["region"], "model_id": outputs["agent_model_id"], "compartment_id": outputs["compartment_ocid"], "catalog": reconciled["catalog_name"]}
    runtime.update(landing_bucket=outputs["medallion_bucket_names"]["landing"], landing_prefix="01_landing/prisma/raw/",
        landing_volume_path=f"/Volumes/{runtime['catalog']}/prisma_ingest/landing",
        checkpoint_volume_path=f"/Volumes/{runtime['catalog']}/prisma_ingest/checkpoints/bronze-v1")
    workspace = reconciled["workspace_key"]
    job = install_job(api, workspace, reconciled["shared_compute_key"], runtime, bundle, ensure_folder=ensure_folder)
    with tempfile.TemporaryDirectory(prefix="prisma-config-") as directory:
        with zipfile.ZipFile(io.BytesIO(wallet)) as archive:
            archive.extractall(directory)
        with oracledb.connect(user="ADMIN", password=admin_password, dsn=wallet_dsn(Path(directory)),
            config_dir=directory, wallet_location=directory, wallet_password=wallet_password) as connection:
            current = read_document(connection, "runtime")
            desired = {**runtime, "workspace_key": workspace, "job_key": job}
            if any(current.get(key) != value for key, value in desired.items()):
                write_document(connection, "runtime", desired, current["revision"])
            connection.commit()
    agent = publish_agent(agent_api, workspace, bundle, context["region"])
    # Materialize the otherwise empty external-volume prefix; Spark ignores this hidden non-event object.
    storage.put_object(runtime["namespace"], runtime["landing_bucket"], runtime["landing_prefix"] + ".keep",
                       b"", content_type="application/octet-stream")
    run_key = run_initial_job(api, workspace, job, hashlib.sha256(bundle).hexdigest())
    version = validate_publication(storage, runtime)
    storage.put_object(runtime["namespace"], runtime["bucket"], ".control/prisma/agent.json", json.dumps(agent).encode(), content_type="application/json")
    return {"prisma_job_ready": True, "prisma_agent_ready": True, "prisma_revision": agent["revision"],
            "prisma_acceptance_run": run_key, "prisma_snapshot_version": version, "external_volume_count": 1}
