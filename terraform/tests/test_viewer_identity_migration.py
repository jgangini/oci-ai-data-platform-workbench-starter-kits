"""Legacy sign-in migration keeps the original stack, VM bytes and infrastructure SHA."""
import base64
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace as NS
from unittest.mock import Mock
import zipfile

import oci
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps/backend"))
sys.path.insert(0, str(ROOT / "scripts"))
import migrate_viewer_identity as migration
from app.config import Settings
from app.gods_eye_view.installation import CREATE_ADDRESSES, ModuleInstallation, RECEIPT_KEY


def archive(identity=b'data "oci_identity_domains" "default" {}\n', duplicate=False):
    value = io.BytesIO()
    with zipfile.ZipFile(value, "w") as bundle:
        bundle.writestr(migration.IDENTITY_FILE, identity)
        bundle.writestr("g_oci_core_instance.tf", b'VM metadata remains unchanged\r\n')
        bundle.writestr("templatefile/user_data.sh", b'root-only configuration\r\n')
        bundle.writestr("runtime.bin", b"original\x00bytes")
        if duplicate:
            bundle.writestr(migration.IDENTITY_FILE, identity)
    return value.getvalue()


@pytest.fixture
def environment():
    settings = Settings(application_commit_sha="b" * 40, aidp_region="region", compartment_id="compartment",
                        aidp_platform_id="platform", objectstorage_namespace="namespace", artifacts_bucket_name="artifacts",
                        bucket_name="landing", gods_eye_control_bucket="gold", agent_model_id="model")
    original = archive()
    receipt = {"schema_version": 1, "source_commit_sha": "a" * 40, "stack_id": "stack", "project_id": "project",
               "deployment_id": "deployment", "workspace_key": "workspace", "catalog_name": "catalog", "agent_model_id": "model",
               "region": "region", "compartment_id": "compartment", "platform_id": "platform", "namespace": "namespace",
               "artifacts_bucket": "artifacts", "buckets": {"landing": "landing", "gold": "gold"},
               "terraform_config_sha256": hashlib.sha256(original).hexdigest()}
    documents = {RECEIPT_KEY: json.dumps(receipt).encode()}
    objects = Mock()
    def get_object(_namespace, _bucket, key):
        if key not in documents:
            raise oci.exceptions.ServiceError(404, "NotFound", {}, "missing")
        return NS(data=NS(content=documents[key]), headers={"etag": hashlib.sha256(documents[key]).hexdigest()})
    def put_object(_namespace, _bucket, key, body, **kwargs):
        assert kwargs.get("if_match") == hashlib.sha256(documents[key]).hexdigest() if key in documents else kwargs.get("if_none_match") == "*"
        documents[key] = body
        return NS(headers={"etag": hashlib.sha256(body).hexdigest()})
    objects.get_object.side_effect, objects.put_object.side_effect = get_object, put_object
    client = NS(object_storage=objects)
    installer = ModuleInstallation(settings, lambda: client, None)
    variables = {"source_commit_sha": "a" * 40, "enabled_vm_modules": '["gods_eye_view"]', "retained": "original"}
    stack = NS(id="stack", compartment_id="compartment", lifecycle_state="ACTIVE", variables=variables,
               freeform_tags={"deployment": "deployment", "catalog_item": "project"}, config_source=NS(working_directory=""))
    manager = Mock()
    manager.archive = original
    manager.get_stack.return_value = NS(data=stack, headers={"etag": "stack-etag"})
    manager.get_stack_tf_config.side_effect = lambda _: NS(data=NS(content=manager.archive))
    manager.list_jobs.__name__ = "list_jobs"
    manager.list_jobs.return_value = oci.response.Response(200, {}, [], None)
    def update_stack(key, details, **kwargs):
        assert key == "stack" and kwargs == {"if_match": "stack-etag"} and details.variables is None
        manager.archive = base64.b64decode(details.config_source.zip_file_base64_encoded)
    manager.update_stack.side_effect = update_stack
    return installer, manager, documents, receipt, variables, original


