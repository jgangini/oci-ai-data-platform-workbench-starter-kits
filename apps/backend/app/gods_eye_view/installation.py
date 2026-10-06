"""Install the module in its original Resource Manager stack, then bootstrap AIDP."""
import asyncio
import hashlib
import json
import time
from uuid import uuid4, uuid5, NAMESPACE_URL

import oci

from .control_store import ObjectControlStore


RECEIPT_KEY = ".control/modules/installation.json"
MODULE_ID = "gods_eye_view"
CREATE_ADDRESSES = {
    "oci_core_instance.gods_eye_view[0]",
    "oci_identity_dynamic_group.gods_eye_view[0]",
    "oci_identity_policy.gods_eye_view[0]",
    "oci_identity_policy.gods_eye_view_run_command[0]",
}
ACTIVE_JOBS = {"ACCEPTED", "IN_PROGRESS", "CANCELING"}


def check_plan(plan, receipt):
    """Fail closed on drift or any write beyond creating the selected module."""
    if (not isinstance(plan, dict) or not str(plan.get("format_version", "")).startswith("1.")
            or plan.get("errored") or plan.get("complete") is False or plan.get("deferred_changes")):
        raise ValueError("The infrastructure plan is incomplete or unsupported")
    variables = plan.get("variables", {})
    if (variables.get("source_commit_sha", {}).get("value") != receipt["source_commit_sha"]
            or MODULE_ID not in variables.get("enabled_vm_modules", {}).get("value", [])):
        raise ValueError("The infrastructure plan does not match this installation")
    for change in plan.get("resource_drift", []):
        if change.get("change", {}).get("actions") != ["no-op"]:
            raise ValueError("Infrastructure drift must be resolved before installing a module")
    found = set()
    for change in plan.get("resource_changes", []):
        address, actions = change.get("address"), change.get("change", {}).get("actions")
        if change.get("mode") == "data" and actions in (["read"], ["no-op"]):
            continue
        if actions != ["no-op"] and not (address in CREATE_ADDRESSES and actions == ["create"]):
            raise ValueError("The module plan would modify unrelated infrastructure")
        if address in CREATE_ADDRESSES:
            found.add(address)
    if found != CREATE_ADDRESSES:
        raise ValueError("The module plan is missing required viewer resources")


