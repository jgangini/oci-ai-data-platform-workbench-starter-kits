"""Additive Territorial bootstrap after the existing encrypted operator delivery."""
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
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps/backend"))
from app.territorial.database import install_schema, read_document, write_document
from app.territorial.scheduling import RUN_FAILED, RUN_SUCCESS, TASK_RUN_QUERY, run_state, task_outcome
from app.territorial.runtime_secrets import identity_hash, shared_credential

SESSION_RETENTION = {"retentionPeriodInDays": 7}
PIPELINE_REQUIREMENTS = "httpx==0.28.1\noracledb==3.4.2\n"
_deadline = 0.0


def pause(seconds=5):
    if _deadline and time.monotonic() + seconds >= _deadline:
        raise RuntimeError("Territorial bootstrap reached the shared post-apply deadline")
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
            raise RuntimeError("Territorial asynchronous operation failed or returned an unknown state")
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
    matches = [item for item in items(api, path, params) if (item.get("displayName") or item.get("name")) == name
               and (item.get("lifecycleState") or item.get("lifeCycleState") or item.get("state")) != "DELETED"]
    if len(matches) > 1:
        raise RuntimeError("Duplicate managed Territorial resource")
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
        raise RuntimeError("Territorial managed compute is being deleted")
    key = latest["resourceName"][len(workspace_prefix):]
    return hidden_compute_detail(api, path, name, key, latest.get("status"))


def hidden_compute_detail(api, path, name, key, status):
    try:
        detail = api.request("GET", path + "/" + quote(key, safe="")).body
    except RuntimeError as exc:
        if getattr(exc, "status_code", None) != 404:
            raise
        if status in {"FAILED", "ERROR", "CANCELED", "CANCELLED"}:
            raise RuntimeError("Territorial compute creation failed without a resource") from None
        return {"key": key, "displayName": name, "type": "AI_COMPUTE", "state": "CREATING"}
    if ((detail.get("displayName") or detail.get("name")) != name
            or (detail.get("type") or detail.get("sourceApi")) != "AI_COMPUTE"):
        raise RuntimeError("Territorial compute operation points to an incompatible resource")
    return detail


def ensure(api, path, name, payload, *, ready=False, params=None):
    current = named(api, path, name, params)
    if not current:
        if payload is None:
            raise RuntimeError("Managed Territorial resource disappeared before readiness check")
        operation(api, api.request("POST", path, payload=payload))
    while True:
        current = named(api, path, name, params)
        if current:
            state = str(current.get("lifecycleState") or current.get("lifeCycleState") or current.get("state") or "").upper()
            if state in {"FAILED", "INACTIVE", "DELETED", "DELETING", "CANCELED", "CANCELLED"} or state.endswith("_FAILED"):
                raise RuntimeError("Managed Territorial resource entered terminal state " + state)
            if not ready or state in {"ACTIVE", "STOPPED"}:
                return current
        elif payload is None:
            raise RuntimeError("Managed Territorial resource disappeared before readiness check")
        pause()


def credential(api, name, values):
    payload = None if values is None else {"displayName": name, "type": "SECRET_TOKEN",
        "credentialDescription": "Territorial managed runtime; never return secret values",
        "credentialDetails": {"credentialType": "SECRET_TOKEN", "secretTokenPair":
            [{"secretKey": key, "secretValue": value} for key, value in values.items()]}}
    current = ensure(api, "/credentials", name, payload, ready=True)
    if (current.get("type") or current.get("credentialType")) != "SECRET_TOKEN":
        raise RuntimeError("Managed Territorial credential has an incompatible type")
    return current


