"""Additive God’s Eye View bootstrap after the existing encrypted operator delivery."""
from __future__ import annotations

import base64
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
from uuid import uuid4

from gods_eye_sources import NOTEBOOK_ROOT, workflow_source
from gods_eye_agent_source import agent_source

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps/backend"))
from app.gods_eye_view.database import install_schema, read_document, write_document
from app.gods_eye_view.control_store import ObjectControlStore
from app.gods_eye_view.scheduling import RUN_FAILED, RUN_SUCCESS, TASK_RUN_QUERY, run_state, task_outcome
from app.gods_eye_view.runtime_secrets import CONTROL_CREDENTIAL_NAME, SHARED_OCI_CREDENTIAL_NAME, identity_hash, shared_credential

SESSION_RETENTION = {"retentionPeriodInDays": 7}
AGENT_NAME = "ai_gods_eye_view"
WORKSPACE_ROOT = "/Workspace/medallion/gods_eye_view"
AGENT_COMPUTE_NAME = "aidp_gods_eye_view_agent_compute"
RESOURCE_ALIASES = {
    AGENT_NAME: ("territorial_assistant",),
    AGENT_COMPUTE_NAME: ("territorial_agent_compute", "prisma_agent_compute"),
    "aidp_gods_eye_view_social_compute": ("social_stream_compute",),
    "aidp_gods_eye_view_sensor_compute": ("sensor_stream_compute",),
    "aidp_gods_eye_view_query_compute": ("territorial_query_compute",),
}
PIPELINE_REQUIREMENTS = "httpx==0.28.1\n"
_deadline = 0.0


def pause(seconds=5):
    if _deadline and time.monotonic() + seconds >= _deadline:
        raise RuntimeError("God’s Eye View bootstrap reached the shared post-apply deadline")
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
            raise RuntimeError("God’s Eye View asynchronous operation failed or returned an unknown state")
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


def named(api, path, name, params=None):
    names = {name, *RESOURCE_ALIASES.get(name, ())} if path.endswith(("/clusters", "/agents")) else {name}
    matches = [item for item in items(api, path, params) if (item.get("displayName") or item.get("name")) in names
               and (item.get("lifecycleState") or item.get("lifeCycleState") or item.get("state")) != "DELETED"]
    if len(matches) > 1:
        raise RuntimeError("Duplicate managed God’s Eye View resource")
    if matches:
        return matches[0]
    if path.endswith("/clusters"):
        return hidden_compute(api, path, name)
    return None


def hidden_compute(api, path, name):
    # AIDP can expose new AI Compute only by key before it appears in the cluster list.
    workspace_prefix = path.split("/")[2] + "."
    names = {name, *RESOURCE_ALIASES.get(name, ())}
    operations = [item for item in items(api, "/asyncOperations", {"resourceType": "AI_COMPUTE"})
                  if item.get("resourceDisplayName") in names
                  and item.get("actionType") in {"CREATE_CLUSTER", "DELETE_CLUSTER"}
                  and str(item.get("resourceName", "")).startswith(workspace_prefix)]
    if not operations:
        return None
    latest = max(operations, key=lambda item: str(item.get("timeStarted") or ""))
    if latest.get("actionType") == "DELETE_CLUSTER":
        if latest.get("status") in {"SUCCESS", "SUCCEEDED"}:
            return None
        raise RuntimeError("God’s Eye View managed compute is being deleted")
    key = latest["resourceName"][len(workspace_prefix):]
    return hidden_compute_detail(api, path, name, key, latest.get("status"))


def hidden_compute_detail(api, path, name, key, status):
    try:
        detail = api.request("GET", path + "/" + quote(key, safe="")).body
    except RuntimeError as exc:
        if getattr(exc, "status_code", None) != 404:
            raise
        if status in {"FAILED", "ERROR", "CANCELED", "CANCELLED"}:
            raise RuntimeError("God’s Eye View compute creation failed without a resource") from None
        return {"key": key, "displayName": name, "type": "AI_COMPUTE", "state": "CREATING"}
    names = {name, *RESOURCE_ALIASES.get(name, ())}
    if ((detail.get("displayName") or detail.get("name")) not in names
            or (detail.get("type") or detail.get("sourceApi")) != "AI_COMPUTE"):
        raise RuntimeError("God’s Eye View compute operation points to an incompatible resource")
    return detail


def ensure(api, path, name, payload, *, ready=False, params=None):
    current = named(api, path, name, params)
    if not current:
        if payload is None:
            raise RuntimeError("Managed God’s Eye View resource disappeared before readiness check")
        operation(api, api.request("POST", path, payload=payload))
    while True:
        current = named(api, path, name, params)
        if current:
            state = str(current.get("lifecycleState") or current.get("lifeCycleState") or current.get("state") or "").upper()
            if state in {"FAILED", "INACTIVE", "DELETED", "DELETING", "CANCELED", "CANCELLED"} or state.endswith("_FAILED"):
                raise RuntimeError("Managed God’s Eye View resource entered terminal state " + state)
            if not ready or state in {"ACTIVE", "STOPPED"}:
                return current
        elif payload is None:
            raise RuntimeError("Managed God’s Eye View resource disappeared before readiness check")
        pause()


def credential(api, name, values):
    payload = None if values is None else {"displayName": name, "type": "SECRET_TOKEN",
        "credentialDescription": "God’s Eye View managed runtime; never return secret values",
        "credentialDetails": {"credentialType": "SECRET_TOKEN", "secretTokenPair":
            [{"secretKey": key, "secretValue": value} for key, value in values.items()]}}
    current = ensure(api, "/credentials", name, payload, ready=True)
    if (current.get("type") or current.get("credentialType")) != "SECRET_TOKEN":
        raise RuntimeError("Managed God’s Eye View credential has an incompatible type")
    return current


