"""Add tracked viewer sign-in resources to a verified legacy stack; never apply Terraform."""
import argparse
import base64
import copy
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import re
import zipfile

import oci

from app.gods_eye_view.installation import ACTIVE_JOBS, ModuleInstallation, RECEIPT_KEY, check_plan


KEY = "viewer-identity-migration.json"
IDENTITY_FILE = "i_oci_identity.tf"
ADDRESSES = {"oci_identity_domains_group.gods_eye_view_readers", "oci_identity_domains_app.viewer"}


def identity_blocks():
    source = (Path(__file__).resolve().parents[1] / "terraform" / IDENTITY_FILE).read_text(encoding="utf-8")
    blocks = []
    for address in sorted(ADDRESSES):
        kind, name = address.split(".")
        marker = f'resource "{kind}" "{name}"'
        if source.count(marker) != 1:
            raise ValueError("Canonical viewer identity source is missing or ambiguous")
        start = source.index(marker)
        end = source.find('\nresource "', start + len(marker))
        blocks.append(source[start:end if end >= 0 else None].strip())
    return "\n\n".join(blocks) + "\n"


def patch_archive(archive):
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(archive)) as source, zipfile.ZipFile(output, "w") as target:
        names = source.namelist()
        if len(names) != len(set(names)) or IDENTITY_FILE not in names:
            raise ValueError("The source archive has duplicate or missing identity files")
        for entry in source.infolist():
            content = source.read(entry)
            if entry.filename.endswith(".tf") and any(
                    re.search(fr'resource\s+"{kind}"\s+"{name}"'.encode(), content)
                    for kind, name in (address.split(".") for address in ADDRESSES)):
                raise ValueError("The source already declares viewer identity resources; inspect the existing deployment")
            if entry.filename == IDENTITY_FILE:
                newline = "\r\n" if b"\r\n" in content else "\n"
                content += (newline * 2 + identity_blocks().replace("\n", newline)).encode("utf-8")
            target.writestr(entry, content)
        target.comment = source.comment
    return output.getvalue()


def original_receipt(installer):
    """An image update does not change the original infrastructure source identity."""
    client, settings = installer.aidp_factory(), installer.settings
    response = client.object_storage.get_object(settings.objectstorage_namespace, settings.artifacts_bucket_name, RECEIPT_KEY)
    receipt = json.loads(response.data.content)
    source = receipt.get("source_commit_sha", "")
    if not isinstance(source, str) or re.fullmatch(r"[0-9a-f]{40}", source) is None:
        raise ValueError("The original infrastructure source is invalid")
    scoped = ModuleInstallation(replace(settings, application_commit_sha=source), installer.aidp_factory, None)
    return scoped.receipt()


def prepare_migration(installer, manager):
    record, _ = installer.store().get_json(KEY)
    if record:
        return record
    receipt = original_receipt(installer)
    response = installer.stack(manager, receipt)
    archive = manager.get_stack_tf_config(receipt["stack_id"]).data.content
    return {"receipt": receipt, "original_archive": base64.b64encode(archive).decode(),
            "variables_sha256": hashlib.sha256(json.dumps(response.data.variables, sort_keys=True).encode()).hexdigest()}


def verify_migration(installer, manager, record, updated):
    """Recheck the saved source migration against live ownership, variables and jobs."""
    receipt = record["receipt"]
    current = original_receipt(installer)
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
    return current, live_hash, response


def migrate(installer, manager, apply=False):
    store = installer.store()
    record = prepare_migration(installer, manager)
    receipt = record["receipt"]
    archive = base64.b64decode(record["original_archive"], validate=True)
    if hashlib.sha256(archive).hexdigest() != receipt["terraform_config_sha256"]:
        raise ValueError("The migration backup does not match the original receipt")
    patched = patch_archive(archive)
    updated = {**receipt, "terraform_config_sha256": hashlib.sha256(patched).hexdigest()}
    current, live_hash, response = verify_migration(installer, manager, record, updated)
    if not apply:
        return "Verified: only the reader group and public viewer app source will be added; no resources will be applied"
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
    return "Migration verified; original archive/receipt retained and variables unchanged. Validate a new identity plan before applying"