def database_users(api, wallet, wallet_password, admin_password, config, outputs, *, wallet_dsn, validate_wallet, generate_password):
    import oracledb
    oci_credential = shared_credential(items(api, "/credentials"))
    with tempfile.TemporaryDirectory(prefix="territorial-bootstrap-") as directory:
        with zipfile.ZipFile(io.BytesIO(validate_wallet(wallet))) as archive:
            archive.extractall(directory)
        dsn = wallet_dsn(Path(directory))
        with oracledb.connect(user="ADMIN", password=admin_password, dsn=dsn, config_dir=directory,
                             wallet_location=directory, wallet_password=wallet_password) as connection:
            install_schema(connection)
            cursor = connection.cursor()
            for user, reader in (("PRISMA_WRITER", False), ("PRISMA_READER", True)):
                name = database_credential_name(api, reader=reader)
                if named(api, "/credentials", name):
                    credential(api, name, None)
                    if reader:
                        for view in ("PRISMA_V_SOCIAL_POSTS", "PRISMA_V_SENSOR_EVENTS"):
                            cursor.execute(f"GRANT SELECT ON ADMIN.{view} TO {user}")
                    continue
                generated_password = generate_password()
                cursor.execute("SELECT COUNT(*) FROM ALL_USERS WHERE USERNAME=:name", name=user)
                verb = "ALTER" if cursor.fetchone()[0] else "CREATE"
                cursor.execute(f'{verb} USER {user} IDENTIFIED BY "{generated_password}"')
                cursor.execute(f"GRANT CREATE SESSION TO {user}")
                if reader:
                    for view in ("PRISMA_V_SNAPSHOTS", "PRISMA_V_INCIDENTS", "PRISMA_V_EVIDENCE", "PRISMA_V_SOCIAL_POSTS", "PRISMA_V_SENSOR_EVENTS"):
                        cursor.execute(f"GRANT SELECT ON ADMIN.{view} TO {user}")
                else:
                    cursor.execute(f"GRANT EXECUTE ON ADMIN.PRISMA_CONTROL TO {user}")
                values = {"db_user": user, "db_password": generated_password, "dsn": dsn,
                    "wallet": base64.b64encode(wallet).decode(), "wallet_password": wallet_password,
                    "region": config["region"], "compartment_id": outputs["compartment_ocid"], "model_id": outputs["agent_model_id"]}
                if not reader and oci_credential is None:
                    values.update({key: str(config[key]) for key in ("tenancy", "user", "fingerprint")})
                    values["private_key"] = Path(config["key_file"]).read_text(encoding="utf-8")
                credential(api, name, values)
            connection.commit()


def database_credential_name(api, *, reader=False):
    """Adopt legacy secrets without recreating them or rotating their database users."""
    role = "Reader" if reader else "Writer"
    canonical, legacy = f"Territorial{role}Runtime", f"Prisma{role}Runtime"
    matches = [name for name in (canonical, legacy) if named(api, "/credentials", name)]
    if len(matches) > 1:
        raise RuntimeError("Ambiguous Territorial database credentials; reconcile the existing identities")
    return matches[0] if matches else canonical


def runtime_archive():
    buffer = io.BytesIO()
    names = ("__init__.py", "core.py", "correlation.py", "corpus.py", "media.py", "area.py", "x.py", "database.py", "runtime_secrets.py", "classification.py", "scheduling.py", "capture.py", "sensor_capture.py", "landing.py", "pipeline.py", "sensors.py", "sensor_pipeline.py", "sensor_reset.py", "synthetic_reset.py", "agent.py")
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in names:
            info = zipfile.ZipInfo("territorial/" + name, date_time=(2026, 1, 1, 0, 0, 0))
            source = (ROOT / "apps/backend/app/territorial" / name).read_bytes()
            ast.parse(source, filename=name)
            archive.writestr(info, source)
        shim = (ROOT / "apps/backend/app/prisma/__init__.py").read_bytes()
        archive.writestr(zipfile.ZipInfo("prisma/__init__.py", date_time=(2026, 1, 1, 0, 0, 0)), shim)
    return buffer.getvalue()


def bundle_prelude(bundle):
    encoded = base64.b64encode(bundle).decode()
    digest = hashlib.sha256(bundle).hexdigest()
    return f'''import base64, hashlib, os, sys, tempfile
_territorial_bundle = base64.b64decode({encoded!r})
assert hashlib.sha256(_territorial_bundle).hexdigest() == {digest!r}
_territorial_path = os.path.join(tempfile.gettempdir(), 'territorial-{digest[:16]}.zip')
with open(_territorial_path, 'wb') as _territorial_file:
    _territorial_file.write(_territorial_bundle)
if _territorial_path not in sys.path:
    sys.path.insert(0, _territorial_path)
'''