@pytest.mark.parametrize("interrupted", [False, True])
def test_migration_is_dry_run_by_default_and_resumes_conditional_receipt_write(environment, interrupted):
    installer, manager, documents, receipt, variables, original = environment
    assert migration.original_receipt(installer)["source_commit_sha"] == "a" * 40
    assert migration.migrate(installer, manager).startswith("Verified")
    assert manager.archive == original and len(documents) == 1
    objects = installer.store().objects
    put = objects.put_object.side_effect
    failures = []
    def put_with_conflict(namespace, bucket, key, body, **kwargs):
        if key == RECEIPT_KEY and interrupted and not failures:
            failures.append(True)
            raise oci.exceptions.ServiceError(412, "PreconditionFailed", {}, "changed")
        return put(namespace, bucket, key, body, **kwargs)
    objects.put_object.side_effect = put_with_conflict
    if interrupted:
        with pytest.raises(oci.exceptions.ServiceError):
            migration.migrate(installer, manager, True)
    assert migration.migrate(installer, manager, True).startswith("Migration verified")
    assert migration.migrate(installer, manager, True).startswith("Migration verified")
    assert manager.update_stack.call_count == 1 and not manager.create_job.called
    assert manager.get_stack.return_value.data.variables == variables
    backup = installer.store().get_json(migration.KEY)[0]
    assert backup["receipt"] == receipt and base64.b64decode(backup["original_archive"]) == original
    with zipfile.ZipFile(io.BytesIO(original)) as before, zipfile.ZipFile(io.BytesIO(manager.archive)) as after:
        assert before.namelist() == after.namelist()
        assert all(before.read(name) == after.read(name) for name in before.namelist() if name != migration.IDENTITY_FILE)
        assert after.read(migration.IDENTITY_FILE).startswith(before.read(migration.IDENTITY_FILE))
        assert migration.identity_blocks().encode() in after.read(migration.IDENTITY_FILE)


@pytest.mark.parametrize("change", ["variables", "archive", "active_job", "receipt"])
def test_migration_refuses_changed_scope_or_active_jobs(environment, change):
    installer, manager, documents, _, _, _ = environment
    migration.migrate(installer, manager, True)
    if change == "variables":
        manager.get_stack.return_value.data.variables["retained"] = "changed"
    elif change == "archive":
        manager.archive = archive(b"foreign source")
    elif change == "active_job":
        manager.list_jobs.return_value.data = [NS(lifecycle_state="IN_PROGRESS")]
    else:
        value = json.loads(documents[RECEIPT_KEY])
        documents[RECEIPT_KEY] = json.dumps({**value, "compartment_id": "foreign"}).encode()
    manager.update_stack.reset_mock()
    with pytest.raises(ValueError):
        migration.migrate(installer, manager, True)
    assert not manager.update_stack.called and not manager.create_job.called


def identity_plan():
    values = {
        "oci_identity_domains_group.gods_eye_view_readers": {"display_name": "aidp-viewer-readers-abcd", "external_id": "aidp-lab-abcd:gods_eye_view"},
        "oci_identity_domains_app.viewer": {"name": "aidp_viewer_abcd", "display_name": "Starter Kits viewer abcd", "active": True,
            "is_oauth_client": True, "client_type": "public", "allowed_grants": ["authorization_code"],
            "based_on_template": [{"value": "CustomBrowserMobileTemplateId"}],
            "redirect_uris": ["https://portal.example/api/auth/oci/callback"]}}
    return {"format_version": "1.2", "variables": {"source_commit_sha": {"value": "a" * 40}, "enabled_vm_modules": {"value": ["gods_eye_view"]}},
            "resource_changes": [{"address": address, "mode": "managed", "change": {"actions": ["no-op"]}} for address in CREATE_ADDRESSES]
            + [{"address": address, "mode": "managed", "change": {"actions": ["create"], "after": value}} for address, value in values.items()]}


def test_plan_only_allows_two_identity_creates_and_preserves_input():
    plan = identity_plan()
    before = copy.deepcopy(plan)
    migration.validate_plan(plan, {"source_commit_sha": "a" * 40}, "aidp-lab-abcd", "https://portal.example")
    assert plan == before
    for row in plan["resource_changes"]:
        row["change"]["actions"] = ["no-op"]
    migration.validate_plan(plan, {"source_commit_sha": "a" * 40}, "aidp-lab-abcd", "https://portal.example")


@pytest.mark.parametrize("field,value", [("client_type", "confidential"), ("allowed_grants", ["authorization_code", "client_credentials"]),
                                        ("redirect_uris", ["https://foreign.example/api/auth/oci/callback"]), ("name", "foreign")])
def test_plan_rejects_wrong_or_privileged_app(field, value):
    plan = identity_plan()
    plan["resource_changes"][-1]["change"]["after"][field] = value
    with pytest.raises(ValueError):
        migration.validate_plan(plan, {"source_commit_sha": "a" * 40}, "aidp-lab-abcd", "https://portal.example")