def database_users(api, wallet, wallet_password, admin_password, config, outputs, *, wallet_dsn, validate_wallet, generate_password):
    import oracledb
    with tempfile.TemporaryDirectory(prefix="gods-eye-view-bootstrap-") as directory:
        with zipfile.ZipFile(io.BytesIO(validate_wallet(wallet))) as archive:
            archive.extractall(directory)
        dsn = wallet_dsn(Path(directory))
        with oracledb.connect(user="ADMIN", password=admin_password, dsn=dsn, config_dir=directory,
                             wallet_location=directory, wallet_password=wallet_password) as connection:
            install_schema(connection)
            cursor = connection.cursor()
            user, name = "PRISMA_WRITER", database_credential_name(api)
            if named(api, "/credentials", name):
                credential(api, name, None)
            else:
                generated_password = generate_password()
                cursor.execute("SELECT COUNT(*) FROM ALL_USERS WHERE USERNAME=:name", name=user)
                verb = "ALTER" if cursor.fetchone()[0] else "CREATE"
                cursor.execute(f'{verb} USER {user} IDENTIFIED BY "{generated_password}"')
                cursor.execute(f"GRANT CREATE SESSION TO {user}")
                cursor.execute(f"GRANT EXECUTE ON ADMIN.PRISMA_CONTROL TO {user}")
                values = {"db_user": user, "db_password": generated_password, "dsn": dsn,
                    "wallet": base64.b64encode(wallet).decode(), "wallet_password": wallet_password,
                    "region": config["region"], "compartment_id": outputs["compartment_ocid"], "model_id": outputs["agent_model_id"]}
                credential(api, name, values)
            connection.commit()


def ensure_oci_credential(api, config):
    """Share the OCI-only identity; never adopt a legacy combined database secret."""
    current = shared_credential(items(api, "/credentials"))
    if current is not None:
        return current
    identity_hash(config)
    values = {key: str(config[key]) for key in ("tenancy", "user", "fingerprint", "region")}
    values["private_key"] = Path(config["key_file"]).read_text(encoding="utf-8")
    if not values["private_key"].strip():
        raise RuntimeError("Shared OCI credential is incomplete")
    return credential(api, SHARED_OCI_CREDENTIAL_NAME, values)


def database_credential_name(api, *, reader=False):
    """Adopt legacy secrets without recreating them or rotating their database users."""
    role = "Reader" if reader else "Writer"
    names = (f"Territorial{role}Runtime", f"Prisma{role}Runtime")
    if not reader:
        names = (CONTROL_CREDENTIAL_NAME, *names)
    matches = [name for name in names if named(api, "/credentials", name)]
    if len(matches) > 1:
        raise RuntimeError("Ambiguous God’s Eye View database credentials; reconcile the existing identities")
    return matches[0] if matches else names[0]


def runtime_archive():
    buffer = io.BytesIO()
    names = ("__init__.py", "core.py", "correlation.py", "corpus.py", "media.py", "area.py", "x.py", "database.py", "control_store.py", "post_index.py", "runtime_secrets.py", "classification.py", "scheduling.py", "capture.py", "sensor_capture.py", "landing.py", "pipeline.py", "sensors.py", "sensor_pipeline.py", "sensor_reset.py", "synthetic_reset.py", "gold_reader.py", "agent.py")
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in names:
            info = zipfile.ZipInfo("gods_eye_view/" + name, date_time=(2026, 1, 1, 0, 0, 0))
            source = (ROOT / "apps/backend/app/gods_eye_view" / name).read_bytes()
            compile(source, name, "exec")
            archive.writestr(info, source)
    return buffer.getvalue()


def publish_runtime_sources(api, workspace, config, bundle, *, ensure_folder):
    sources = {
        "10_bronze/social_network.py": workflow_source("pipeline", config, bundle),
        "10_bronze/sensor_stream.py": workflow_source("sensor_pipeline", config, bundle),
    }
    for name, source in sources.items():
        compile(source, name, "exec")
    sources['README.md'] = (NOTEBOOK_ROOT / 'README.md').read_text(encoding="utf-8")
    sources['20_silver/README.md'] = (NOTEBOOK_ROOT / '20_silver/README.md').read_text(encoding="utf-8")
    sources['30_gold/README.md'] = (NOTEBOOK_ROOT / '30_gold/README.md').read_text(encoding="utf-8")
    manifest = json.dumps({"bundle_sha256": hashlib.sha256(bundle).hexdigest(),
        "files": {name: hashlib.sha256(source.encode("utf-8")).hexdigest() for name, source in sorted(sources.items())}},
        sort_keys=True, indent=2) + "\n"
    root = WORKSPACE_ROOT
    endpoint = f"/workspaces/{workspace}/notebook/api/contents/{quote(root + '/manifest.json', safe='')}"
    try:
        previous = api.request("GET", endpoint, params={"content": "1"}).body.get("content")
    except RuntimeError as exc:
        if getattr(exc, "status_code", None) != 404:
            raise
        previous = None
    changed = previous != manifest
    if not changed:
        for name, source in sources.items():
            path = root + "/" + name
            try:
                current = api.request("GET", f"/workspaces/{workspace}/notebook/api/contents/{quote(path, safe='')}", params={"content": "1"}).body.get("content")
            except RuntimeError as exc:
                if getattr(exc, "status_code", None) != 404:
                    raise
                current = None
            if current != source:
                changed = True
                break
    if changed:
        for job in items(api, f"/workspaces/{workspace}/jobs"):
            if job.get("name") not in {"wf_ai_gods_eye_view_social_network", "wf_ai_gods_eye_view_sensor_stream",
                    "territorial_social_network", "territorial_sensor_stream", "prisma_bogota_tick", "prisma_colombia_sensors"}:
                continue
            if not job.get("key"):
                raise RuntimeError("Managed God’s Eye View workflow identity is incomplete")
            detail = api.request("GET", f"/workspaces/{workspace}/jobs/{quote(str(job['key']), safe='')}").body
            if not any(task.get("filePath") in {root + "/10_bronze/social_network.py", root + "/10_bronze/sensor_stream.py"} for task in detail.get("tasks", [])):
                continue
            if any(run_state(run) not in RUN_SUCCESS | RUN_FAILED for run in
                    items(api, f"/workspaces/{workspace}/jobRuns", {"jobKey": job["key"], "limit": 100})):
                raise RuntimeError("Stop both managed God’s Eye View workflows before replacing shared sources")
    for path in ("/Workspace/medallion", root, *(root + "/" + layer for layer in ("10_bronze", "20_silver", "30_gold", "40_report"))):
        ensure_folder(api, workspace, path)
    for name, source in sorted(sources.items()):
        upload(api, workspace, root + "/" + name, source, replace=True)
    upload(api, workspace, root + "/manifest.json", manifest, replace=True)
    return root


