"""Explicit, resumable migration of the two Oracle automatic tag defaults; never apply Terraform."""
import argparse
import base64
import copy
import hashlib
import io
import json
import time
import zipfile

import oci

from app.gods_eye_view.installation import ACTIVE_JOBS, RECEIPT_KEY, check_plan


KEY = "provider-tags-migration.json"
TAGS = {"Oracle-Tags.CreatedBy", "Oracle-Tags.CreatedOn"}
PROVIDERS = {"a_versions.tf": 'provider "oci" {\n  region = var.region\n}',
             "d_main.tf": 'provider "oci" {\n  alias  = "home"\n  region = var.home_region\n}'}


def patch_archive(archive):
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(archive)) as source, zipfile.ZipFile(output, "w") as target:
        if len(source.namelist()) != len(set(source.namelist())) or not set(PROVIDERS) <= set(source.namelist()):
            raise ValueError("The source archive has duplicate or missing provider files")
        for entry in source.infolist():
            content = source.read(entry)
            if entry.filename in PROVIDERS:
                newline = "\r\n" if b"\r\n" in content else "\n"
                original = PROVIDERS[entry.filename].replace("\n", newline).encode()
                if content.count(original) != 1 or b"ignore_defined_tags" in content:
                    raise ValueError("The provider source is not the supported original configuration")
                replacement = original[:-1] + b'  ignore_defined_tags = ["Oracle-Tags.CreatedBy", "Oracle-Tags.CreatedOn"]' + newline.encode() + b"}"
                content = content.replace(original, replacement)
            target.writestr(entry, content)
        target.comment = source.comment
    return output.getvalue()


def validate_old_plan(plan, receipt):
    checked = copy.deepcopy(plan)
    rows = [item for item in checked.get("resource_changes", []) if item.get("address") == "oci_core_instance.lab"]
    if len(rows) != 1 or rows[0].get("change", {}).get("actions") != ["update"]:
        raise ValueError("The saved plan is not the automatic portal-tag removal")
    change = rows[0]["change"]
    expected = copy.deepcopy(change.get("before"))
    if not isinstance(expected, dict) or not isinstance(expected.get("defined_tags"), dict) or not TAGS <= expected["defined_tags"].keys():
        raise ValueError("The saved plan is missing the automatic tags")
    expected["defined_tags"] = {key: value for key, value in expected["defined_tags"].items() if key not in TAGS}
    if expected != change.get("after") or change.get("after_unknown") or change.get("replace_paths"):
        raise ValueError("The saved plan also changes portal configuration")
    change["actions"] = ["no-op"]
    check_plan(checked, receipt)


def prepare_migration(installer, manager):
    state = installer.read()
    if state.get("status") != "failed" or state.get("stage") != "plan" or state.get("apply_job_id"):
        raise ValueError("Migration requires a failed plan with no apply job")
    record, _ = installer.store().get_json(KEY)
    if record:
        return record
    receipt = installer.receipt()
    response = installer.stack(manager, receipt)
    job = installer.wait_job(manager, receipt, state["plan_job_id"], "PLAN", time.monotonic() + 1)
    if dict(job.variables or {}) != dict(response.data.variables or {}):
        raise ValueError("The stack variables no longer match the failed plan")
    archive = manager.get_stack_tf_config(receipt["stack_id"]).data.content
    if hashlib.sha256(manager.get_job_tf_config(job.id).data.content).hexdigest() != receipt["terraform_config_sha256"]:
        raise ValueError("The saved plan used a different source archive")
    validate_old_plan(json.loads(manager.get_job_tf_plan(job.id, tf_plan_format="JSON").data.content), receipt)
    return {"receipt": receipt, "original_archive": base64.b64encode(archive).decode(),
            "variables_sha256": hashlib.sha256(json.dumps(response.data.variables, sort_keys=True).encode()).hexdigest()}


def migrate(installer, manager, apply=False):
    store = installer.store()
    record = prepare_migration(installer, manager)
    receipt = record["receipt"]
    archive = base64.b64decode(record["original_archive"], validate=True)
    if hashlib.sha256(archive).hexdigest() != receipt["terraform_config_sha256"]:
        raise ValueError("The migration backup does not match the original receipt")
    patched = patch_archive(archive)
    updated = {**receipt, "terraform_config_sha256": hashlib.sha256(patched).hexdigest()}
    current = installer.receipt()
    if current not in (receipt, updated):
        raise ValueError("The installation receipt changed during migration")
    live_hash = hashlib.sha256(manager.get_stack_tf_config(receipt["stack_id"]).data.content).hexdigest()
    if live_hash not in (receipt["terraform_config_sha256"], updated["terraform_config_sha256"]):
        raise ValueError("The infrastructure source changed outside this migration")
    response = installer.stack(manager, {**receipt, "terraform_config_sha256": live_hash})
    if hashlib.sha256(json.dumps(response.data.variables, sort_keys=True).encode()).hexdigest() != record["variables_sha256"]:
        raise ValueError("The stack variables changed during migration")
    jobs = oci.pagination.list_call_get_all_results(manager.list_jobs, stack_id=receipt["stack_id"]).data
    if any(job.lifecycle_state in ACTIVE_JOBS for job in jobs):
        raise ValueError("Another infrastructure job is active")
    if not apply:
        return "Verified: only the two OCI provider blocks will preserve automatic tags; no resources will be applied"
    if store.get_json(KEY)[0] is None:
        store.put_json(KEY, record, create=True)
    scope = (receipt["namespace"], receipt["artifacts_bucket"], RECEIPT_KEY)
    stored = store.objects.get_object(*scope)
    if json.loads(stored.data.content) != current:
        raise ValueError("The installation receipt changed before migration")
    if live_hash == receipt["terraform_config_sha256"]:
        manager.update_stack(receipt["stack_id"], oci.resource_manager.models.UpdateStackDetails(
            config_source=oci.resource_manager.models.UpdateZipUploadConfigSourceDetails(
                working_directory=response.data.config_source.working_directory,
                zip_file_base64_encoded=base64.b64encode(patched).decode())), if_match=response.headers["etag"])
        installer.stack(manager, updated)
    if current == receipt:
        store.objects.put_object(*scope, json.dumps(updated, sort_keys=True).encode(), content_type="application/json",
                                 if_match=stored.headers["etag"])
    return "Migration verified; original archive and receipt retained, variables/resources unchanged. Generate a new plan before applying"


if __name__ == "__main__":
    from app.config import Settings
    from app.aidp import AidpClient
    from app.gods_eye_view.installation import ModuleInstallation

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Migrate only the verified provider configuration and receipt")
    args = parser.parse_args()
    settings = Settings.from_env()
    client = AidpClient(settings)
    installer = ModuleInstallation(settings, lambda: client, None)
    manager = oci.resource_manager.ResourceManagerClient(dict(client._oci_config, region=settings.aidp_region), signer=client.signer)
    try:
        print(migrate(installer, manager, args.apply))
    except Exception as exc:
        # SDK payloads can include Terraform variables and the source archive.
        print(str(exc) if isinstance(exc, ValueError) else "Migration stopped: " + type(exc).__name__)
        raise SystemExit(1) from None