@pytest.mark.parametrize("field,value", [("based_on_template", [{"value": "CustomEnterpriseAppTemplateId"}]),
    ("admin_roles", [{"value": "admin"}]), ("app_roles", [{"value": "admin"}]), ("granted_app_roles", [{"value": "admin"}]),
    ("allowed_operations", ["introspect"]), ("grants", [{"value": "admin"}]), ("user_roles", [{"value": "admin"}]),
    ("client_secret", "not-a-real-secret")])
def test_plan_rejects_privileges_and_nonpublic_template(field, value):
    plan = identity_plan()
    plan["resource_changes"][-1]["change"]["after"][field] = value
    with pytest.raises(ValueError):
        migration.validate_plan(plan, {"source_commit_sha": "a" * 40}, "aidp-lab-abcd", "https://portal.example")


@pytest.mark.parametrize("unknown", [{"name": True}, {"allowed_grants": [False, True]},
    {"redirect_uris": [{"value": True}]}, {"based_on_template": [{"value": True}]},
    {"based_on_template": True}, {"based_on_template": [True]}])
def test_plan_rejects_unknown_configured_security_fields(unknown):
    plan = identity_plan()
    plan["resource_changes"][-1]["change"]["after_unknown"] = unknown
    with pytest.raises(ValueError, match="unknown"):
        migration.validate_plan(plan, {"source_commit_sha": "a" * 40}, "aidp-lab-abcd", "https://portal.example")


@pytest.mark.parametrize("actions", [["create"], ["no-op"]])
def test_plan_tolerates_unset_provider_computed_defaults_but_not_unknown_configuration(actions):
    plan = identity_plan()
    app = plan["resource_changes"][-1]["change"]
    app["actions"] = actions
    app["after"].update(admin_roles=None, app_roles=[], allowed_operations=None, client_secret=None)
    app["after"]["based_on_template"][0].update(ref="https://identity.example/Templates/template", last_modified="date", well_known_id="public-template")
    app["after_unknown"] = {"admin_roles": True, "granted_app_roles": True, "allowed_operations": True,
                            "client_secret": True, "grants": True, "user_roles": True, "allowed_scopes": True,
                            "allowed_grants": [False], "redirect_uris": [False],
                            "based_on_template": [{"last_modified": True, "ref": True, "well_known_id": True}]}
    migration.validate_plan(plan, {"source_commit_sha": "a" * 40}, "aidp-lab-abcd", "https://portal.example")


@pytest.mark.parametrize("actions", [["update"], ["delete", "create"], ["create"]])
def test_plan_rejects_any_unrelated_write(actions):
    plan = identity_plan()
    plan["resource_changes"].append({"address": "oci_core_instance.lab", "mode": "managed", "change": {"actions": actions}})
    with pytest.raises(ValueError):
        migration.validate_plan(plan, {"source_commit_sha": "a" * 40}, "aidp-lab-abcd", "https://portal.example")


@pytest.mark.parametrize("changes", [
    {"complete": False}, {"errored": True}, {"deferred_changes": [{}]}, {"resource_changes": []},
    {"variables": {"source_commit_sha": {"value": "b" * 40}}},
    {"resource_drift": [{"address": "oci_core_instance.lab", "change": {"actions": ["update"]}}]},
])
def test_plan_reuses_existing_incomplete_source_and_drift_guards(changes):
    with pytest.raises(ValueError):
        migration.validate_plan({**identity_plan(), **changes}, {"source_commit_sha": "a" * 40}, "aidp-lab-abcd", "https://portal.example")


@pytest.mark.parametrize("changes", [{"actions": ["update"]}, {"actions": ["delete", "create"]},
                                     {"after": {"display_name": "foreign", "external_id": "foreign:gods_eye_view"}}])
def test_plan_rejects_identity_replacement_update_or_foreign_group(changes):
    plan = identity_plan()
    plan["resource_changes"][-2]["change"].update(changes)
    with pytest.raises(ValueError):
        migration.validate_plan(plan, {"source_commit_sha": "a" * 40}, "aidp-lab-abcd", "https://portal.example")


def test_archive_rejects_preexisting_identity_resources_and_duplicate_members():
    with pytest.raises(ValueError, match="already declares"):
        migration.patch_archive(archive(migration.identity_blocks().encode()))
    with pytest.warns(UserWarning, match="Duplicate"):
        duplicate = archive(duplicate=True)
    with pytest.raises(ValueError, match="duplicate"):
        migration.patch_archive(duplicate)