def workflow_source(module):
    return f'''"""Readable Territorial workflow. Validate with --check-runtime before activation."""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--check-runtime", action="store_true")
    args = parser.parse_args()
    root = Path(args.runtime_root).resolve(strict=True)
    manifest_bytes = (root / "manifest.json").read_bytes()
    if hashlib.sha256(manifest_bytes).hexdigest() != root.name:
        raise RuntimeError("Territorial release manifest does not match its version")
    manifest = json.loads(manifest_bytes)
    for name, digest in manifest["files"].items():
        if not re.fullmatch(r"(?:territorial/[a-z_]+\\.py|prisma/__init__\\.py|social_network\\.py|sensor_stream\\.py|config\\.json|README\\.md)", name):
            raise RuntimeError("Unexpected Territorial release member")
        path = (root / name).resolve(strict=True)
        if not path.is_relative_to(root) or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise RuntimeError("Territorial release source does not match its manifest")
    config = json.loads((root / "config.json").read_text(encoding="utf-8"))
    sys.path.insert(0, str(root))
    from territorial.{module} import run
    for name, loaded in tuple(sys.modules.items()):
        if name == "territorial" or name.startswith("territorial."):
            if not Path(loaded.__file__).resolve().is_relative_to(root):
                raise RuntimeError("A different Territorial release is already imported")
    utilities = globals().get("aidputils")
    if utilities is None:
        import aidputils as utilities
    secret_get = utilities.secrets.get
    if not callable(secret_get):
        raise RuntimeError("Native AIDP secret access is unavailable")
    session = globals().get("spark")
    if session is None:
        from pyspark.sql import SparkSession
        session = SparkSession.builder.getOrCreate()
    if args.check_runtime:
        print(json.dumps({{"runtime_ready": True, "release": root.name, "workflow": "{module}"}}))
        return
    run(session, secret_get, config)


if __name__ == "__main__":
    main()
'''