class ModuleInstallation:
    def __init__(self, settings, aidp_factory, verify):
        self.settings, self.aidp_factory, self.verify = settings, aidp_factory, verify
        self.lock, self.task = asyncio.Lock(), None

    def store(self):
        return ObjectControlStore(self.aidp_factory().object_storage, self.settings.objectstorage_namespace,
                                  self.settings.artifacts_bucket_name, prefix=".control/gods_eye_view/install/")

    def read(self):
        return self.store().get_json("operation.json")[0] or {}

    def write(self, **values):
        store = self.store()
        state, etag = store.get_json("operation.json")
        state = {**(state or {}), **values, "updated_at": time.time()}
        store.put_json("operation.json", state, expected_etag=etag, create=etag is None)
        return state

    def receipt(self):
        client, settings = self.aidp_factory(), self.settings
        response = client.object_storage.get_object(settings.objectstorage_namespace, settings.artifacts_bucket_name, RECEIPT_KEY)
        receipt = json.loads(response.data.content)
        expected = {"schema_version": 1, "source_commit_sha": settings.application_commit_sha,
                    "region": settings.aidp_region, "compartment_id": settings.compartment_id,
                    "platform_id": settings.aidp_platform_id, "namespace": settings.objectstorage_namespace,
                    "artifacts_bucket": settings.artifacts_bucket_name, "agent_model_id": settings.agent_model_id}
        if (not isinstance(receipt, dict) or any(receipt.get(key) != value for key, value in expected.items())
                or receipt.get("buckets", {}).get("gold") != settings.gods_eye_control_bucket
                or receipt.get("buckets", {}).get("landing") != settings.bucket_name
                or any(not isinstance(receipt.get(key), str) or not receipt[key] for key in
                       ("stack_id", "project_id", "deployment_id", "workspace_key", "catalog_name", "agent_model_id", "terraform_config_sha256"))):
            raise ValueError("The installation receipt does not match this portal and release")
        return receipt

    def stack(self, manager, receipt):
        response = manager.get_stack(receipt["stack_id"])
        stack = response.data
        tags, variables = stack.freeform_tags or {}, stack.variables or {}
        if (stack.id != receipt["stack_id"] or stack.compartment_id != receipt["compartment_id"]
                or stack.lifecycle_state != "ACTIVE" or tags.get("deployment") != receipt["deployment_id"]
                or tags.get("catalog_item") != receipt["project_id"]
                or variables.get("source_commit_sha") != receipt["source_commit_sha"]
                or str(variables.get("portal_managed_modules", "true")).lower() != "true"):
            raise ValueError("The infrastructure stack identity or source has changed")
        archive = manager.get_stack_tf_config(stack.id).data.content
        if hashlib.sha256(archive).hexdigest() != receipt["terraform_config_sha256"]:
            raise ValueError("The infrastructure source archive has changed")
        return response

    async def status(self, deploy=False):
        async with self.lock:
            state = await asyncio.to_thread(self.read)
            running = self.task is not None and not self.task.done()
            if deploy and not running:
                # ponytail: the portal runs one API process; CAS and OCI job tokens preserve restart recovery.
                # Add a renewable distributed lease before replicating the administrative API.
                await asyncio.to_thread(self.receipt)
                if state.get("status") == "failed":
                    state = await asyncio.to_thread(self.write, attempt=state.get("attempt", 0) + 1,
                        plan_job_id=None, apply_job_id=None, status="activating", enabled=False)
                elif state.get("status") != "activating":
                    state = await asyncio.to_thread(self.write, status="activating", enabled=bool(state.get("enabled")),
                        operation_id=state.get("operation_id") or str(uuid4()), attempt=state.get("attempt", 0),
                        stage="verification" if state.get("enabled") else "infrastructure",
                        message="Checking this installation and its infrastructure.")
                self.task = asyncio.create_task(asyncio.to_thread(self.run))
                self.task.add_done_callback(lambda task: None if task.cancelled() else task.exception())
                running = True
            return {"status": state.get("status", "available"), "enabled": bool(state.get("enabled")),
                    "operation_id": state.get("operation_id"), "stage": state.get("stage"),
                    "message": state.get("message", "Install the private viewer and its shared AIDP resources."),
                    "resumable": state.get("status") == "activating" and not running}

    def job(self, manager, receipt, operation):
        state = self.read()
        field = operation.lower() + "_job_id"
        tags = {"deployment": receipt["deployment_id"], "module": MODULE_ID,
                "module_operation": state["operation_id"], "module_attempt": str(state.get("attempt", 0)), "module_phase": operation}
        key = state.get(field)
        jobs = oci.pagination.list_call_get_all_results(manager.list_jobs, stack_id=receipt["stack_id"]).data
        matches = [job for job in jobs if all((job.freeform_tags or {}).get(k) == v for k, v in tags.items())]
        if len(matches) > 1 or key and matches and matches[0].id != key:
            raise ValueError("Multiple infrastructure jobs claim the same module operation")
        if not key and matches:
            key = matches[0].id
        if any(job.lifecycle_state in ACTIVE_JOBS and job.id != key
               and any((job.freeform_tags or {}).get(k) != v for k, v in tags.items() if k != "module_phase") for job in jobs):
            raise ValueError("Another infrastructure job is active; wait for it before retrying")
        if not key:
            models = oci.resource_manager.models
            details = (models.CreatePlanJobOperationDetails(is_provider_upgrade_required=False) if operation == "PLAN"
                       else models.CreateApplyJobOperationDetails(execution_plan_strategy="FROM_PLAN_JOB_ID",
                                execution_plan_job_id=state["plan_job_id"], is_provider_upgrade_required=False))
            created = manager.create_job(models.CreateJobDetails(stack_id=receipt["stack_id"], operation=operation,
                display_name=f"Install God's Eye View · {operation.lower()}", job_operation_details=details, freeform_tags=tags),
                opc_retry_token=str(uuid5(NAMESPACE_URL, json.dumps(tags, sort_keys=True)))).data
            key = created.id
        self.write(**{field: key})
        return key

    def wait_job(self, manager, receipt, key, operation, deadline):
        state = self.read()
        while time.monotonic() < deadline:
            job = manager.get_job(key).data
            tags = job.freeform_tags or {}
            expected = {"deployment": receipt["deployment_id"], "module": MODULE_ID,
                        "module_operation": state["operation_id"], "module_attempt": str(state.get("attempt", 0)), "module_phase": operation}
            if job.stack_id != receipt["stack_id"] or job.operation != operation or any(tags.get(k) != v for k, v in expected.items()):
                raise ValueError("Infrastructure job does not belong to this module operation")
            if job.lifecycle_state == "SUCCEEDED":
                if operation == "APPLY" and (job.job_operation_details is None
                        or job.job_operation_details.execution_plan_job_id != state["plan_job_id"]):
                    raise ValueError("The apply job used a different infrastructure plan")
                return job
            if job.lifecycle_state not in ACTIVE_JOBS:
                raise RuntimeError("Infrastructure job did not succeed; inspect its Resource Manager log and retry")
            time.sleep(10)
        raise TimeoutError("Infrastructure job is still running; resume installation to continue tracking it")

    def provision(self, manager, receipt, deadline):
        response = self.stack(manager, receipt)
        variables = dict(response.data.variables or {})
        modules = json.loads(variables.get("enabled_vm_modules", "[]"))
        if not isinstance(modules, list) or any(not isinstance(value, str) for value in modules):
            raise ValueError("Invalid enabled module configuration")
        if MODULE_ID not in modules:
            jobs = oci.pagination.list_call_get_all_results(manager.list_jobs, stack_id=receipt["stack_id"]).data
            if any(job.lifecycle_state in ACTIVE_JOBS for job in jobs):
                raise ValueError("Another infrastructure job is active; wait for it before retrying")
            # Update variables only; retain the exact source archive and every other stack setting.
            variables["enabled_vm_modules"] = json.dumps(sorted({*modules, MODULE_ID}))
            manager.update_stack(receipt["stack_id"], oci.resource_manager.models.UpdateStackDetails(variables=variables),
                                 if_match=response.headers["etag"])
        self.write(stage="plan", message="Planning the private viewer and checking that the portal remains unchanged.")
        plan_id = self.job(manager, receipt, "PLAN")
        plan_job = self.wait_job(manager, receipt, plan_id, "PLAN", deadline)
        if dict(plan_job.variables or {}) != variables:
            raise ValueError("Stack variables differ from the saved module plan")
        if hashlib.sha256(manager.get_job_tf_config(plan_id).data.content).hexdigest() != receipt["terraform_config_sha256"]:
            raise ValueError("The module plan was generated from a different infrastructure archive")
        while True:
            try:
                response = manager.get_job_tf_plan(plan_id, tf_plan_format="JSON")
                break
            except oci.exceptions.ServiceError as exc:
                if exc.status != 409 or time.monotonic() + 10 >= deadline:
                    raise
                time.sleep(10)
        check_plan(json.loads(response.data.content), receipt)
        checked = self.stack(manager, receipt)
        if dict(checked.data.variables or {}) != variables:
            raise ValueError("Stack variables changed after the module plan was prepared")
        self.write(stage="infrastructure", message="Creating the private viewer. The administrative VM stays running.")
        apply_id = self.job(manager, receipt, "APPLY")
        self.wait_job(manager, receipt, apply_id, "APPLY", deadline)

    def bootstrap(self, receipt, deadline):
        from post_apply import AidpApi, ensure_workspace_folder
        from gods_eye_view_bootstrap import bootstrap_gods_eye_view

        client = self.aidp_factory()
        context = {"region": receipt["region"], "deployment_id": receipt["deployment_id"]}
        outputs = {"ai_data_platform_id": receipt["platform_id"], "objectstorage_namespace": receipt["namespace"],
                   "medallion_bucket_names": receipt["buckets"], "agent_model_id": receipt["agent_model_id"],
                   "compartment_ocid": receipt["compartment_id"]}
        api = AidpApi(receipt["region"], receipt["platform_id"], client.signer, receipt["deployment_id"])
        try:
            bootstrap_gods_eye_view(api, context, outputs, client._oci_config, client.signer, client.object_storage,
                {"workspace_key": receipt["workspace_key"], "catalog_name": receipt["catalog_name"]},
                deadline=deadline, ensure_folder=ensure_workspace_folder,
                progress=lambda stage, message: self.write(stage=stage, message=message))
        finally:
            api.session.close()

    def run(self):
        try:
            receipt, client = self.receipt(), self.aidp_factory()
            if self.read().get("stage") != "verification":
                manager = oci.resource_manager.ResourceManagerClient({**client._oci_config, "region": receipt["region"]}, signer=client.signer)
                self.provision(manager, receipt, time.monotonic() + 3600)
                self.write(stage="aidp", message="Preparing the shared data workflows and agent.")
                self.bootstrap(receipt, time.monotonic() + 3600)
            self.write(stage="verification", message="Verifying the private viewer, workflows and published data.")
            verified = self.verify()
            if verified.get("status") != "ready" or not verified.get("enabled"):
                raise RuntimeError("Native module readiness has not been confirmed")
            self.write(status="ready", enabled=True, stage="complete", message=verified["message"])
        except Exception as exc:
            state = self.read()
            uncertain = (isinstance(exc, (TimeoutError, oci.exceptions.RequestException))
                         or isinstance(exc, oci.exceptions.ServiceError) and exc.status in {408, 429, 500, 502, 503, 504})
            # Do not expose SDK payloads: they can contain Terraform variables or credentials.
            message = (str(exc) if isinstance(exc, (ValueError, TimeoutError)) else
                       f"Installation stopped during {state.get('stage', 'verification')}. Check the corresponding OCI or AIDP job, then retry.")
            if uncertain:
                message = "The last cloud response could not be confirmed. Resume installation to track the same operation."
            self.write(status="activating" if uncertain else "failed", enabled=False, message=message)
