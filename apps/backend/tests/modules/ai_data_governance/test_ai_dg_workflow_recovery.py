from copy import deepcopy
from datetime import timedelta
from unittest.mock import Mock

import pytest

from app.modules.ai_data_governance import native_pipeline
from app.modules.ai_data_governance.materialize import _documentary_work_transition
from app.modules.ai_data_governance.workflow_staging import stage_documentary_item
from tests.modules.ai_data_governance.test_ai_dg_inventory_writer import MemoryRepository, observations, AT
from tests.modules.ai_data_governance.test_ai_dg_materialize import fixture, materialize
from tests.modules.ai_data_governance.test_ai_dg_workflow import binding


def staging_fixture():
    from app.modules.ai_data_governance.inventory_writer import persist_inventory
    repository = MemoryRepository()
    records = observations()
    persist_inventory(repository, "scope", "run", records)
    work = records.work[0]
    evidence = {**records.evidence[0], "evidence_id": "usage-evidence", "evidence_kind": "RUNTIME_LINEAGE",
        "expires_at": AT + timedelta(hours=1)}
    collect = Mock(return_value=("Observed usage", [evidence]))
    inventory = repository.data["AI_DG_RUN"][("run",)]
    return repository, work, inventory, collect


def test_staging_retry_reuses_original_evidence_timestamps_without_renewing_validity():
    repository, work, inventory, collect = staging_fixture()
    first, _ = stage_documentary_item(repository, binding(), work, inventory, "producer", collect, check_active=Mock())
    context, evidence = collect.return_value
    evidence[0]["observed_at"] += timedelta(minutes=20)
    evidence[0]["expires_at"] += timedelta(minutes=20)
    second, _ = stage_documentary_item(repository, binding(), work, inventory, "producer", collect, check_active=Mock())
    assert first == second
    stored = repository.data["AI_DG_EVIDENCE"][("usage-evidence",)]
    assert stored["observed_at"] == AT and stored["expires_at"] == AT + timedelta(hours=1)


def test_staging_cancelled_during_context_read_performs_no_evidence_commit():
    repository, work, inventory, collect = staging_fixture()
    repository.calls.clear()
    with pytest.raises(RuntimeError, match="cancelled"):
        stage_documentary_item(repository, binding(), work, inventory, "producer", collect,
            check_active=Mock(side_effect=RuntimeError("cancelled")))
    collect.assert_called_once()
    assert not [c for c in repository.calls if c[0] in {"insert", "update"}]


def test_context_abstention_advances_work_cooldown_without_creating_a_document():
    repository, work, inventory, collect = staging_fixture()
    admission, _ = stage_documentary_item(repository, binding(), work, inventory, "producer", collect, check_active=Mock())
    outcome = {"status": "insufficient_context", "document_id": None, "reason_code": "USAGE_CONTEXT_MISSING"}
    from app.modules.ai_data_governance.documentary_graph import _admission
    changed = _documentary_work_transition(work, _admission(admission), outcome, "writer", AT)
    assert changed["state"] == "WAIT_CONTEXT" and changed["selected_document_id"] is None
    assert changed["next_attempt_at"] == AT + timedelta(minutes=30)


def test_replica_task_recovers_publish_phase_with_intent_and_skips_work_without_intent(monkeypatch):
    from app.modules.ai_data_governance.task_arguments import TaskArguments
    from tests.modules.ai_data_governance.test_ai_dg_native_documentary import RUN, TASK
    rows = [{"work_id": "started", "phase": "PUBLISH"}, {"work_id": "not-started", "phase": "PUBLISH"}]
    monkeypatch.setattr(native_pipeline, "load_execution_receipt", lambda *a: {
        "documentary_revision": "a" * 64, "scopes": [{"scope_id": "scope", "inventory_run_id": "inventory"}]})
    monkeypatch.setattr(native_pipeline, "prepare_phase_context", lambda *a: "writer")
    selector = Mock(return_value=rows)
    monkeypatch.setattr(native_pipeline, "phase_work", selector)
    monkeypatch.setattr(native_pipeline, "work_has_publication", lambda _, w: w["work_id"] == "started")
    replicate = Mock(return_value={"replica_state": "VERIFIED"})
    monkeypatch.setattr(native_pipeline, "replicate_work", replicate)
    monkeypatch.setattr(native_pipeline, "_finish_pipeline_phase", Mock())
    monkeypatch.setattr(native_pipeline, "verify_pipeline_completion", Mock())
    coverage = Mock()
    monkeypatch.setattr(native_pipeline, "verify_batch_replica_coverage", coverage)
    args = TaskArguments(RUN, TASK, "replicate_model", 1, 0)
    result = native_pipeline.pipeline_effects(Mock(), Mock(), Mock(), Mock(), binding(), "revision", args, check_active=Mock())
    assert selector.call_args.args[2] == ("PUBLISH", "REPLICATE")
    assert replicate.call_count == 1 and replicate.call_args.args[4] == "started"
    assert result["model_calls"] == 0
    coverage.assert_called_once()


