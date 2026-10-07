import asyncio
import hashlib
import json
import io
from pathlib import Path
import time
import zipfile
from dataclasses import replace
from threading import Event
from types import SimpleNamespace as NS

import pytest
import oci

from app.config import Settings
from app.gods_eye_view.installation import CREATE_ADDRESSES, ModuleInstallation, RECEIPT_KEY, check_plan
from test_gods_eye_view_control_store import Objects


ARCHIVE = b"immutable terraform archive"
RECEIPT = {
    "schema_version": 1, "source_commit_sha": "a" * 40, "stack_id": "ocid1.ormstack.fixture",
    "deployment_id": "deployment", "project_id": "starter-kits", "platform_id": "platform",
    "compartment_id": "compartment", "region": "us-chicago-1", "namespace": "namespace",
    "artifacts_bucket": "artifacts", "buckets": {"landing": "landing", "gold": "gold"},
    "workspace_key": "workspace", "catalog_name": "catalog", "agent_model_id": "model",
    "terraform_config_sha256": hashlib.sha256(ARCHIVE).hexdigest(),
}


def plan():
    return {"format_version": "1.2", "variables": {
        "source_commit_sha": {"value": RECEIPT["source_commit_sha"]},
        "enabled_vm_modules": {"value": ["gods_eye_view"]}},
        "resource_changes": [{"address": address, "mode": "managed", "change": {"actions": ["create"]}}
                             for address in sorted(CREATE_ADDRESSES)]}


class Manager:
    def __init__(self):
        self.variables = {"source_commit_sha": RECEIPT["source_commit_sha"], "keep": "unchanged"}
        self.jobs, self.updates, self.creates = [], [], []
        self.plan = plan()
        self.archive = ARCHIVE
        self.lose_response = None

    def get_stack(self, key):
        return NS(data=NS(id=key, compartment_id="compartment", lifecycle_state="ACTIVE", variables=dict(self.variables),
                         freeform_tags={"deployment": "deployment", "catalog_item": "starter-kits"}), headers={"etag": "revision"})

    def get_stack_tf_config(self, _):
        return NS(data=NS(content=self.archive))

    get_job_tf_config = get_stack_tf_config

    def update_stack(self, key, details, **kwargs):
        assert kwargs == {"if_match": "revision"} and details.config_source is None
        self.updates.append(key)
        self.variables = details.variables

    def list_jobs(self, *, stack_id, **_):
        assert stack_id == RECEIPT["stack_id"]
        return oci.response.Response(200, {}, list(self.jobs), None)

    def create_job(self, details, **kwargs):
        if details.operation == "APPLY":
            assert details.job_operation_details.execution_plan_strategy == "FROM_PLAN_JOB_ID"
            assert details.job_operation_details.execution_plan_job_id == self.jobs[0].id
        assert kwargs.get("opc_retry_token") and details.job_operation_details.is_provider_upgrade_required is False
        key = details.operation + str(len(self.jobs))
        result = NS(id=key, stack_id=details.stack_id, lifecycle_state="SUCCEEDED", freeform_tags=details.freeform_tags,
                    variables=dict(self.variables), operation=details.operation,
                    job_operation_details=oci.resource_manager.models.ApplyJobOperationDetails(
                        execution_plan_job_id=self.jobs[0].id) if details.operation == "APPLY" else None)
        self.jobs.append(result)
        self.creates.append(details.operation)
        if details.operation == self.lose_response:
            self.lose_response = None
            raise TimeoutError("lost response")
        return NS(data=result)

    def get_job(self, key):
        return NS(data=next(job for job in self.jobs if job.id == key))

    def get_job_tf_plan(self, key, **kwargs):
        assert kwargs == {"tf_plan_format": "JSON"}
        return NS(data=NS(content=json.dumps(self.plan).encode()))


