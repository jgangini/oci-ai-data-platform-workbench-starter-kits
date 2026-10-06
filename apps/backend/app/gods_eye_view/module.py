"""Activate the global module on infrastructure provisioned by Deploy Studio."""
import asyncio
import json
import os
from urllib.parse import quote
from uuid import uuid4

import httpx
from fastapi import HTTPException

from .agent_gateway import checked_endpoint
from .scheduling import RUN_FAILED, RUN_SUCCESS, SOCIAL_TASK_KEYS, TASK_RUN_QUERY, active_run, job_path, run_state, submit_run, task_outcome

PACKAGE = {"package_id": "gods_eye_view", "display_name": "God’s Eye View · Custom layers",
           "bundled_version": "1.0.0", "kind": "module", "scope": "global", "status": "available"}


class GodsEyeViewModule:
    def __init__(self, settings, runtime):
        self.settings, self.runtime = settings, runtime
        self.lock = asyncio.Lock()

    async def status(self, deploy=False):
        async with self.lock:
            try:
                return await asyncio.to_thread(self._status, deploy)
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(503, "The Gods Eye View native runtime is not ready. Check the Deploy Studio post-apply result and retry.") from exc

    def _read(self):
        if self.settings.gods_eye_view_local_mode:
            with self.runtime.store.connection() as db:
                return self.runtime.store._get(db, "status_module", {})
        return self.runtime._doc("status_module")

    def _write(self, values):
        if self.settings.gods_eye_view_local_mode:
            with self.runtime.store.connection() as db:
                self.runtime.store._put(db, "status_module", values)
            return values
        return self.runtime._change("status_module", lambda document: {**document, **values})

    def _response(self, state, **values):
        return {"module_id": PACKAGE["package_id"], "display_name": PACKAGE["display_name"],
                "installed": bool(state.get("enabled")), "enabled": bool(state.get("enabled")),
                "status": state.get("status", "available"), "operation_id": state.get("operation_id"),
                "viewer_url": "/gods-eye-view/", "runtime": "local_fixture" if self.settings.gods_eye_view_local_mode else "aidp",
                "message": state.get("message", "Enable the module to validate its viewer, native workflow and agent."), **values}

    def _stream_runs(self, client, runtime, social_job):
        if (runtime.get("streaming_mode") != "persistent" or not runtime.get("sensor_job_key")
                or runtime["sensor_job_key"] == runtime["job_key"]):
            raise HTTPException(409, "Both independent persistent workflows must be provisioned before activation.")
        runs = []
        for field, allowed_keys in (("job_key", SOCIAL_TASK_KEYS), ("sensor_job_key", {"sensor_stream"})):
            scoped = {**runtime, "job_key": runtime[field]}
            job = social_job if field == "job_key" else client._request("GET", job_path(scoped))
            tasks = job.get("tasks", [])
            if len(tasks) != 1 or tasks[0].get("taskKey") not in allowed_keys or tasks[0].get("isStreaming") is not True:
                raise HTTPException(503, "The provisioned persistent workflow definition is incomplete.")
            task_key = tasks[0]["taskKey"]
            run = active_run(client._request, scoped)
            if not run or run_state(run) != "RUNNING":
                raise HTTPException(503, "Both existing native workflows must be running before activation.")
            attempts = client._list("/workspaces/" + quote(runtime["workspace_key"], safe="") + "/taskRuns",
                                    params={"jobRunKey": run["key"], **TASK_RUN_QUERY})
            # TASK_RUN_QUERY orders all pages oldest first; only the latest retry represents this run.
            if (not attempts or any(task.get("taskKey") != task_key for task in attempts)
                    or run_state(attempts[-1]) != "RUNNING"):
                raise HTTPException(503, "Both existing native workflow tasks must be running before activation.")
            runs.append(run["key"])
        return runs

    def _prerequisites(self):
        runtime = self.runtime._doc("runtime")
        client = self.runtime.aidp_factory()
        job = client._request("GET", job_path(runtime))
        if not any(task.get("taskKey") in SOCIAL_TASK_KEYS for task in job.get("tasks", [])):
            raise HTTPException(503, "The provisioned Gods Eye View workflow is incomplete.")
        streams = self._stream_runs(client, runtime, job) if any(task.get("isStreaming") for task in job.get("tasks", [])) else []
        if runtime.get("streaming_mode") == "persistent" and not streams:
            raise HTTPException(503, "The provisioned persistent workflow definition is incomplete.")
        response = client.object_storage.get_object(runtime["namespace"], runtime["bucket"], ".control/prisma/agent.json")
        metadata = json.loads(response.data.content)
        checked_endpoint(metadata["endpoint"], runtime["region"])
        path = "/workspaces/{}/agents/{}/deployments/{}".format(
            *(quote(str(value), safe="") for value in (runtime["workspace_key"], metadata["agent_key"], metadata["deployment_key"]))
        )
        agent = client._request("GET", path)
        if str(agent.get("lifecycleState") or agent.get("state")) != "ACTIVE":
            raise HTTPException(503, "The Gods Eye View agent deployment is not active.")
        viewer = os.environ.get("GODS_EYE_VIEW_URL", os.environ.get("TERRITORIAL_VIEWER_URL", os.environ.get("PRISMA_VIEWER_URL", ""))).rstrip("/")
        if not viewer:
            raise HTTPException(503, "The private viewer URL is missing; redeploy with the God's Eye View option.")
        with httpx.Client(timeout=10, follow_redirects=False) as http:
            ready = http.get(viewer + "/ready")
            ready.raise_for_status()
        self.runtime._snapshot()
        return client, runtime, streams

    def _poll(self, state, client, runtime):
        base = "/workspaces/" + quote(runtime["workspace_key"], safe="")
        result = client._request("GET", base + "/jobRuns/" + quote(state["run_key"], safe=""))
        status = run_state(result)
        if status in RUN_FAILED:
            return self._write({**state, "status": "failed", "enabled": False,
                                "message": "Native Gods Eye View activation failed; inspect the AIDP job run before retrying."})
        if status in RUN_SUCCESS:
            tasks = client._list(base + "/taskRuns", params={"jobRunKey": state["run_key"], **TASK_RUN_QUERY})
            outcome = task_outcome(tasks)
            if outcome == "SUCCESS":
                snapshot = self.runtime._snapshot()
                return self._write({**state, "status": "ready", "enabled": True, "snapshot_version": snapshot["version"],
                                    "message": "Native workflow, publication and private viewer verified. Agent deployment is active; conversation acceptance is separate."})
            if outcome == "FAILED":
                return self._write({**state, "status": "failed", "enabled": False, "message": "The native Gods Eye View task failed."})
        return state

    def _activate(self, state, client, runtime):
        if not state.get("enabled") and state.get("status") != "activating":
            state = self._write({"status": "activating", "enabled": False, "operation_id": str(uuid4()), "run_key": None,
                                 "message": "Waiting for the native Gods Eye View activation run and its publication."})
        if state.get("status") == "activating" and not state.get("run_key"):
            result = submit_run(client._request, runtime, state["operation_id"])
            if not result.get("key"):
                raise HTTPException(503, "AIDP did not return the activation run key; retry the same operation.")
            state = self._write({**state, "run_key": str(result["key"])})
        return state

    def _status(self, deploy):
        if not self.settings.gods_eye_view_enabled:
            message = "Deploy this release with the God's Eye View option in Deploy Studio before enabling the module."
            if deploy:
                raise HTTPException(409, message)
            return self._response({}, status="deployment_required", message=message)
        state = self._read()
        if self.settings.gods_eye_view_local_mode:
            if deploy and not state.get("enabled"):
                state = self._write({"enabled": True, "status": "ready", "operation_id": str(uuid4()),
                                     "message": "Local simulation module enabled. OCI/AIDP deployment is not claimed."})
            return self._response(state)
        client, runtime, streams = self._prerequisites()
        if streams:
            if deploy and not state.get("enabled"):
                state = self._write({**state, "status": "ready", "enabled": True,
                    "operation_id": state.get("operation_id") or str(uuid4()), "run_key": streams[0], "sensor_run_key": streams[1],
                    "snapshot_version": self.runtime._snapshot()["version"],
                    "message": "Existing native workflows, publication and private viewer verified. Agent deployment is active; conversation acceptance is separate."})
            return self._response(state)
        if state.get("status") == "activating" and state.get("run_key"):
            state = self._poll(state, client, runtime)
        if deploy:
            state = self._activate(state, client, runtime)
        return self._response(state)