def publish_runtime_sources(api, workspace, config, bundle, *, ensure_folder):
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        names = archive.namelist()
        if (len(names) != len(set(names)) or not {"territorial/__init__.py", "territorial/pipeline.py", "territorial/sensor_pipeline.py"} <= set(names)
                or any(not re.fullmatch(r"(?:territorial/[a-z_]+\.py|prisma/__init__\.py)", name) for name in names)):
            raise ValueError("Invalid Territorial runtime source archive")
        sources = {name: archive.read(name).decode("utf-8") for name in names}
    sources.update({"social_network.py": workflow_source("pipeline"), "sensor_stream.py": workflow_source("sensor_pipeline")})
    for name, source in sources.items():
        ast.parse(source, filename=name)
    sources["config.json"] = json.dumps(config, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    sources["README.md"] = """# Territorial workflows

Open `social_network.py` or `sensor_stream.py` in Workbench to inspect the Python
entrypoints. Their implementation is in the readable `territorial/` modules.
`config.json` contains runtime configuration and credential names, never secrets.
Secrets remain in the AIDP credential store. `prisma/__init__.py` is an import
compatibility shim for older notebooks; new sources use `territorial`.

Each release is immutable and checked against `manifest.json` before execution.
To improve the code, edit a copy, commit the changes to the source repository,
and redeploy a new release. Editing an active release in place fails its hash
check instead of silently changing a running workflow.

Both scripts accept `--runtime-root <this-release-directory> --check-runtime`
for a finite import/Spark/credential-access capability check. That mode does not
run either pipeline or retrieve secret values. Normal workflow execution omits
`--check-runtime` and uses the existing capture controls and checkpoints.
"""
    manifest = json.dumps({"bundle_sha256": hashlib.sha256(bundle).hexdigest(),
        "files": {name: hashlib.sha256(source.encode("utf-8")).hexdigest() for name, source in sorted(sources.items())}},
        sort_keys=True, indent=2) + "\n"
    root = "/Workspace/territorial/releases/" + hashlib.sha256(manifest.encode("utf-8")).hexdigest()
    for path in ("/Workspace/territorial", "/Workspace/territorial/releases", root, root + "/territorial", root + "/prisma"):
        ensure_folder(api, workspace, path)
    for name, source in sorted(sources.items()):
        upload(api, workspace, root + "/" + name, source)
    upload(api, workspace, root + "/manifest.json", manifest)
    return root


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
        raise RuntimeError("Territorial versioned workspace path already has different content")
    if kind == "notebook":
        parent = path.rsplit("/", 1)[0]
        created = api.request("POST", f"/workspaces/{workspace}/notebook/api/contents/{quote(parent, safe='')}",
                              payload={"copy_from": None, "ext": ".ipynb", "type": "notebook"}).body
        if not created.get("path"):
            raise RuntimeError("Territorial notebook creation did not return a path")
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
        raise RuntimeError("Territorial workspace content round-trip mismatch")


def install_volumes(api, config):
    catalog = named(api, "/catalogs", config["catalog"])
    if not catalog:
        raise RuntimeError("Territorial requires its existing governed catalog")
    schema = ensure(api, "/schemas", "prisma_ingest", {"displayName": "prisma_ingest", "catalogName": config["catalog"]},
                    ready=True, params={"catalogKey": catalog["key"]})
    schema_detail = api.request("GET", "/schemas/" + quote(str(schema["key"]), safe="")).body
    if schema_detail.get("catalogName") != config["catalog"] or schema_detail.get("displayName") != "prisma_ingest":
        raise RuntimeError("Territorial ingest schema does not match the governed catalog")
    for name, kind in (("landing", "EXTERNAL"), ("checkpoints", "MANAGED")):
        payload = {"displayName": name, "catalogName": config["catalog"], "schemaName": "prisma_ingest", "volumeType": kind}
        if kind == "EXTERNAL":
            payload["storageLocation"] = f"oci://{config['landing_bucket']}@{config['namespace']}/{config['landing_prefix']}"
        volume = ensure(api, "/volumes", name, payload, ready=True,
                        params={"catalogKey": catalog["key"], "schemaKey": schema["key"]})
        detail = api.request("GET", "/volumes/" + quote(str(volume["key"]), safe="")).body
        if any(detail.get(field) != value for field, value in payload.items()):
            raise RuntimeError("Territorial volume type or location differs from its configured contract")


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
                raise RuntimeError("Pause jobs using the shared cluster before installing Territorial libraries")
    return assignments


def cluster_idle(api, workspace, compute):
    base = f"/workspaces/{workspace}"
    detail = api.request("GET", base + "/clusters/" + quote(compute, safe="")).body
    if detail.get("attachedSessions") or detail.get("attachedNotebooks"):
        raise RuntimeError("Territorial library installation requires a cluster without attached notebook sessions")
    assignments = cluster_job_assignments(api, base, compute)
    runs = items(api, base + "/jobRuns", {"sortBy": "timeCreated", "sortOrder": "DESC", "limit": 100})
    for run in runs:
        if run_state(run) not in RUN_SUCCESS | RUN_FAILED:
            assigned = assignments.get(run.get("jobKey"))
            if not assigned or compute in assigned:
                raise RuntimeError("Territorial library installation cannot resolve or safely exclude an active run on this cluster")


def install_cluster_libraries(api, workspace, compute, *, ensure_folder):
    digest = hashlib.sha256(PIPELINE_REQUIREMENTS.encode()).hexdigest()[:12]
    directory = "/Workspace/medallon/prisma/dependencies_" + digest
    ensure_folder(api, workspace, "/Workspace/medallon/prisma")
    ensure_folder(api, workspace, directory)
    path = directory + "/requirements.txt"
    upload(api, workspace, path, PIPELINE_REQUIREMENTS)
    base = f"/workspaces/{workspace}/clusters/{quote(compute, safe='')}"

    def status():
        matches = [item for item in items(api, base + "/libraries") if item.get("path") == path and item.get("status") != "DELETED"]
        if len(matches) > 1:
            raise RuntimeError("Duplicate Territorial cluster library")
        state = matches[0].get("status") if matches else None
        if state in {"FAILED", "SKIPPED", "UNINSTALL_ON_RESTART"}:
            raise RuntimeError("Territorial cluster library is not usable: " + state)
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
            raise RuntimeError("Territorial cluster failed while installing libraries")
        if state == "ACTIVE" and status() == "INSTALLED":
            return path
        pause()


def install_stream_compute(api, workspace, name):
    """Dedicated small USER cluster; omission of autoTerminationMinutes keeps it always on."""
    from app.aidp import AidpClient
    if name not in {"social_stream_compute", "sensor_stream_compute"}:
        raise ValueError("Invalid dedicated streaming compute")
    payload = {"type": "USER", "displayName": name,
        "description": "Dedicated Territorial Control permanent streaming workflow",
        "driverConfig": {"driverShape": "amd.generic", "driverShapeConfig": {"ocpus": 2, "memoryInGBs": 32}},
        "workerConfig": {"workerShape": "amd.generic", "workerShapeConfig": {"ocpus": 2, "memoryInGBs": 32},
                         "minWorkerCount": 1, "maxWorkerCount": 1},
        "clusterRuntimeConfig": {"type": "SPARK", "sparkVersion": "3.5.0",
            "sparkAdvancedConfigurations": {"spark.aidp.lineage.enabled": "true"}, "sparkEnvVariables": {}, "initScripts": []}}
    path = f"/workspaces/{workspace}/clusters"
    resource = ensure(api, path, name, payload, ready=True)
    path += "/" + quote(str(resource["key"]), safe="")
    expected = {key: value for key, value in payload.items() if key != "description"}
    started = False
    while True:
        response = api.request("GET", path)
        detail = response.body
        if not AidpClient._notebook_matches(detail, expected) or detail.get("autoTerminationMinutes") not in (None, 0):
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
    matches = [job for job in items(api, base) if job.get("name") in {name, legacy_name}
               and (job.get("lifecycleState") or job.get("state")) != "DELETED"]
    if len(matches) > 1 or (key and matches and matches[0].get("key") != key):
        raise RuntimeError("Duplicate or mismatched managed Territorial workflow")
    if key:
        current = api.request("GET", base + "/" + quote(key, safe="")).body
        if (current.get("name") not in {name, legacy_name} or current.get("key") != key
                or (current.get("lifecycleState") or current.get("state")) in {"DELETING", "DELETED"}):
            raise RuntimeError("Configured Territorial workflow identity does not match")
        return current
    if matches and (matches[0].get("lifecycleState") or matches[0].get("state")) == "DELETING":
        raise RuntimeError("Managed Territorial workflow is being deleted")
    return matches[0] if matches else None


def install_job(api, workspace, compute, config, bundle, *, ensure_folder, workflow="social"):
    from app.aidp import AidpClient
    if config.get("streaming_mode", "finite") not in {"finite", "persistent"}:
        raise ValueError("Invalid Territorial Control streaming mode")
    persistent = config.get("streaming_mode") == "persistent"
    if workflow not in {"social", "sensors"} or (workflow == "sensors" and not persistent):
        raise ValueError("Invalid Territorial Control workflow")
    task = "sensor_stream" if workflow == "sensors" else "social_network"
    name = "territorial_" + task
    legacy_name = "prisma_colombia_sensors" if workflow == "sensors" else "prisma_bogota_tick"
    current = managed_workflow(api, workspace, name, legacy_name, config.get("sensor_job_key" if workflow == "sensors" else "job_key"))
    root = publish_runtime_sources(api, workspace, config, bundle, ensure_folder=ensure_folder)
    path = root + "/" + task + ".py"
    payload = {"name": name, "path": "/Workspace/territorial", "description": "Finite Territorial collection and publication tick",
        "maxConcurrentRuns": 1, "queue": {"isEnabled": False}, "timeoutSeconds": 600,
        "schedule": {"quartzCronExpression": "0 * * * * ?", "timezoneId": "UTC", "pauseStatus": "PAUSED"},
        "jobClusters": [{"clusterKey": compute}], "tasks": [{"type": "PYTHON_TASK", "taskKey": task,
        "dependsOn": [], "runIf": "ALL_SUCCESS", "maxRetries": 0, "isRetryOnTimeout": False,
        # AIDP assigns this string to sys.argv; it expects a list literal, not shell arguments.
        "source": "WORKSPACE", "filePath": path, "commandLineArguments": json.dumps([path, "--runtime-root", root]),
        "cluster": {"clusterKey": compute}}]}
    payload["tasks"][0]["isStreaming"] = persistent
    if persistent:
        payload.update(description="Territorial Control persistent " + ("sensor TXT ingestion" if workflow == "sensors" else "social ingestion and publication"))
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
            raise RuntimeError("Stop the managed Territorial Control workflow before upgrading its source or compute")
        operation(api, api.request("PUT", f"/workspaces/{workspace}/jobs/{key}", payload=payload,
                                  headers={"If-Match": detail.headers["etag"]} if detail.headers.get("etag") else None))
        if not matches(api.request("GET", f"/workspaces/{workspace}/jobs/{key}").body):
            raise RuntimeError("Territorial job definition did not round-trip")
    return key


def agent_deployments(api, base):
    deployments = [item for item in items(api, base) if (item.get("lifecycleState") or item.get("state")) != "DELETED"]
    if len(deployments) > 1:
        raise RuntimeError("Duplicate Territorial agent deployments")
    return deployments


def deployment_endpoint(detail, region):
    endpoint = str(detail.get("endpointUrl") or "").rstrip("/")
    if not endpoint.endswith("/chat"):
        endpoint += "/chat"
    url = urlsplit(endpoint)
    if (url.scheme != "https" or url.netloc != f"gateway.aidp.{region}.oci.oraclecloud.com"
        or url.query or url.fragment or not re.fullmatch(r"/agentendpoint/[A-Za-z0-9_.-]+/chat", url.path)):
        raise RuntimeError("Territorial agent endpoint did not round-trip")
    retention = {key: value for key, value in detail.get("sessionRetentionConfig", {}).items() if value is not None}
    if retention != SESSION_RETENTION:
        raise RuntimeError("Territorial agent retention did not round-trip")
    return endpoint


def wait_agent_deployment(api, base, region):
    while True:
        for item in agent_deployments(api, base):
            detail = api.request("GET", base + "/" + quote(str(item["key"]), safe="")).body
            state = str(detail.get("lifecycleState") or detail.get("state") or "").upper()
            if state in {"FAILED", "INACTIVE", "DELETING", "DELETED"} or state.endswith("_FAILED"):
                raise RuntimeError("Territorial agent deployment failed; prior deployment preserved")
            if state == "ACTIVE":
                return {"deployment_key": item["key"], "endpoint": deployment_endpoint(detail, region)}
        pause(10)


def publish_agent(api, workspace, bundle, region, runtime=None):
    root = "/Workspace/territorial"
    agent_config = {key: runtime[key] for key in ("region", "model_id", "compartment_id", "oci_credential_name", "oci_identity_sha256")} if runtime is not None else None
    if agent_config is not None:
        agent_config["reader_credential_name"] = runtime.get("reader_credential_name", "PrismaReaderRuntime")
    suffix = "\nRUNTIME_CONFIG = " + repr(agent_config) + "\n" if agent_config is not None else ""
    digest = hashlib.sha256(bundle + suffix.encode()).hexdigest()[:12]
    entry = root + f"/agent_{digest}.py"
    dependencies = root + "/requirements_" + digest + ".txt"
    upload(api, workspace, dependencies, "oracledb==3.4.2\n")
    upload(api, workspace, entry, bundle_prelude(bundle) + (ROOT / "apps/backend/app/territorial/agent.py").read_text(encoding="utf-8").replace("from __future__ import annotations\n", "") + suffix)
    compute = ensure(api, f"/workspaces/{workspace}/clusters", "prisma_agent_compute", {
        "type": "AI_COMPUTE", "displayName": "prisma_agent_compute", "description": "Territorial conversational agent",
        "driverConfig": {"driverShapeConfig": {"ocpus": 1, "memoryInGBs": 16}}, "replicaConfig": {"minReplica": 1, "maxReplica": 1}}, ready=True)
    if str(compute.get("type") or compute.get("sourceApi")) != "AI_COMPUTE":
        raise RuntimeError("Existing Territorial agent compute is not AI_COMPUTE")
    name = "territorial_assistant_" + digest
    agent = ensure(api, f"/workspaces/{workspace}/agents", name, {
        "displayName": name, "description": "Territorial evidence-grounded assistant", "type": "CODE", "pathInfo": "/Workspace",
        "entryFilePath": entry, "dependenciesFilePath": dependencies, "computeKey": compute["key"],
        "sessionConfig": {"variables": {}, "sessionRetentionConfig": SESSION_RETENTION}})
    detail = api.request("GET", f"/workspaces/{workspace}/agents/{quote(str(agent['key']), safe='')}").body
    if (detail.get("type") != "CODE" or str(detail.get("entryFilePath", "")).lstrip("/") != entry.lstrip("/")
        or detail.get("computeKey") != compute["key"]):
        raise RuntimeError("Territorial agent definition did not round-trip")
    base = f"/workspaces/{workspace}/agents/{agent['key']}/deployments"
    if not agent_deployments(api, base):
        operation(api, api.request("POST", base + "/actions/deploy", payload={"displayName": name + "_deployment",
            "description": "Territorial deployment " + digest, "agentKey": agent["key"], "agentComputeKey": compute["key"],
            "sessionRetentionConfig": SESSION_RETENTION}))
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
    python = (task.get("type") == "PYTHON_TASK" and task.get("taskKey") == "social_network" and task.get("source") == "WORKSPACE"
              and re.fullmatch(r"/Workspace/territorial/releases/[a-f0-9]{64}/social_network\.py", path)
              and task.get("commandLineArguments") == json.dumps([path, "--runtime-root", path.rsplit("/", 1)[0]]))
    legacy = task.get("type") == "NOTEBOOK_TASK" and re.fullmatch(r"/Workspace/medallon/prisma/prisma_tick_[a-f0-9]{12}\.ipynb", path)
    if not python and not legacy:
        raise RuntimeError("Territorial initial job must reference one content-versioned source")
    token = hashlib.sha256(f"{api.deployment_id}:prisma:{job}:{revision}:{path}".encode()).hexdigest()
    response = api.request("POST", base, payload={"jobKey": job, "parameters": []},
                           headers={"opc-retry-token": token})
    operation(api, response)
    key = str((response.body or {}).get("key") or "")
    if not key:
        raise RuntimeError("Territorial initial job run did not return a key")

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
                raise RuntimeError("Territorial initial task failed; no readiness claimed")
        elif state in RUN_FAILED:
            raise RuntimeError("Territorial initial job failed; no readiness claimed")
        pause(10)


def validate_publication(storage, runtime):
    def read(key):
        response = storage.get_object(runtime["namespace"], runtime["bucket"], key)
        content = response.data.content
        if len(content) > 10 * 1024 * 1024:
            raise RuntimeError("Territorial publication exceeds its bounded bootstrap check")
        return json.loads(content)
    pointer = read("04_gold/prisma/current.json")
    key = str(pointer.get("snapshot_key") or "")
    if not key.startswith("04_gold/prisma/snapshots/") or ".." in key or not pointer.get("version"):
        raise RuntimeError("Territorial initial publication pointer is invalid")
    snapshot = read(key)
    if snapshot.get("version") != pointer["version"]:
        raise RuntimeError("Territorial initial publication is incomplete")
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


def bootstrap_territorial(api, context, outputs, config, signer, storage, wallet, wallet_password, admin_password, reconciled,
                     *, deadline, wallet_dsn, validate_wallet, generate_password, ensure_folder):
    global _deadline
    _deadline = deadline
    import oracledb
    agent_api = api.__class__(context["region"], outputs["ai_data_platform_id"], signer, context["deployment_id"],
                             api_version="20260430", resource_segment="aiDataPlatforms")
    database_users(agent_api, wallet, wallet_password, admin_password, {**config, "region": context["region"]}, outputs,
                   wallet_dsn=wallet_dsn, validate_wallet=validate_wallet, generate_password=generate_password)
    oci_credential = shared_credential(items(agent_api, "/credentials"))
    if oci_credential is None:
        raise RuntimeError("Shared OCI runtime credential is unavailable")
    bundle = runtime_archive()
    runtime = {"namespace": outputs["objectstorage_namespace"], "bucket": outputs["medallion_bucket_names"]["gold"], "workbench_base": api.base,
        "region": context["region"], "model_id": outputs["agent_model_id"], "compartment_id": outputs["compartment_ocid"], "catalog": reconciled["catalog_name"],
        "streaming_mode": "persistent", "pipeline_revision": hashlib.sha256(bundle).hexdigest(),
        "oci_credential_name": oci_credential["displayName"], "oci_identity_sha256": identity_hash(config),
        "writer_credential_name": database_credential_name(agent_api),
        "reader_credential_name": database_credential_name(agent_api, reader=True)}
    runtime.update(landing_bucket=outputs["medallion_bucket_names"]["landing"], landing_prefix="01_landing/prisma/raw/",
        landing_volume_path=f"/Volumes/{runtime['catalog']}/prisma_ingest/landing",
        checkpoint_volume_path=f"/Volumes/{runtime['catalog']}/prisma_ingest/checkpoints/bronze-v1",
        sensor_landing_prefix="01_landing/prisma/raw/sensors/",
        sensor_landing_volume_path=f"/Volumes/{runtime['catalog']}/prisma_ingest/landing/sensors",
        sensor_checkpoint_volume_path=f"/Volumes/{runtime['catalog']}/prisma_ingest/checkpoints/sensors-v1")
    workspace = reconciled["workspace_key"]
    install_volumes(agent_api, runtime)
    social_compute = install_stream_compute(api, workspace, "social_stream_compute")
    sensor_compute = install_stream_compute(api, workspace, "sensor_stream_compute")
    for compute in (social_compute, sensor_compute):
        install_cluster_libraries(agent_api, workspace, compute, ensure_folder=ensure_folder)
    job = install_job(api, workspace, social_compute, runtime, bundle, ensure_folder=ensure_folder)
    sensor_job = install_job(api, workspace, sensor_compute, runtime, bundle, ensure_folder=ensure_folder, workflow="sensors")
    runtime.update(workspace_key=workspace, job_key=job, sensor_job_key=sensor_job,
                   social_compute_key=social_compute, sensor_compute_key=sensor_compute)
    agent = publish_agent(agent_api, workspace, bundle, context["region"], runtime)
    # Materialize empty governed roots; Spark ignores these hidden non-event objects.
    for prefix in (runtime["landing_prefix"], runtime["sensor_landing_prefix"]):
        storage.put_object(runtime["namespace"], runtime["landing_bucket"], prefix + ".keep", b"", content_type="application/octet-stream")
    with tempfile.TemporaryDirectory(prefix="territorial-config-") as directory:
        with zipfile.ZipFile(io.BytesIO(wallet)) as archive:
            archive.extractall(directory)
        with oracledb.connect(user="ADMIN", password=admin_password, dsn=wallet_dsn(Path(directory)),
            config_dir=directory, wallet_location=directory, wallet_password=wallet_password) as connection:
            current = read_document(connection, "runtime")
            desired = {**current, **runtime}
            if any(current.get(key) != value for key, value in desired.items()):
                write_document(connection, "runtime", desired, current["revision"])
            connection.commit()
            sensor_run = start_stream_job(api, workspace, sensor_job, "sensor_stream")
            run_key = start_stream_job(api, workspace, job, "social_network")
            version = wait_stream_jobs(api, storage, runtime, [(run_key, "social_network"), (sensor_run, "sensor_stream")], connection)
    storage.put_object(runtime["namespace"], runtime["bucket"], ".control/prisma/agent.json", json.dumps(agent).encode(), content_type="application/json")
    result = {"job_ready": True, "agent_ready": True, "revision": agent["revision"],
              "acceptance_run": run_key, "sensor_run": sensor_run, "snapshot_version": version}
    return {**{"territorial_" + key: value for key, value in result.items()},
            **{"prisma_" + key: value for key, value in result.items()}, "external_volume_count": 1}