def upload(api, workspace, path, content, kind="file", *, replace=False):
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
        if not replace or kind != "file":
            raise RuntimeError("God’s Eye View versioned workspace path already has different content")
    if kind == "notebook":
        parent = path.rsplit("/", 1)[0]
        created = api.request("POST", f"/workspaces/{workspace}/notebook/api/contents/{quote(parent, safe='')}",
                              payload={"copy_from": None, "ext": ".ipynb", "type": "notebook"}).body
        if not created.get("path"):
            raise RuntimeError("God’s Eye View notebook creation did not return a path")
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
        raise RuntimeError("God’s Eye View workspace content round-trip mismatch")


def install_volumes(api, config):
    catalog = named(api, "/catalogs", config["catalog"])
    if not catalog:
        raise RuntimeError("God’s Eye View requires its existing governed catalog")
    schema = ensure(api, "/schemas", "prisma_ingest", {"displayName": "prisma_ingest", "catalogName": config["catalog"]},
                    ready=True, params={"catalogKey": catalog["key"]})
    schema_detail = api.request("GET", "/schemas/" + quote(str(schema["key"]), safe="")).body
    if schema_detail.get("catalogName") != config["catalog"] or schema_detail.get("displayName") != "prisma_ingest":
        raise RuntimeError("God’s Eye View ingest schema does not match the governed catalog")
    for name, kind in (("landing", "EXTERNAL"), ("checkpoints", "MANAGED")):
        payload = {"displayName": name, "catalogName": config["catalog"], "schemaName": "prisma_ingest", "volumeType": kind}
        if kind == "EXTERNAL":
            payload["storageLocation"] = f"oci://{config['landing_bucket']}@{config['namespace']}/{config['landing_prefix']}"
        volume = ensure(api, "/volumes", name, payload, ready=True,
                        params={"catalogKey": catalog["key"], "schemaKey": schema["key"]})
        detail = api.request("GET", "/volumes/" + quote(str(volume["key"]), safe="")).body
        if any(detail.get(field) != value for field, value in payload.items()):
            raise RuntimeError("God’s Eye View volume type or location differs from its configured contract")


def cluster_job_assignments(api, base, compute):
    assignments = {}
    for job in items(api, base + "/jobs"):
        if not job.get("key"):
            raise RuntimeError("Cannot resolve a workspace job before cluster maintenance")
        body = api.request("GET", base + "/jobs/" + quote(str(job["key"]), safe="")).body
        clusters = body.get("jobClusters", []) + [task.get("cluster") or {} for task in body.get("tasks", [])]
        assignments[job["key"]] = {cluster["clusterKey"] for cluster in clusters if cluster.get("clusterKey")}
        if compute in assignments[job["key"]]:
            if any((body.get(kind) or {}).get("pauseStatus") == "UNPAUSED" for kind in ("schedule", "continuous")):
                raise RuntimeError("Pause jobs using the shared cluster before installing God’s Eye View libraries")
    return assignments


def cluster_idle(api, workspace, compute):
    base = f"/workspaces/{workspace}"
    detail = api.request("GET", base + "/clusters/" + quote(compute, safe="")).body
    if detail.get("attachedSessions") or detail.get("attachedNotebooks"):
        raise RuntimeError("God’s Eye View library installation requires a cluster without attached notebook sessions")
    assignments = cluster_job_assignments(api, base, compute)
    runs = items(api, base + "/jobRuns", {"sortBy": "timeCreated", "sortOrder": "DESC", "limit": 100})
    for run in runs:
        if run_state(run) not in RUN_SUCCESS | RUN_FAILED:
            assigned = assignments.get(run.get("jobKey"))
            if not assigned or compute in assigned:
                raise RuntimeError("God’s Eye View library installation cannot resolve or safely exclude an active run on this cluster")


