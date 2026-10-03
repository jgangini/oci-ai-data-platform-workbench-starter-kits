"""Native AIDP schedule reconciliation; manual work uses finite queued job runs."""
import time
from urllib.parse import quote, urlsplit

from .database import read_document


JOB_FIELDS = ("name", "path", "description", "maxConcurrentRuns", "jobClusters", "tasks", "queue", "schedule", "timeoutSeconds")


def needs_schedule(configuration, simulation, now=None):
    now = time.time() if now is None else now
    elapsed = float(simulation.get("elapsed_seconds", 0))
    if simulation.get("status") == "running":
        elapsed += max(0, now - float(simulation.get("started_at", now)))
        if elapsed < 600 or not simulation.get("capture_complete", False) or simulation.get("final_job_pending"):
            return True
    return any(source.get("enabled") and source.get("mode") == "real" for source in configuration.get("sources", {}).values())


def job_path(runtime):
    if not runtime.get("workspace_key") or not runtime.get("job_key"):
        raise RuntimeError("PRISMA native job is not configured")
    return f"/workspaces/{quote(runtime['workspace_key'], safe='')}/jobs/{quote(runtime['job_key'], safe='')}"


def set_schedule(request, runtime, enabled):
    path = job_path(runtime)
    job, headers = request("GET", path, phase="content", include_headers=True)
    etag = headers.get("etag")
    if not etag:
        raise RuntimeError("PRISMA job schedule update requires an ETag")
    payload = {key: job[key] for key in JOB_FIELDS if key in job}
    payload.update(maxConcurrentRuns=1, queue={"isEnabled": False},
        schedule={"quartzCronExpression": "0 * * * * ?", "timezoneId": "UTC", "pauseStatus": "UNPAUSED" if enabled else "PAUSED"})
    request("PUT", path, payload=payload, headers={"If-Match": etag}, phase="content")


def submit_run(request, runtime, request_id):
    job_path(runtime)
    # Queue only this explicit run. Periodic ticks retain queue=false and cannot accumulate a backlog.
    return request("POST", f"/workspaces/{quote(runtime['workspace_key'], safe='')}/jobRuns",
        payload={"jobKey": runtime["job_key"], "parameters": [], "queue": {"isEnabled": True}},
        phase="content", retry_scope="prisma-run:" + request_id)


def workbench_request(base, region, signed):
    from oci._vendor import requests
    parsed = urlsplit(base)
    if (parsed.scheme != "https" or parsed.hostname != f"datalake.{region}.oci.oraclecloud.com"
            or parsed.username or parsed.password or parsed.port or parsed.query or parsed.fragment):
        raise ValueError("Invalid verified PRISMA Workbench endpoint")
    def request(method, path, *, payload=None, headers=None, include_headers=False, phase=None, retry_scope=None):
        if not path.startswith("/workspaces/"):
            raise ValueError("PRISMA scheduler is limited to its workspace")
        response = requests.request(method, base.rstrip("/") + path, auth=signed, json=payload,
                                    headers={"Accept": "application/json", **(headers or {})}, timeout=(10, 30))
        if not 200 <= response.status_code < 300:
            raise RuntimeError(f"PRISMA scheduler request failed with HTTP {response.status_code}")
        body = response.json() if response.content else None
        return (body, response.headers) if include_headers else body
    return request


def reconcile_after_tick(connection, request, now):
    configuration = read_document(connection, "configuration")
    simulation = read_document(connection, "simulation")
    runtime = read_document(connection, "runtime")
    set_schedule(request, runtime, needs_schedule(configuration, simulation, now))