def test_all_done_reconciliation_cannot_report_success_when_documentary_task_failed():
    from tests.modules.ai_data_governance.test_ai_dg_native_documentary import JOB, RUN, TASK
    run = {"key": RUN, "jobKey": JOB, "taskToTaskRunMap": {"sync_inventory": "sync", "document_metadata": TASK,
        "publish_metadata": "publish"}}
    def response(method, path):
        if "/jobRuns/" in path:
            return run, {}
        key = path.rsplit("/", 1)[-1]
        name = "sync_inventory" if key == "sync" else "document_metadata"
        return {"key": key, "jobRunKey": RUN, "jobKey": JOB, "taskKey": name,
            "state": {"status": "SUCCESS" if name == "sync_inventory" else "FAILED"}}, {}
    with pytest.raises(RuntimeError, match="upstream phase"):
        native_pipeline.verify_pipeline_completion(response, {**binding(), "job_key": JOB}, RUN)


@pytest.mark.parametrize("failure", ["upstream", "receipt", "coverage"])
def test_phase_receipt_is_not_successful_before_all_completion_checks(monkeypatch, failure):
    from app.modules.ai_data_governance.task_arguments import TaskArguments
    from tests.modules.ai_data_governance.test_ai_dg_native_documentary import RUN, TASK
    monkeypatch.setattr(native_pipeline, "load_execution_receipt", lambda *a: {
        "documentary_revision": "a" * 64, "scopes": [{"scope_id": "scope", "inventory_run_id": "inventory"}]})
    monkeypatch.setattr(native_pipeline, "prepare_phase_context", lambda *a: "writer")
    monkeypatch.setattr(native_pipeline, "phase_work", Mock(return_value=[{"work_id": "work", "phase": "REPLICATE"}]))
    monkeypatch.setattr(native_pipeline, "replicate_work", Mock(return_value={
        "replica_state": "UNKNOWN" if failure == "receipt" else "VERIFIED"}))
    finish = Mock()
    monkeypatch.setattr(native_pipeline, "_finish_pipeline_phase", finish)
    verification = Mock(side_effect=RuntimeError("upstream phase") if failure == "upstream" else None)
    monkeypatch.setattr(native_pipeline, "verify_pipeline_completion", verification)
    coverage = Mock(side_effect=RuntimeError("coverage pending") if failure == "coverage" else None)
    monkeypatch.setattr(native_pipeline, "verify_batch_replica_coverage", coverage)
    args = TaskArguments(RUN, TASK, "replicate_model", 1, 0)
    with pytest.raises(RuntimeError):
        native_pipeline.pipeline_effects(Mock(), Mock(), Mock(), Mock(), binding(), "revision", args, check_active=Mock())
    finish.assert_not_called()
    coverage.assert_called_once()
    assert verification.call_count == (1 if failure == "upstream" else 0)


def test_native_entry_uses_injected_secret_getter_without_importing_aidputils(monkeypatch, capsys):
    import sys
    from types import ModuleType, SimpleNamespace
    from app.modules.ai_data_governance.pipeline_source import pipeline_task_source
    from tests.modules.ai_data_governance.test_ai_dg_native_documentary import RUN, TASK
    source = pipeline_task_source(binding(), "sync_inventory").decode()
    body, footer = source.rsplit('if __name__ == "__main__":', 1)
    module = ModuleType("injected_runtime")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    exec(compile(body, "sync_inventory.py", "exec"), module.__dict__)
    getter = Mock()
    run = Mock(return_value={"phase": "sync_inventory", "status": "test-only"})
    module.__dict__.update(__name__="__main__", aidputils=SimpleNamespace(secrets=SimpleNamespace(get=getter)), run_native_pipeline=run)
    monkeypatch.setattr(sys, "argv", ["sync_inventory.py", "--job-run-id", RUN, "--task-run-id", TASK,
        "--task-key", "sync_inventory", "--execution-count", "1", "--repair-count", "0"])
    exec(compile('if __name__ == "__main__":' + footer, "sync_inventory.py", "exec"), module.__dict__)
    assert run.call_args.args[2] is getter
    assert '"status":"test-only"' in capsys.readouterr().out