def install_cluster_libraries(api, workspace, compute, *, ensure_folder):
    digest = hashlib.sha256(PIPELINE_REQUIREMENTS.encode()).hexdigest()[:12]
    base = f"/workspaces/{workspace}/clusters/{quote(compute, safe='')}"
    path = WORKSPACE_ROOT + "/dependencies_" + digest + "/requirements.txt"
    legacy = "/Workspace/medallon/prisma/dependencies_" + digest + "/requirements.txt"
    matches = [item for item in items(api, base + "/libraries")
               if item.get("path") in {path, legacy} and item.get("status") != "DELETED"]
    if len(matches) > 1:
        raise RuntimeError("Duplicate God’s Eye View cluster library")
    # Adopt the same installed requirement hash; do not attach a second library on rename.
    if matches:
        path = matches[0]["path"]
    directory = path.rsplit("/", 1)[0]
    ensure_folder(api, workspace, directory.rsplit("/", 1)[0])
    ensure_folder(api, workspace, directory)
    upload(api, workspace, path, PIPELINE_REQUIREMENTS)

    def status():
        matches = [item for item in items(api, base + "/libraries") if item.get("path") == path and item.get("status") != "DELETED"]
        if len(matches) > 1:
            raise RuntimeError("Duplicate God’s Eye View cluster library")
        state = matches[0].get("status") if matches else None
        if state in {"FAILED", "SKIPPED", "UNINSTALL_ON_RESTART"}:
            raise RuntimeError("God’s Eye View cluster library is not usable: " + state)
        return state

    current = status()
    if current == "INSTALLED":
        return path
    cluster_idle(api, workspace, compute)
    if current is None:
        operation(api, api.request("PATCH", base + "/libraries", payload={"items": [
            {"operation": "INSTALL", "type": "WORKSPACE_FILE", "path": path}]}))
    while status() not in {"INSTALLED", "INSTALL_ON_RESTART"}:
        pause()
    cluster_idle(api, workspace, compute)
    operation(api, api.request("POST", base + "/actions/restart", payload={}))
    while True:
        state = api.request("GET", base).body.get("state")
        if state in {"FAILED", "DELETED", "DELETING"} or str(state).endswith("_FAILED"):
            raise RuntimeError("God’s Eye View cluster failed while installing libraries")
        if state == "ACTIVE" and status() == "INSTALLED":
            return path
        pause()


def install_stream_compute(api, workspace, name, key=""):
    """Dedicated small USER cluster; omission of autoTerminationMinutes keeps it always on."""
    from app.aidp import AidpClient
    name = next((canonical for canonical, aliases in RESOURCE_ALIASES.items() if name in aliases), name)
    if name not in {"aidp_gods_eye_view_social_compute", "aidp_gods_eye_view_sensor_compute", "aidp_gods_eye_view_query_compute"}:
        raise ValueError("Invalid dedicated streaming compute")
    payload = {"type": "USER", "displayName": name,
        "description": "God's Eye View Gold queries" if name == "aidp_gods_eye_view_query_compute" else "God's Eye View permanent streaming workflow",
        "driverConfig": {"driverShape": "amd.generic", "driverShapeConfig": {"ocpus": 2, "memoryInGBs": 16}},
        "workerConfig": {"workerShape": "amd.generic", "workerShapeConfig": {"ocpus": 2, "memoryInGBs": 16},
                         "minWorkerCount": 1, "maxWorkerCount": 1},
        "clusterRuntimeConfig": {"type": "SPARK", "sparkVersion": "3.5.0",
            "sparkAdvancedConfigurations": {"spark.aidp.lineage.enabled": "true"}, "sparkEnvVariables": {}, "initScripts": []}}
    path = f"/workspaces/{workspace}/clusters"
    if key != "":
        if not isinstance(key, str) or not key.strip():
            raise RuntimeError("Dedicated compute identity is invalid")
        resource = api.request("GET", path + "/" + quote(key, safe="")).body
        if resource.get("key") != key:
            raise RuntimeError("Dedicated compute identity differs")
    else:
        resource = ensure(api, path, name, payload, ready=True)
    path += "/" + quote(str(resource["key"]), safe="")
    expected = {key: value for key, value in payload.items() if key not in {"description", "displayName"}}
    started = False
    while True:
        response = api.request("GET", path)
        detail = response.body
        if ((detail.get("displayName") or detail.get("name")) not in {name, *RESOURCE_ALIASES[name]}
                or not AidpClient._notebook_matches(detail, expected) or detail.get("autoTerminationMinutes") not in (None, 0)):
            raise RuntimeError("Dedicated streaming compute differs from its always-on fixed-size contract")
        state = str(detail.get("state") or detail.get("lifecycleState") or "").upper()
        if state == "ACTIVE":
            return str(resource["key"])
        if state not in {"STOPPED", "STARTING"}:
            raise RuntimeError("Dedicated streaming compute failed to become active: " + state)
        if state == "STOPPED" and not started:
            etag = response.headers.get("etag") or response.headers.get("ETag")
            headers = {"opc-retry-token": uuid4().hex, **({"If-Match": etag} if etag else {})}
            operation(api, api.request("POST", path + "/actions/start", payload={}, headers=headers))
            started = True
        pause()


def managed_workflow(api, workspace, name, legacy_name, key=None):
    base = f"/workspaces/{workspace}/jobs"
    names = {name, *(legacy_name if isinstance(legacy_name, tuple) else (legacy_name,))}
    matches = [job for job in items(api, base) if job.get("name") in names
               and (job.get("lifecycleState") or job.get("state")) != "DELETED"]
    if len(matches) > 1 or (key and matches and matches[0].get("key") != key):
        raise RuntimeError("Duplicate or mismatched managed God’s Eye View workflow")
    if key:
        current = api.request("GET", base + "/" + quote(key, safe="")).body
        if (current.get("name") not in names or current.get("key") != key
                or (current.get("lifecycleState") or current.get("state")) in {"DELETING", "DELETED"}):
            raise RuntimeError("Configured God’s Eye View workflow identity does not match")
        return current
    if matches and (matches[0].get("lifecycleState") or matches[0].get("state")) == "DELETING":
        raise RuntimeError("Managed God’s Eye View workflow is being deleted")
    return matches[0] if matches else None