def installer():
    objects = Objects()
    objects.put_object("namespace", "artifacts", RECEIPT_KEY, json.dumps(RECEIPT).encode(), if_none_match="*")
    settings = Settings(portal_managed_modules=True, gods_eye_view_enabled=True, application_commit_sha="a" * 40,
        aidp_region="us-chicago-1", aidp_platform_id="platform", compartment_id="compartment",
        objectstorage_namespace="namespace", artifacts_bucket_name="artifacts", bucket_name="landing", gods_eye_control_bucket="gold")
    settings = replace(settings, agent_model_id="model")
    client = NS(object_storage=objects, _oci_config={}, signer=None)
    result = ModuleInstallation(settings, lambda: client, lambda: {"status": "ready", "enabled": True, "message": "verified"})
    result.write(operation_id="operation", attempt=0, status="activating", enabled=False)
    return result


def test_only_module_creates_or_unchanged_resources_are_accepted():
    allowed = plan()
    allowed["resource_changes"].append({"address": "oci_core_instance.lab", "change": {"actions": ["no-op"]}})
    check_plan(allowed, RECEIPT)
    for row in allowed["resource_changes"]:
        row["change"]["actions"] = ["no-op"]
    check_plan(allowed, RECEIPT)


@pytest.mark.parametrize("actions", [["delete"], ["update"], ["delete", "create"], ["create", "delete"], [], None])
def test_destructive_or_unknown_plan_actions_never_apply(actions):
    value = plan()
    value["resource_changes"][0]["change"]["actions"] = actions
    with pytest.raises(ValueError):
        check_plan(value, RECEIPT)


@pytest.mark.parametrize("change", [
    {"resource_drift": [{"change": {"actions": ["update"]}}]}, {"complete": False}, {"errored": True},
    {"deferred_changes": [{}]}, {"format_version": "2.0"}, {"resource_changes": []},
    {"variables": {"source_commit_sha": {"value": "other"}}},
])
def test_incomplete_drifted_or_wrong_release_plan_is_rejected(change):
    with pytest.raises(ValueError):
        check_plan({**plan(), **change}, RECEIPT)


@pytest.mark.parametrize("resource_type,before,after", [
    ("oci_ai_data_platform_ai_data_platform", {"ai_feature_status": "IN_PROGRESS", "time_updated": "old"},
     {"ai_feature_status": "ENABLED", "time_updated": "new"}),
    ("oci_database_autonomous_database", {"actual_used_data_storage_size_in_tbs": 0, "apex_details": [{"apex_version": "old"}]},
     {"actual_used_data_storage_size_in_tbs": 1, "apex_details": [{"apex_version": "new"}]}),
    ("oci_database_autonomous_database", {"whitelisted_ips": None, "is_backup_retention_locked": None, "is_reconnect_clone_enabled": None},
     {"whitelisted_ips": [], "is_backup_retention_locked": False, "is_reconnect_clone_enabled": False}),
    ("oci_objectstorage_bucket", {"metadata": None, "approximate_count": 0, "approximate_size": 0},
     {"metadata": {}, "approximate_count": 2, "approximate_size": 100}),
    ("oci_objectstorage_bucket", {"etag": "old"}, {"etag": "new"}),
    ("oci_identity_domains_group", {"urnietfparamsscimschemasoracleidcsextensiondynamic_group": []},
     {"urnietfparamsscimschemasoracleidcsextensiondynamic_group": [{"membership_rule": "", "membership_type": "static"}]}),
])
def test_provider_observations_are_accepted_only_without_planned_writes(resource_type, before, after):
    value = plan()
    address = resource_type + ".existing"
    value["resource_drift"] = [{"address": address, "type": resource_type,
        "change": {"actions": ["update"], "before": {"id": "existing", **before}, "after": {"id": "existing", **after}}}]
    row = {"address": address, "change": {"actions": ["no-op"]}}
    value["resource_changes"].append(row)
    check_plan(value, RECEIPT)
    row["change"]["actions"] = ["update"]
    with pytest.raises(ValueError, match="drift"):
        check_plan(value, RECEIPT)
    value["resource_changes"].remove(row)
    with pytest.raises(ValueError, match="drift"):
        check_plan(value, RECEIPT)