def test_repeated_lineage_observations_keep_distinct_logical_unique_keys_between_inventory_runs(monkeypatch):
    from types import SimpleNamespace
    from app.modules.ai_data_governance.inventory import identity
    repository, work, inventory, _ = staging_fixture()
    records = observations()
    reader = Mock()
    reader.read.return_value = SimpleNamespace(nodes=({"id": "source"}, {"id": "target"}),
        links=({"fromNodeId": "source", "toNodeId": "target"},), pagination_complete=True, omitted_nodes=0, unresolved_links=0)
    monkeypatch.setattr(native_pipeline, "LineageReader", Mock(return_value=reader))
    cfg = {**binding(), "source_scopes": [{"catalog_key": "catalog", "schema_key": "catalog.schema"}]}
    a, first = native_pipeline.collect_usage_context(repository, Mock(), cfg, records.assets[0], records.versions[0], "scope", "run")
    b, second = native_pipeline.collect_usage_context(repository, Mock(), cfg, records.assets[0], records.versions[0], "scope", "next-run")
    assert a == b
    assert first[0]["evidence_id"] != second[0]["evidence_id"]
    assert first[0]["evidence_hash"] != second[0]["evidence_hash"]
    assert first[0]["evidence_kind"] == second[0]["evidence_kind"] == "RUNTIME_LINEAGE"
    repository.insert_immutable("AI_DG_EVIDENCE", first + second)


def test_repaired_documentary_attempt_preserves_original_producer_and_materialized_snapshot():
    from app.modules.ai_data_governance.inventory import identity
    from tests.modules.ai_data_governance.test_ai_dg_outcome_receipts import receipts
    from tests.modules.ai_data_governance.test_ai_dg_documentary_hooks import BATCH
    repository, journal, _, work = fixture()
    row = deepcopy(journal.read("batches", BATCH)["batch"]["admissions"][0])
    row["run_id"] = identity("documentary-producer", "native-run", "scope")
    journal, _ = receipts(row)
    journal.rows["batches", BATCH]["execution"] = {"scope_id": "scope", "job_key": "job",
        "job_run_id": "native-run", "inventory_run_id": "inventory"}
    repository.data["AI_DG_AGENT"].clear()
    repository.data["AI_DG_RUN"].pop(("producer",))
    cfg = {**binding(), "job_key": "job", "platform_id": "platform", "workspace_key": "value"}
    staged = {"scope_id": "scope", "inventory_run_id": "inventory", "batch_ref": BATCH}
    native_pipeline.record_documentary_producer(repository, journal, cfg, staged, {"jobRunKey": "native-run", "key": "first-task"})
    materialize(repository, journal)
    original = deepcopy(repository.data["AI_DG_RUN"][(row["run_id"],)])
    agent_key = next(iter(repository.data["AI_DG_AGENT"]))
    repository.data["AI_DG_AGENT"][agent_key]["deployment_version"] = "later-deployment"
    native_pipeline.record_documentary_producer(repository, journal, cfg, staged, {"jobRunKey": "native-run", "key": "repaired-task"})
    materialize(repository, journal)
    assert repository.data["AI_DG_AGENT"][agent_key]["deployment_version"] == "later-deployment"
    assert repository.data["AI_DG_RUN"][(row["run_id"],)] == original
    assert original["native_run_ref"] == "native-run:first-task"