def install_job(api, workspace, compute, config, bundle, *, ensure_folder, workflow="social"):
    from app.aidp import AidpClient
    if config.get("streaming_mode", "finite") not in {"finite", "persistent"}:
        raise ValueError("Invalid God’s Eye View streaming mode")
    persistent = config.get("streaming_mode") == "persistent"
    if workflow not in {"social", "sensors"} or (workflow == "sensors" and not persistent):
        raise ValueError("Invalid God’s Eye View workflow")
    task = "sensor_stream" if workflow == "sensors" else "social_network"
    name = "wf_ai_gods_eye_view_" + task
    legacy_name = ("territorial_" + task, "prisma_colombia_sensors" if workflow == "sensors" else "prisma_bogota_tick")
    current = managed_workflow(api, workspace, name, legacy_name, config.get("sensor_job_key" if workflow == "sensors" else "job_key"))
    root = publish_runtime_sources(api, workspace, config, bundle, ensure_folder=ensure_folder)
    path = root + "/10_bronze/" + task + ".py"
    payload = {"name": name, "path": WORKSPACE_ROOT, "description": "God's Eye View collection and publication tick",
        "maxConcurrentRuns": 1, "queue": {"isEnabled": False}, "timeoutSeconds": 600,
        "schedule": {"quartzCronExpression": "0 * * * * ?", "timezoneId": "UTC", "pauseStatus": "PAUSED"},
        "jobClusters": [{"clusterKey": compute}], "tasks": [{"type": "PYTHON_TASK", "taskKey": task,
        "dependsOn": [], "runIf": "ALL_SUCCESS", "maxRetries": 0, "isRetryOnTimeout": False,
        # AIDP assigns this string to sys.argv; it expects a list literal, not shell arguments.
        "source": "WORKSPACE", "filePath": path, "commandLineArguments": json.dumps([path]),
        "cluster": {"clusterKey": compute}}]}
    payload["tasks"][0]["isStreaming"] = persistent
    if persistent:
        payload.update(description="God's Eye View persistent " + ("sensor TXT ingestion" if workflow == "sensors" else "social ingestion and publication"))
        payload.pop("timeoutSeconds")
        payload["tasks"][0].pop("maxRetries")
        payload["tasks"][0].pop("isRetryOnTimeout")
    jobs_path = f"/workspaces/{workspace}/jobs"
    job = current or ensure(api, jobs_path, payload["name"], {
        field: payload[field] for field in ("name", "path", "description", "maxConcurrentRuns")
    })
    key = str(job["key"])
    detail = api.request("GET", f"/workspaces/{workspace}/jobs/{key}")
    # Preserve a live operator's schedule when updating only the versioned source.
    payload["schedule"] = detail.body.get("schedule") or payload["schedule"]
    if persistent:
        payload["schedule"] = {**payload["schedule"], "pauseStatus": "PAUSED"}
    def matches(body):
        return (all(AidpClient._notebook_matches(body.get(field), value) for field, value in payload.items() if field != "tasks")
                and (not persistent or body.get("timeoutSeconds") in (None, 0))
                and AidpClient._job_tasks_match(body.get("tasks"), payload["tasks"], compute)
                and all(bool(task.get("isStreaming")) == persistent for task in body.get("tasks", [])))
    if not matches(detail.body):
        active = [run for run in items(api, f"/workspaces/{workspace}/jobRuns", {"jobKey": key, "limit": 100})
                  if run.get("jobKey") == key and run_state(run) not in RUN_SUCCESS | RUN_FAILED]
        if active:
            raise RuntimeError("Stop the managed God’s Eye View workflow before upgrading its source or compute")
        operation(api, api.request("PUT", f"/workspaces/{workspace}/jobs/{key}", payload=payload,
                                  headers={"If-Match": detail.headers["etag"]} if detail.headers.get("etag") else None))
        if not matches(api.request("GET", f"/workspaces/{workspace}/jobs/{key}").body):
            raise RuntimeError("God’s Eye View job definition did not round-trip")
    return key


def agent_deployments(api, base):
    deployments = [item for item in items(api, base) if (item.get("lifecycleState") or item.get("state")) != "DELETED"]
    if len(deployments) > 1:
        raise RuntimeError("Duplicate God’s Eye View agent deployments")
    return deployments


def deployment_endpoint(detail, region):
    endpoint = str(detail.get("endpointUrl") or "").rstrip("/")
    if not endpoint.endswith("/chat"):
        endpoint += "/chat"
    url = urlsplit(endpoint)
    if (url.scheme != "https" or url.netloc != f"gateway.aidp.{region}.oci.oraclecloud.com"
        or url.query or url.fragment or not re.fullmatch(r"/agentendpoint/[A-Za-z0-9_.-]+/chat", url.path)):
        raise RuntimeError("God’s Eye View agent endpoint did not round-trip")
    retention = {key: value for key, value in detail.get("sessionRetentionConfig", {}).items() if value is not None}
    if retention != SESSION_RETENTION:
        raise RuntimeError("God’s Eye View agent retention did not round-trip")
    return endpoint


def wait_agent_deployment(api, base, region):
    while True:
        for item in agent_deployments(api, base):
            detail = api.request("GET", base + "/" + quote(str(item["key"]), safe="")).body
            state = str(detail.get("lifecycleState") or detail.get("state") or "").upper()
            if state in {"FAILED", "INACTIVE", "DELETING", "DELETED"} or state.endswith("_FAILED"):
                raise RuntimeError("God’s Eye View agent deployment failed; prior deployment preserved")
            if state == "ACTIVE":
                return {"deployment_key": item["key"], "endpoint": deployment_endpoint(detail, region)}
        pause(10)