@pytest.mark.parametrize("resource_type,key,before,after", [
    ("oci_core_instance", "shape", "small", "large"),
    ("oci_ai_data_platform_ai_data_platform", "id", "existing", "foreign"),
    ("oci_database_autonomous_database", "whitelisted_ips", [], ["192.0.2.10"]),
    ("oci_database_autonomous_database", "is_backup_retention_locked", True, False),
    ("oci_objectstorage_bucket", "metadata", {"owner": "existing"}, {}),
    ("oci_identity_domains_group", "urnietfparamsscimschemasoracleidcsextensiondynamic_group", [],
     [{"membership_rule": "all users", "membership_type": "dynamic"}]),
])
def test_ignored_configuration_or_security_drift_is_still_rejected(resource_type, key, before, after):
    value = plan()
    address = resource_type + ".existing"
    value["resource_drift"] = [{"address": address, "type": resource_type,
        "change": {"actions": ["update"], "before": {key: before}, "after": {key: after}}}]
    value["resource_changes"].append({"address": address, "change": {"actions": ["no-op"]}})
    with pytest.raises(ValueError, match="drift") as error:
        check_plan(value, RECEIPT)
    assert "all users" not in str(error.value) and "192.0.2.10" not in str(error.value)


def test_plan_then_exact_apply_preserves_every_other_variable_and_reuses_jobs():
    install, manager = installer(), Manager()
    install.provision(manager, install.receipt(), time.monotonic() + 10)
    install.provision(manager, install.receipt(), time.monotonic() + 10)
    assert manager.creates == ["PLAN", "APPLY"]
    assert len(manager.updates) == 1 and manager.variables["keep"] == "unchanged"
    assert json.loads(manager.variables["enabled_vm_modules"]) == ["gods_eye_view"]


@pytest.mark.parametrize("operation", ["PLAN", "APPLY"])
def test_lost_create_response_resumes_exact_job_without_duplicate(operation):
    install, manager = installer(), Manager()
    manager.lose_response = operation
    with pytest.raises(TimeoutError):
        install.provision(manager, install.receipt(), time.monotonic() + 10)
    install.provision(manager, install.receipt(), time.monotonic() + 10)
    assert manager.creates == ["PLAN", "APPLY"]


def test_unrelated_update_blocks_apply_and_foreign_active_job_blocks_stack_edit():
    install, manager = installer(), Manager()
    manager.plan["resource_changes"].append({"address": "oci_core_instance.lab", "change": {"actions": ["update"]}})
    with pytest.raises(ValueError, match="unrelated"):
        install.provision(manager, install.receipt(), time.monotonic() + 10)
    assert manager.creates == ["PLAN"]
    other = Manager()
    other.jobs.append(NS(id="foreign", lifecycle_state="IN_PROGRESS", freeform_tags={}))
    with pytest.raises(ValueError, match="Another"):
        install.provision(other, install.receipt(), time.monotonic() + 10)
    assert not other.updates and not other.creates


@pytest.mark.parametrize("field,value", [("application_commit_sha", "b" * 40), ("compartment_id", "other"),
                                      ("gods_eye_control_bucket", "other"), ("aidp_platform_id", "other"), ("agent_model_id", "other")])
def test_receipt_cannot_be_reused_in_another_installation(field, value):
    install = installer()
    install.settings = replace(install.settings, **{field: value})
    with pytest.raises(ValueError, match="receipt"):
        install.receipt()


def test_changed_infrastructure_archive_never_updates_stack():
    install, manager = installer(), Manager()
    manager.archive = b"changed"
    with pytest.raises(ValueError, match="archive"):
        install.provision(manager, install.receipt(), time.monotonic() + 10)
    assert not manager.updates and not manager.creates


