"""Native AIDP schedules and single-run persistent task admission."""
import time
from urllib.parse import quote, urlsplit

from .database import read_document


JOB_FIELDS = ("runAs", "name", "path", "description", "maxConcurrentRuns", "jobClusters", "tasks",
              "queue", "schedule", "continuous", "gitConfig", "parameters", "timeoutSeconds")
RUN_SUCCESS = {"SUCCESS", "SUCCEEDED"}
RUN_FAILED = {"FAILED", "ERROR", "CANCELED", "CANCELLED", "TIMED_OUT", "SKIPPED", "BLOCKED",
              "INTERNAL_ERROR", "UPSTREAM_FAILED", "UPSTREAM_CANCELED", "EXCLUDED"}
TASK_RUN_QUERY = {"sortBy": "timeCreated", "sortOrder": "ASC", "limit": 100}
SOCIAL_TASK_KEYS = {"social_network", "prisma_tick"}  # Retain admission for existing job histories.


def run_state(document):
    state = document.get("state") or {}
    value = state.get("status") if isinstance(state, dict) else state
    return str(value or document.get("status") or "").upper()


def task_outcome(tasks):
    if any(task.get("taskKey") not in SOCIAL_TASK_KEYS or run_state(task) in RUN_FAILED for task in tasks):
        return "FAILED"
    if tasks and all(run_state(task) in RUN_SUCCESS for task in tasks):
        return "SUCCESS"
    return "RUNNING"


def needs_schedule(configuration, simulation, now=None):
    now = time.time() if now is None else now
    elapsed = float(simulation.get("elapsed_seconds", 0))
    if simulation.get("status") == "running":
        elapsed += max(0, now - float(simulation.get("started_at", now)))
        if elapsed < 600 or not simulation.get("capture_complete", False) or simulation.get("final_job_pending"):
            return True
    return bool(configuration.get("sensors", {}).get("capture_running")) or any(source.get("enabled") and source.get("capture_running") for source in configuration.get("sources", {}).values())


def job_path(runtime):
    if not runtime.get("workspace_key") or not runtime.get("job_key"):
        raise RuntimeError("Gods Eye View native job is not configured")
    return f"/workspaces/{quote(runtime['workspace_key'], safe='')}/jobs/{quote(runtime['job_key'], safe='')}"


def set_schedule(request, runtime, enabled):
    path = job_path(runtime)
    job, headers = request("GET", path, phase="content", include_headers=True)
    etag = headers.get("etag") or headers.get("ETag")
    persistent = any(task.get("isStreaming") for task in job.get("tasks", []))
    payload = {key: job[key] for key in JOB_FIELDS if key in job}
    payload["tasks"] = [dict(task) for task in job.get("tasks", [])]
    # Native GET returns zero for an unlimited timeout, but PUT rejects explicit values below 60.
    for item in [payload, *payload["tasks"]]:
        if item.get("timeoutSeconds") in (None, 0):
            item.pop("timeoutSeconds", None)
    payload.update(maxConcurrentRuns=1, queue={"isEnabled": False},
        schedule={"quartzCronExpression": "0 * * * * ?", "timezoneId": "UTC", "pauseStatus": "UNPAUSED" if enabled and not persistent else "PAUSED"})
    if persistent and payload.get("continuous"):
        payload["continuous"] = {**payload["continuous"], "pauseStatus": "PAUSED"}
    # ponytail: GET may omit ETag, making concurrent edits last-write-wins; server ETags restore conditional protection.
    request("PUT", path, payload=payload, headers={"If-Match": etag} if etag else None, phase="content")
    return persistent


def active_run(request, runtime):
    path = f"/workspaces/{quote(runtime['workspace_key'], safe='')}/jobRuns"
    params = {"jobKey": runtime["job_key"], "sortBy": "timeCreated", "sortOrder": "DESC", "limit": 100}
    seen = set()
    # ponytail: cap history at 500 runs; a larger history requires operator inspection, never an unsafe duplicate.
    for _ in range(5):
        body, headers = request("GET", path, params=params, include_headers=True, phase="content")
        rows = body if isinstance(body, list) else body.get("items") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            raise RuntimeError("Native run inspection returned an invalid collection")
        for run in rows:
            if not isinstance(run, dict) or not run.get("jobKey") or not run.get("key"):
                raise RuntimeError("Native run inspection returned an incomplete identity")
            if run["jobKey"] == runtime["job_key"] and run_state(run) not in RUN_SUCCESS | RUN_FAILED:
                return run
        page = headers.get("opc-next-page") or headers.get("Opc-Next-Page")
        if not page:
            return None
        if page in seen:
            break
        seen.add(page)
        params["page"] = page
    raise RuntimeError("Native run inspection exceeded its pagination bound")


def submit_run(request, runtime, request_id, *, persistent=False):
    job_path(runtime)
    if persistent:
        existing = active_run(request, runtime)
        if existing:
            return existing
    # Native maxConcurrentRuns=1 plus queue=false closes the race between simultaneous streaming starts.
    return request("POST", f"/workspaces/{quote(runtime['workspace_key'], safe='')}/jobRuns",
        payload={"jobKey": runtime["job_key"], "parameters": [], "queue": {"isEnabled": not persistent}},
        phase="content", retry_scope="prisma-run:" + request_id)


def keep_streams_running(request, runtime, request_id):
    """One run per independent stream, including idle sources; failures resume their checkpoints."""
    if runtime.get("streaming_mode") != "persistent":
        return
    errors = []
    for field in ("job_key", "sensor_job_key"):
        if not runtime.get(field):
            raise RuntimeError("Independent streaming workflows are not configured")
        try:
            submit_run(request, {**runtime, "job_key": runtime[field]}, request_id + "-" + field, persistent=True)
        except Exception as exc:
            errors.append(exc)
    if errors:
        raise errors[0]


def workbench_request(base, region, signed):
    from oci._vendor import requests
    parsed = urlsplit(base)
    if (parsed.scheme != "https" or parsed.hostname != f"datalake.{region}.oci.oraclecloud.com"
            or parsed.username or parsed.password or parsed.port or parsed.query or parsed.fragment):
        raise ValueError("Invalid verified Gods Eye View Workbench endpoint")
    def request(method, path, *, payload=None, headers=None, include_headers=False, phase=None, retry_scope=None):
        if not path.startswith("/workspaces/"):
            raise ValueError("Gods Eye View scheduler is limited to its workspace")
        response = requests.request(method, base.rstrip("/") + path, auth=signed, json=payload,
                                    headers={"Accept": "application/json", **(headers or {})}, timeout=(10, 30))
        if not 200 <= response.status_code < 300:
            raise RuntimeError(f"Gods Eye View scheduler request failed with HTTP {response.status_code}")
        body = response.json() if response.content else None
        return (body, response.headers) if include_headers else body
    return request


def reconcile_after_tick(connection, request, now):
    configuration = read_document(connection, "configuration")
    simulation = read_document(connection, "simulation")
    runtime = read_document(connection, "runtime")
    pipeline = read_document(connection, "status_pipeline")
    pending = pipeline.get("pending_count", 0) > 0 and not pipeline.get("needs_attention")
    set_schedule(request, runtime, needs_schedule(configuration, simulation, now) or pending)