def install_agent_compute(api, workspace, key=""):
    path = f"/workspaces/{workspace}/clusters"
    if key != "":
        if not isinstance(key, str) or not key.strip():
            raise RuntimeError("God’s Eye View agent compute identity is invalid")
        compute = api.request("GET", path + "/" + quote(key, safe="")).body
        if compute.get("key") != key:
            raise RuntimeError("God’s Eye View agent compute identity differs")
    else:
        compute = ensure(api, path, AGENT_COMPUTE_NAME, {
            "type": "AI_COMPUTE", "displayName": AGENT_COMPUTE_NAME, "description": "God's Eye View conversational agent",
            "driverConfig": {"driverShapeConfig": {"ocpus": 1, "memoryInGBs": 16}},
            "replicaConfig": {"minReplica": 1, "maxReplica": 1}}, ready=True)
    if ((compute.get("type") or compute.get("sourceApi")) != "AI_COMPUTE"
            or (compute.get("displayName") or compute.get("name")) not in {AGENT_COMPUTE_NAME, *RESOURCE_ALIASES[AGENT_COMPUTE_NAME]}
            or (compute.get("lifecycleState") or compute.get("state")) not in {"ACTIVE", "STOPPED"}):
        raise RuntimeError("Existing God’s Eye View agent compute is incompatible or not ready")
    return compute


def publish_agent(api, workspace, bundle, region, runtime=None):
    from app.aidp import AidpClient
    root = WORKSPACE_ROOT
    fields = ("region", "model_id", "compartment_id", "oci_credential_name", "oci_identity_sha256", "catalog", "gold_query_compute_id")
    if not runtime or any(not isinstance(runtime.get(key), str) or not runtime[key] for key in fields):
        raise RuntimeError("God’s Eye View Gold agent configuration incomplete")
    agent_config = {key: runtime[key] for key in fields}
    manifest = api.request("GET", f"/workspaces/{workspace}/notebook/api/contents/{quote(root + '/manifest.json', safe='')}", params={"content": "1"}).body["content"]
    if json.loads(manifest).get("bundle_sha256") != hashlib.sha256(bundle).hexdigest():
        raise RuntimeError("Publish the matching God’s Eye View runtime before its agent")
    entry, dependencies = root + "/40_report/ai_gods_eye_view.py", root + "/40_report/requirements.txt"
    source = agent_source(agent_config, bundle)
    compile(source, entry, "exec")
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:12]
    upload(api, workspace, dependencies, "# Dependencies are provided by native AIDP AI Compute.\n", replace=True)
    upload(api, workspace, entry, source, replace=True)
    compute = install_agent_compute(api, workspace, runtime.get("agent_compute_id", ""))
    name = AGENT_NAME
    payload = {
        "displayName": name, "description": "God's Eye View evidence-grounded assistant " + digest, "type": "CODE", "pathInfo": "/Workspace",
        "entryFilePath": entry, "dependenciesFilePath": dependencies, "computeKey": compute["key"],
        "sessionConfig": {"variables": {}, "sessionRetentionConfig": SESSION_RETENTION}}
    if "agent_id" in runtime:
        key = runtime["agent_id"]
        if not isinstance(key, str) or not key.strip():
            raise RuntimeError("Configured God's Eye View agent identity is invalid")
        agent = api.request("GET", f"/workspaces/{workspace}/agents/{quote(key, safe='')}").body
        if (agent.get("key") != key or agent.get("displayName") not in {name, *RESOURCE_ALIASES[name]}
                or (agent.get("lifecycleState") or agent.get("state")) in {"DELETED", "DELETING"}):
            raise RuntimeError("Configured God's Eye View agent identity differs")
    else:
        agent = ensure(api, f"/workspaces/{workspace}/agents", name, payload)
    agent_path = f"/workspaces/{workspace}/agents/{quote(str(agent['key']), safe='')}"
    response = api.request("GET", agent_path)
    if any(not AidpClient._notebook_matches(response.body.get(field), value) for field, value in payload.items()):
        operation(api, api.request("PUT", agent_path, payload={key: value for key, value in payload.items() if key not in {"type", "pathInfo"}},
            headers={"If-Match": response.headers["etag"]} if response.headers.get("etag") else None))
    detail = api.request("GET", agent_path).body
    if any(not AidpClient._notebook_matches(detail.get(field), value) for field, value in payload.items()):
        raise RuntimeError("God’s Eye View agent definition did not round-trip")
    base = agent_path + "/deployments"
    deployments = agent_deployments(api, base)
    deployment = {"displayName": name + "_deployment", "description": "God's Eye View deployment " + digest,
        "agentKey": agent["key"], "agentComputeKey": compute["key"]}
    if not deployments:
        operation(api, api.request("POST", base + "/actions/deploy", payload={**deployment, "sessionRetentionConfig": SESSION_RETENTION}))
    else:
        current = api.request("GET", base + "/" + quote(str(deployments[0]["key"]), safe=""))
        if current.body.get("description") != deployment["description"]:
            if (current.body.get("lifecycleState") or current.body.get("state")) != "ACTIVE":
                raise RuntimeError("God’s Eye View agent deployment is not ready for an update")
            operation(api, api.request("POST", base + "/actions/redeploy", payload=deployment,
                headers={"opc-retry-token": hashlib.sha256(f"{agent['key']}:{digest}".encode()).hexdigest(),
                    **({"If-Match": current.headers["etag"]} if current.headers.get("etag") else {})}))
    return {"state": "ACTIVE", "agent_key": agent["key"], "revision": digest,
            **wait_agent_deployment(api, base, region)}