def test_saved_plan_rejected_when_another_stack_variable_changes():
    install, manager = installer(), Manager()
    manager.lose_response = "PLAN"
    with pytest.raises(TimeoutError):
        install.provision(manager, install.receipt(), time.monotonic() + 10)
    manager.variables["keep"] = "externally changed"
    with pytest.raises(ValueError, match="saved module plan"):
        install.provision(manager, install.receipt(), time.monotonic() + 10)
    assert manager.creates == ["PLAN"]


@pytest.mark.parametrize("attribute,value", [("stack_id", "foreign"), ("operation", "DESTROY"),
    ("freeform_tags", {}), ("job_operation_details", NS(execution_plan_job_id="foreign-plan"))])
def test_saved_job_identity_and_apply_plan_are_verified(attribute, value):
    install, manager = installer(), Manager()
    install.provision(manager, install.receipt(), time.monotonic() + 10)
    job = manager.jobs[1]
    setattr(job, attribute, value)
    with pytest.raises(ValueError):
        install.wait_job(manager, RECEIPT, job.id, "APPLY", time.monotonic() + 10)


@pytest.mark.parametrize("initial_status", ["available", "activating", "failed"])
def test_reads_never_deploy_and_repeated_posts_keep_one_background_operation(monkeypatch, initial_status):
    install = installer()
    install.write(status=initial_status)
    entered, finish = Event(), Event()
    calls = []
    def run():
        calls.append(1)
        entered.set()
        finish.wait(5)
    monkeypatch.setattr(install, "run", run)
    async def scenario():
        journal = install.read()
        read = await install.status()
        assert read["resumable"] == (initial_status == "activating") and not calls
        assert install.read() == journal
        first = await install.status(True, {"id": "administrator", "ocid": "ocid1.user.fixture"})
        await asyncio.to_thread(entered.wait, 2)
        second = await install.status(True, {"id": "other", "ocid": "ocid1.user.other"})
        assert first["operation_id"] == second["operation_id"] and calls == [1]
        assert install.read()["administrator_user_id"] == "administrator"
        assert install.read()["administrator_ocid"] == "ocid1.user.fixture"
        finish.set()
        await install.task
    asyncio.run(scenario())


def test_readiness_failure_never_reports_success_or_leaks_provider_details(monkeypatch):
    install = installer()
    install.write(stage="verification")
    def fail():
        raise RuntimeError("sensitive provider response")
    monkeypatch.setattr(install, "verify", fail)
    install.run()
    state = install.read()
    assert state["status"] == "failed" and not state["enabled"]
    assert "sensitive" not in state["message"]


def test_native_transport_failure_preserves_attempt_and_recovers_job_on_resume(monkeypatch):
    install, manager = installer(), Manager()
    original_create = manager.create_job
    def create_with_lost_response(details, **kwargs):
        result = original_create(details, **kwargs)
        if details.operation == "APPLY":
            raise oci.exceptions.RequestException(Exception("private transport payload"))
        return result
    monkeypatch.setattr(manager, "create_job", create_with_lost_response)
    monkeypatch.setattr(oci.resource_manager, "ResourceManagerClient", lambda *_args, **_kwargs: manager)
    monkeypatch.setattr(install, "bootstrap", lambda *_: None)
    install.run()
    assert install.read()["status"] == "activating" and install.read()["attempt"] == 0
    assert "private" not in install.read()["message"]
    async def resume():
        assert (await install.status())["resumable"]
        await install.status(True)
        await install.task
    asyncio.run(resume())
    assert manager.creates == ["PLAN", "APPLY"]
    assert install.read()["status"] == "ready" and install.read()["attempt"] == 0


