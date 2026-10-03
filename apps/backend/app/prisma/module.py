"""Activate the global module on infrastructure provisioned by Deploy Studio."""
import asyncio
import json
import os
from urllib.parse import quote
from uuid import uuid4

import httpx
from fastapi import HTTPException

from .agent_gateway import checked_endpoint
from .scheduling import job_path, submit_run

PACKAGE = {"package_id": "territorial_control", "display_name": "Territorial Control · God’s Eye View",
           "bundled_version": "1.0.0", "kind": "module", "scope": "global", "status": "available"}
SUCCESS = {"SUCCESS", "SUCCEEDED"}
FAILED = {"FAILED", "ERROR", "CANCELED", "CANCELLED", "TIMED_OUT", "SKIPPED", "BLOCKED"}


def run_state(document):
    state = document.get("state") or {}
    value = state.get("status") if isinstance(state, dict) else state
    return str(value or document.get("status") or "").upper()


class TerritorialModule:
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
                raise HTTPException(503, "The Territorial Control native runtime is not ready. Check the Deploy Studio post-apply result and retry.") from exc

    def _read(self):
        if self.settings.local_development_mode:
            with self.runtime.store.connection() as db:
                return self.runtime.store._get(db, "status_module", {})
        return self.runtime._doc("status_module")

    def _write(self, values):
        if self.settings.local_development_mode:
            with self.runtime.store.connection() as db:
                self.runtime.store._put(db, "status_module", values)
            return values
        return self.runtime._change("status_module", lambda document: {**document, **values})

    def _response(self, state, **values):
        return {"module_id": PACKAGE["package_id"], "display_name": PACKAGE["display_name"],
                "installed": bool(state.get("enabled")), "enabled": bool(state.get("enabled")),
                "status": state.get("status", "available"), "operation_id": state.get("operation_id"),
                "viewer_url": "/gods-eye-view/", "runtime": "local_fixture" if self.settings.local_development_mode else "aidp",
                "message": state.get("message", "Enable the module to validate its viewer, native workflow and agent."), **values}

    def _prerequisites(self):
        runtime = self.runtime._doc("runtime")
        client = self.runtime.aidp_factory()
        job = client._request("GET", job_path(runtime))
        if not any(task.get("taskKey") == "prisma_tick" for task in job.get("tasks", [])):
            raise HTTPException(503, "The provisioned Territorial Control workflow is incomplete.")
        response = client.object_storage.get_object(runtime["namespace"], runtime["bucket"], ".control/prisma/agent.json")
        metadata = json.loads(response.data.content)
        checked_endpoint(metadata["endpoint"], runtime["region"])
        path = "/workspaces/{}/agents/{}/deployments/{}".format(
            *(quote(str(value), safe="") for value in (runtime["workspace_key"], metadata["agent_key"], metadata["deployment_key"]))
        )
        agent = client._request("GET", path)
        if str(agent.get("lifecycleState") or agent.get("state")) != "ACTIVE":
            raise HTTPException(503, "The Territorial Control agent deployment is not active.")
        viewer = os.environ.get("PRISMA_VIEWER_URL", "").rstrip("/")
        if not viewer:
            raise HTTPException(503, "The private viewer URL is missing; redeploy with the God's Eye View option.")
        with httpx.Client(timeout=10, follow_redirects=False) as http:
            ready = http.get(viewer + "/ready")
            ready.raise_for_status()
        self.runtime._snapshot()
        return client, runtime

    def _poll(self, state, client, runtime):
        base = "/workspaces/" + quote(runtime["workspace_key"], safe="")
        result = client._request("GET", base + "/jobRuns/" + quote(state["run_key"], safe=""))
        status = run_state(result)
        if status in FAILED:
            return self._write({**state, "status": "failed", "enabled": False,
                                "message": "Native Territorial Control activation failed; inspect the AIDP job run before retrying."})
        if status in SUCCESS:
            tasks = client._list(base + "/taskRuns", params={"jobRunKey": state["run_key"]})
            if len(tasks) == 1 and tasks[0].get("taskKey") == "prisma_tick" and run_state(tasks[0]) in SUCCESS:
                snapshot = self.runtime._snapshot()
                return self._write({**state, "status": "ready", "enabled": True, "snapshot_version": snapshot["version"],
                                    "message": "Native workflow, publication, agent and private viewer verified. Configure sources to start capture."})
            if any(run_state(task) in FAILED for task in tasks):
                return self._write({**state, "status": "failed", "enabled": False, "message": "The native Territorial Control task failed."})
        return state

    def _status(self, deploy):
        if not self.settings.prisma_enabled:
            message = "Deploy this release with the God's Eye View option in Deploy Studio before enabling the module."
            if deploy:
                raise HTTPException(409, message)
            return self._response({}, status="deployment_required", message=message)
        state = self._read()
        if self.settings.local_development_mode:
            if deploy and not state.get("enabled"):
                state = self._write({"enabled": True, "status": "ready", "operation_id": str(uuid4()),
                                     "message": "Local simulation module enabled. OCI/AIDP deployment is not claimed."})
            return self._response(state)
        client, runtime = self._prerequisites()
        if state.get("status") == "activating" and state.get("run_key"):
            state = self._poll(state, client, runtime)
        if deploy and not state.get("enabled") and state.get("status") != "activating":
            state = self._write({"status": "activating", "enabled": False, "operation_id": str(uuid4()), "run_key": None,
                                 "message": "Waiting for the native Territorial Control activation run and its publication."})
        if deploy and state.get("status") == "activating" and not state.get("run_key"):
            result = submit_run(client._request, runtime, state["operation_id"])
            if not result.get("key"):
                raise HTTPException(503, "AIDP did not return the activation run key; retry the same operation.")
            state = self._write({**state, "run_key": str(result["key"])})
        return self._response(state)