def run_initial_job(api, workspace, job, revision):
    base = f"/workspaces/{workspace}/jobRuns"
    detail = api.request("GET", f"/workspaces/{workspace}/jobs/{quote(job, safe='')}").body
    if any(task.get("isStreaming") for task in detail.get("tasks", [])):
        raise RuntimeError("Initial acceptance requires the finite job; persistent task readiness is a separate check")
    tasks = detail.get("tasks", [])
    task = tasks[0] if len(tasks) == 1 else {}
    path = task.get("filePath", "") if task.get("type") == "PYTHON_TASK" else task.get("notebookPath", "")
    try:
        arguments = json.loads(task.get("commandLineArguments") or "[]")
    except (ValueError, TypeError):
        arguments = []
    python = (task.get("type") == "PYTHON_TASK" and task.get("taskKey") == "social_network" and task.get("source") == "WORKSPACE"
              and path == WORKSPACE_ROOT + "/10_bronze/social_network.py" and arguments == [path])
    legacy = task.get("type") == "NOTEBOOK_TASK" and re.fullmatch(r"/Workspace/medallon/prisma/prisma_tick_[a-f0-9]{12}\.ipynb", path)
    if not python and not legacy:
        raise RuntimeError("God’s Eye View initial job must reference one managed source")
    source_revision = revision
    if python:
        source = api.request("GET", f"/workspaces/{workspace}/notebook/api/contents/{quote(path, safe='')}", params={"content": "1"}).body.get("content")
        if not isinstance(source, str) or not source.strip():
            raise RuntimeError("Managed workflow source is missing")
        source_revision = hashlib.sha256(source.encode("utf-8")).hexdigest()
    token = hashlib.sha256(f"{api.deployment_id}:territorial:{job}:{revision}:{source_revision}:{path}:{arguments}".encode()).hexdigest()
    response = api.request("POST", base, payload={"jobKey": job, "parameters": []},
                           headers={"opc-retry-token": token})
    operation(api, response)
    key = str((response.body or {}).get("key") or "")
    if not key:
        raise RuntimeError("God’s Eye View initial job run did not return a key")

    while True:
        pause(0)
        state = run_state(api.request("GET", base + "/" + quote(key, safe="")).body)
        if state in RUN_SUCCESS:
            tasks = items(api, f"/workspaces/{workspace}/taskRuns", {
                "jobRunKey": key, **TASK_RUN_QUERY,
            })
            outcome = task_outcome(tasks)
            if outcome == "SUCCESS":
                return key
            if outcome == "FAILED":
                raise RuntimeError("God’s Eye View initial task failed; no readiness claimed")
        elif state in RUN_FAILED:
            raise RuntimeError("God’s Eye View initial job failed; no readiness claimed")
        pause(10)


def validate_publication(storage, runtime):
    def read(key):
        response = storage.get_object(runtime["namespace"], runtime["bucket"], key)
        content = response.data.content
        if len(content) > 10 * 1024 * 1024:
            raise RuntimeError("God’s Eye View publication exceeds its bounded bootstrap check")
        return json.loads(content)
    pointer = read("04_gold/prisma/current.json")
    key = str(pointer.get("snapshot_key") or "")
    if not key.startswith("04_gold/prisma/snapshots/") or ".." in key or not pointer.get("version"):
        raise RuntimeError("God’s Eye View initial publication pointer is invalid")
    snapshot = read(key)
    if snapshot.get("version") != pointer["version"]:
        raise RuntimeError("God’s Eye View initial publication is incomplete")
    return pointer["version"]


def start_stream_job(api, workspace, job, task_key):
    """Admit one permanent workflow; an existing active execution is reused."""
    base = f"/workspaces/{workspace}"
    detail = api.request("GET", base + "/jobs/" + quote(job, safe="")).body
    tasks = detail.get("tasks", [])
    if len(tasks) != 1 or tasks[0].get("taskKey") != task_key or tasks[0].get("isStreaming") is not True:
        raise RuntimeError("Permanent workflow definition is incomplete")
    active = [run for run in items(api, base + "/jobRuns", {"jobKey": job, "limit": 100})
              if run.get("jobKey") == job and run_state(run) not in RUN_SUCCESS | RUN_FAILED]
    if len(active) > 1:
        raise RuntimeError("Permanent workflow has multiple active runs")
    if active:
        return str(active[0]["key"])
    response = api.request("POST", base + "/jobRuns",
        payload={"jobKey": job, "parameters": [], "queue": {"isEnabled": False}}, headers={"opc-retry-token": uuid4().hex})
    operation(api, response)
    key = str((response.body or {}).get("key") or "")
    if not key:
        raise RuntimeError("Permanent workflow did not return its run identity")
    return key


def wait_stream_jobs(api, storage, runtime, runs, connection):
    """Acceptance is RUNNING plus current code heartbeats and coherent publication, never terminal SUCCESS."""
    base = f"/workspaces/{runtime['workspace_key']}"
    while True:
        ready = True
        for key, task in runs:
            state = run_state(api.request("GET", base + "/jobRuns/" + quote(key, safe="")).body)
            tasks = items(api, base + "/taskRuns", {"jobRunKey": key, **TASK_RUN_QUERY})
            if state in RUN_SUCCESS | RUN_FAILED or any(item.get("taskKey") != task or run_state(item) in RUN_FAILED for item in tasks):
                raise RuntimeError("Permanent workflow stopped or failed before readiness")
            ready = ready and state == "RUNNING" and bool(tasks) and all(run_state(item) == "RUNNING" for item in tasks)
        social = read_document(connection, "status_pipeline")
        sensors = read_document(connection, "status_sensorstream")
        current = all(item.get("pipeline_revision") == runtime["pipeline_revision"] for item in (social, sensors))
        if ready and current and sensors.get("status") == "running" and social.get("status") in {"ready", "pending", "needs_attention"}:
            version = validate_publication(storage, runtime)
            if social.get("version") == version:
                return version
        pause(10)