def tag_migration(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "scripts"))
    import migrate_module_default_tags as migration
    import base64
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        for name, provider in migration.PROVIDERS.items():
            bundle.writestr(name, provider + "\n")
        bundle.writestr("runtime.bin", b"unchanged\x00bytes")
    install, manager = installer(), Manager()
    manager.archive = archive.getvalue()
    receipt = {**RECEIPT, "terraform_config_sha256": hashlib.sha256(manager.archive).hexdigest()}
    objects = install.store().objects
    stored = objects.get_object("namespace", "artifacts", RECEIPT_KEY)
    objects.put_object("namespace", "artifacts", RECEIPT_KEY, json.dumps(receipt).encode(), if_match=stored.headers["etag"])
    before = {"defined_tags": {"Oracle-Tags.CreatedBy": "operator", "Oracle-Tags.CreatedOn": "timestamp", "bootstrap.run": "scope"}}
    manager.plan["resource_changes"].append({"address": "oci_core_instance.lab", "change": {
        "actions": ["update"], "before": before, "after": {"defined_tags": {"bootstrap.run": "scope"}}}})
    with pytest.raises(ValueError, match="unrelated"):
        install.provision(manager, receipt, time.monotonic() + 10)
    install.write(status="failed")
    original_get_stack = manager.get_stack
    def get_stack(key):
        response = original_get_stack(key)
        response.data.config_source = NS(working_directory="")
        return response
    monkeypatch.setattr(manager, "get_stack", get_stack)
    variables = dict(manager.variables)
    original_archive = manager.archive
    updates = []
    def update_stack(_key, details, **kwargs):
        assert kwargs == {"if_match": "revision"} and details.variables is None
        updates.append(1)
        manager.archive = base64.b64decode(details.config_source.zip_file_base64_encoded)
    monkeypatch.setattr(manager, "update_stack", update_stack)
    return install, manager, migration, objects, receipt, variables, original_archive, updates


@pytest.mark.parametrize("lost_receipt_write", [False, True])
def test_explicit_tag_migration_preserves_archive_variables_and_resumes_after_partial_write(monkeypatch, lost_receipt_write):
    import base64
    install, manager, migration, objects, receipt, variables, original_archive, updates = tag_migration(monkeypatch)
    assert migration.migrate(install, manager).startswith("Verified")
    assert not updates and install.store().get_json(migration.KEY)[0] is None
    original_put = objects.put_object
    def put_with_conflict(namespace, bucket, key, body, **kwargs):
        if key == RECEIPT_KEY and lost_receipt_write and not failures:
            failures.append(1)
            raise oci.exceptions.ServiceError(412, "PreconditionFailed", {}, "changed")
        return original_put(namespace, bucket, key, body, **kwargs)
    failures = []
    monkeypatch.setattr(objects, "put_object", put_with_conflict)
    if lost_receipt_write:
        with pytest.raises(oci.exceptions.ServiceError):
            migration.migrate(install, manager, True)
    assert migration.migrate(install, manager, True).startswith("Migration verified")
    assert migration.migrate(install, manager, True).startswith("Migration verified")
    assert updates == [1] and manager.creates == ["PLAN"] and manager.variables == variables
    record = install.store().get_json(migration.KEY)[0]
    assert base64.b64decode(record["original_archive"]) == original_archive and record["receipt"] == receipt
    with zipfile.ZipFile(io.BytesIO(manager.archive)) as bundle:
        assert bundle.read("runtime.bin") == b"unchanged\x00bytes"
        assert all(bundle.read(name).count(b"ignore_defined_tags") == 1 for name in migration.PROVIDERS)
    assert install.read()["status"] == "failed" and install.read()["operation_id"] == "operation"
    manager.archive = b"unrelated source change"
    with pytest.raises(ValueError, match="outside this migration"):
        migration.migrate(install, manager, True)


def test_tag_migration_never_accepts_an_additional_portal_change_or_replacement(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "scripts"))
    import migrate_module_default_tags as migration
    value = plan()
    value["resource_changes"].append({"address": "oci_core_instance.lab", "change": {
        "actions": ["update"], "before": {"defined_tags": {key: "automatic" for key in migration.TAGS}, "shape": "existing"},
        "after": {"defined_tags": {}, "shape": "changed"}}})
    with pytest.raises(ValueError, match="also changes"):
        migration.validate_old_plan(value, RECEIPT)