def _unknown(value):
    if isinstance(value, dict):
        return any(_unknown(item) for item in value.values())
    if isinstance(value, list):
        return any(_unknown(item) for item in value)
    return value is True


def _check_identity_resource(row, expected):
    change = row.get("change", {})
    if change.get("actions") not in (["create"], ["no-op"]):
        raise ValueError("The identity migration cannot update, replace or delete resources")
    if any(change.get("after", {}).get(key) != value for key, value in expected.items()):
        raise ValueError("The identity plan does not match the scoped public viewer configuration")
    if any(_unknown(change.get("after_unknown", {}).get(key)) for key in expected):
        raise ValueError("The identity plan has unknown security configuration")
    change["actions"] = ["no-op"]


def _check_public_app(change):
    after = change.get("after", {})
    templates = after.get("based_on_template", [])
    if not isinstance(templates, list) or len(templates) != 1 or not isinstance(templates[0], dict) or templates[0].get("value") != "CustomBrowserMobileTemplateId":
        raise ValueError("The viewer app must use the public browser/mobile template")
    unknown = change.get("after_unknown", {}).get("based_on_template")
    if _unknown([item.get("value") if isinstance(item, dict) else item for item in unknown] if isinstance(unknown, list) else unknown):
        raise ValueError("The viewer app template is unknown")
    privileges = ("admin_roles", "app_roles", "granted_app_roles", "allowed_operations", "grants", "user_roles", "client_secret")
    if any(after.get(key) for key in privileges):
        raise ValueError("The viewer app must not have provisioning privileges or a client secret")


def validate_plan(plan, receipt, lab_marker, portal_url):
    """Use only on the exact migrated archive and unchanged variables, before FROM_PLAN_JOB_ID apply."""
    if re.fullmatch(r"aidp-lab-[a-z0-9]{4,12}", lab_marker) is None or re.fullmatch(r"https://[A-Za-z0-9.-]+", portal_url) is None:
        raise ValueError("Invalid deployment identity or public portal origin")
    checked = copy.deepcopy(plan)
    rows = [row for row in checked.get("resource_changes", []) if row.get("address") in ADDRESSES]
    if len(rows) != 2 or {row["address"] for row in rows} != ADDRESSES:
        raise ValueError("The identity plan is missing or duplicates required resources")
    suffix = lab_marker.removeprefix("aidp-lab-")
    expected = {
        "oci_identity_domains_group.gods_eye_view_readers": {
            "display_name": f"aidp-viewer-readers-{suffix}", "external_id": f"{lab_marker}:gods_eye_view"},
        "oci_identity_domains_app.viewer": {
            "name": f"aidp_viewer_{suffix}", "display_name": f"Starter Kits viewer {suffix}", "active": True,
            "is_oauth_client": True, "client_type": "public", "allowed_grants": ["authorization_code"],
            "redirect_uris": [portal_url + "/api/auth/oci/callback"]}}
    for row in rows:
        _check_identity_resource(row, expected[row["address"]])
    app = next(row for row in rows if row["address"] == "oci_identity_domains_app.viewer")
    _check_public_app(app["change"])
    for row in checked.get("resource_changes", []):
        if row.get("mode") != "data" and row.get("change", {}).get("actions") != ["no-op"]:
            raise ValueError("The identity plan would modify unrelated infrastructure")
    check_plan(checked, receipt)


if __name__ == "__main__":
    from app.config import Settings
    from app.aidp import AidpClient

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Migrate only the verified stack source and receipt")
    args = parser.parse_args()
    settings = Settings.from_env()
    client = AidpClient(settings)
    installer = ModuleInstallation(settings, lambda: client, None)
    manager = oci.resource_manager.ResourceManagerClient(dict(client._oci_config, region=settings.aidp_region), signer=client.signer)
    try:
        print(migrate(installer, manager, args.apply))
    except Exception as exc:
        print(str(exc) if isinstance(exc, ValueError) else "Migration stopped: " + type(exc).__name__)
        raise SystemExit(1) from None