def initialize_controls(api, agent_api, storage, runtime, workspace):
    """Create controls only when the managed deployment has no prior resources or data."""
    connection = ObjectControlStore(storage, runtime["namespace"], runtime["bucket"])
    current = read_document(connection, "runtime")
    if current != {"revision": 0}:
        connection.require_ready()
        return connection
    job_names = {"wf_ai_gods_eye_view_social_network", "wf_ai_gods_eye_view_sensor_stream", "territorial_social_network",
                 "territorial_sensor_stream", "prisma_bogota_tick", "prisma_colombia_sensors"}
    agent_names = {AGENT_NAME, *RESOURCE_ALIASES.get(AGENT_NAME, ())}
    if (any(item.get("name") in job_names for item in items(api, f"/workspaces/{workspace}/jobs"))
            or any(item.get("displayName") in agent_names or str(item.get("displayName", "")).startswith("prisma_bogota")
                   for item in items(agent_api, "/agents"))
            or any(item.get("displayName") in {"AidpControlStore", "TerritorialWriterRuntime", "PrismaWriterRuntime", "PrismaReaderRuntime"}
                   for item in items(agent_api, "/credentials"))):
        raise RuntimeError("God’s Eye View existing controls require a verified Object Storage migration")
    for bucket, prefix in ((runtime["bucket"], ".control/"), (runtime["bucket"], "04_gold/prisma/"),
                           (runtime["landing_bucket"], runtime["landing_prefix"])):
        result = storage.list_objects(runtime["namespace"], bucket, prefix=prefix, limit=1).data
        if result.objects or result.next_start_with:
            raise RuntimeError("God’s Eye View existing data requires a verified Object Storage migration")
    write_document(connection, "runtime", {**runtime, "control_new_install": True}, 0)
    return connection


def bootstrap_gods_eye_view(api, context, outputs, config, signer, storage, wallet, wallet_password, admin_password, reconciled,
                     *, deadline, wallet_dsn, validate_wallet, generate_password, ensure_folder):
    global _deadline
    _deadline = deadline
    agent_api = api.__class__(context["region"], outputs["ai_data_platform_id"], signer, context["deployment_id"],
                             api_version="20260430", resource_segment="aiDataPlatforms")
    bundle = runtime_archive()
    runtime = {"namespace": outputs["objectstorage_namespace"], "bucket": outputs["medallion_bucket_names"]["gold"], "workbench_base": api.base,
        "region": context["region"], "model_id": outputs["agent_model_id"], "compartment_id": outputs["compartment_ocid"], "catalog": reconciled["catalog_name"],
        "streaming_mode": "persistent", "pipeline_revision": hashlib.sha256(bundle).hexdigest(), "analytics_store": "gold",
        "oci_identity_sha256": identity_hash(config)}
    runtime.update(landing_bucket=outputs["medallion_bucket_names"]["landing"], landing_prefix="01_landing/prisma/raw/",
        landing_volume_path=f"/Volumes/{runtime['catalog']}/prisma_ingest/landing",
        checkpoint_volume_path=f"/Volumes/{runtime['catalog']}/prisma_ingest/checkpoints/bronze-v1",
        sensor_landing_prefix="01_landing/prisma/raw/sensors/",
        sensor_landing_volume_path=f"/Volumes/{runtime['catalog']}/prisma_ingest/landing/sensors",
        sensor_checkpoint_volume_path=f"/Volumes/{runtime['catalog']}/prisma_ingest/checkpoints/sensors-v1")
    workspace = reconciled["workspace_key"]
    connection = initialize_controls(api, agent_api, storage, runtime, workspace)
    oci_credential = ensure_oci_credential(agent_api, {**config, "region": context["region"]})
    runtime["oci_credential_name"] = oci_credential["displayName"]
    install_volumes(agent_api, runtime)
    social_compute = install_stream_compute(api, workspace, "aidp_gods_eye_view_social_compute")
    sensor_compute = install_stream_compute(api, workspace, "aidp_gods_eye_view_sensor_compute")
    runtime["gold_query_compute_id"] = install_stream_compute(api, workspace, "aidp_gods_eye_view_query_compute")
    runtime["agent_compute_id"] = install_agent_compute(agent_api, workspace)["key"]
    for compute in (social_compute, sensor_compute):
        install_cluster_libraries(agent_api, workspace, compute, ensure_folder=ensure_folder)
    job = install_job(api, workspace, social_compute, runtime, bundle, ensure_folder=ensure_folder)
    sensor_job = install_job(api, workspace, sensor_compute, runtime, bundle, ensure_folder=ensure_folder, workflow="sensors")
    runtime.update(workspace_key=workspace, job_key=job, sensor_job_key=sensor_job,
                   social_compute_key=social_compute, sensor_compute_key=sensor_compute)
    agent = publish_agent(agent_api, workspace, bundle, context["region"], runtime)
    runtime["agent_id"] = agent["agent_key"]
    # Materialize empty governed roots; Spark ignores these hidden non-event objects.
    for prefix in (runtime["landing_prefix"], runtime["sensor_landing_prefix"]):
        storage.put_object(runtime["namespace"], runtime["landing_bucket"], prefix + ".keep", b"", content_type="application/octet-stream")
    current = read_document(connection, "runtime")
    desired = {key: value for key, value in {**current, **runtime}.items()
               if key not in {"writer_credential_name", "reader_credential_name"}}
    if current != desired:
        write_document(connection, "runtime", desired, current["revision"])
    sensor_run = start_stream_job(api, workspace, sensor_job, "sensor_stream")
    run_key = start_stream_job(api, workspace, job, "social_network")
    version = wait_stream_jobs(api, storage, runtime, [(run_key, "social_network"), (sensor_run, "sensor_stream")], connection)
    storage.put_object(runtime["namespace"], runtime["bucket"], ".control/prisma/agent.json", json.dumps(agent).encode(), content_type="application/json")
    result = {"job_ready": True, "agent_ready": True, "revision": agent["revision"],
              "acceptance_run": run_key, "sensor_run": sensor_run, "snapshot_version": version}
    return {**{"gods_eye_view_" + key: value for key, value in result.items()},
            **{"territorial_" + key: value for key, value in result.items()},
            **{"prisma_" + key: value for key, value in result.items()}, "external_volume_count": 1}