@pytest.mark.parametrize("changed", [None, ("enabled", False), ("native_agent_key", "foreign-agent")])
def test_documentary_revision_upgrade_preserves_logical_agent_and_historical_runs(changed):
    from app.modules.ai_data_governance.inventory import identity
    from tests.modules.ai_data_governance.test_ai_dg_outcome_receipts import receipts
    from tests.modules.ai_data_governance.test_ai_dg_documentary_hooks import BATCH
    repository, journal, _, _ = fixture()
    row = deepcopy(journal.read("batches", BATCH)["batch"]["admissions"][0])
    row["run_id"] = identity("documentary-producer", "native-run", "scope")
    journal, _ = receipts(row)
    journal.rows["batches", BATCH]["execution"] = {"scope_id": "scope", "job_key": "job",
        "job_run_id": "native-run", "inventory_run_id": "inventory"}
    cfg = {**binding(), "job_key": "job", "platform_id": "platform", "workspace_key": "value"}
    key = identity("AI_DG_AGENT", "platform", "value", "DOCUMENTATION")
    old_agent = {"agent_id": key, "platform_key": "platform", "workspace_key": "value",
        "agent_role": "DOCUMENTATION", "native_agent_key": cfg["documentation_agent_key"],
        "deployment_version": "previous-revision",
        "code_path": "/Workspace/.artifact/ai_data_governance/agents/documentation/documentation_agent.py",
        "capabilities_json": '["document_metadata"]', "enabled": True}
    repository.data["AI_DG_AGENT"] = {(key,): deepcopy(old_agent)}
    if changed:
        repository.data["AI_DG_AGENT"][(key,)][changed[0]] = changed[1]
    historical = deepcopy(repository.data["AI_DG_RUN"])
    staged = {"scope_id": "scope", "inventory_run_id": "inventory", "batch_ref": BATCH}
    if changed:
        with pytest.raises(ValueError, match="identity or enabled state"):
            native_pipeline.record_documentary_producer(repository, journal, cfg, staged,
                {"jobRunKey": "native-run", "key": "new-task"})
        assert repository.data["AI_DG_RUN"] == historical
        return
    native_pipeline.record_documentary_producer(repository, journal, cfg, staged,
        {"jobRunKey": "native-run", "key": "new-task"})
    assert len(repository.data["AI_DG_AGENT"]) == 1
    assert repository.data["AI_DG_AGENT"][(key,)] == {**old_agent, "deployment_version": cfg["documentary_revision"]}
    assert all(repository.data["AI_DG_RUN"][k] == v for k, v in historical.items())
    assert repository.data["AI_DG_RUN"][(row["run_id"],)]["component_version"] == cfg["documentary_revision"]


@pytest.mark.parametrize("omitted,unresolved,paginated,coverage", [
    (0, 0, True, "COMPLETE"), (1, 0, True, "PARTIAL"),
    (0, 1, True, "PARTIAL"), (0, 0, False, "PARTIAL"),
])
def test_lineage_coverage_retains_omissions_and_unresolved_links(monkeypatch, omitted, unresolved, paginated, coverage):
    import json
    from types import SimpleNamespace
    repository, _, _, _ = staging_fixture()
    records = observations()
    reader = Mock()
    reader.read.return_value = SimpleNamespace(nodes=({"id": "visible"},),
        links=({"fromNodeId": "visible", "toNodeId": "visible"},),
        pagination_complete=paginated, omitted_nodes=omitted, unresolved_links=unresolved)
    monkeypatch.setattr(native_pipeline, "LineageReader", Mock(return_value=reader))
    cfg = {**binding(), "source_scopes": [{"catalog_key": "catalog", "schema_key": "catalog.schema"}]}
    context, evidence = native_pipeline.collect_usage_context(repository, Mock(), cfg,
        records.assets[0], records.versions[0], "scope", "run")
    assert evidence[0]["coverage_state"] == coverage
    for value in (json.loads(context), json.loads(evidence[0]["facts_json"])["usage_context"]):
        assert value["omitted_nodes"] == omitted and value["unresolved_links"] == unresolved
        assert value["execution_evidence"] is False


def test_final_batch_check_catches_terminal_work_with_a_redacted_projection():
    from app.modules.ai_data_governance.native_pipeline import verify_batch_replica_coverage
    from tests.modules.ai_data_governance.test_ai_dg_documentary_hooks import BATCH
    repository, journal, _, work = fixture()
    materialize(repository, journal)
    row = {"scope_id": "scope", "batch_ref": BATCH}
    verify_batch_replica_coverage(repository, journal, row)
    repository.data['AI_DG_WORK'][(work['work_id'],)]['state'] = 'PRESERVED'
    repository.data['AI_DG_SCOPE_ASSET'][('scope', work['asset_id'])]['description'] = '*********(redacted)'
    with pytest.raises(RuntimeError, match='redacted'):
        verify_batch_replica_coverage(repository, journal, row)


@pytest.mark.parametrize("kind", ["missing", "foreign", "duplicate"])
def test_final_batch_check_rejects_incomplete_or_foreign_projection_coverage(kind):
    from app.modules.ai_data_governance.native_pipeline import verify_batch_replica_coverage
    from tests.modules.ai_data_governance.test_ai_dg_documentary_hooks import BATCH
    repository, journal, _, work = fixture()
    materialize(repository, journal)
    original = repository.read_by_keys
    def read(table, keys):
        rows = original(table, keys)
        if table != 'AI_DG_SCOPE_ASSET':
            return rows
        if kind == 'missing':
            return []
        if kind == 'duplicate':
            return rows + rows
        return [{**rows[0], 'scope_id': 'other-scope'}]
    repository.read_by_keys = read
    with pytest.raises(RuntimeError, match='coverage'):
        verify_batch_replica_coverage(repository, journal, {'scope_id': 'scope', 'batch_ref': BATCH})
