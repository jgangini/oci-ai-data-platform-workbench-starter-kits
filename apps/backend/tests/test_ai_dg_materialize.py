from copy import deepcopy
from unittest.mock import Mock
import json

import pytest

from app.ai_data_governance.documentation import DocumentationAdmission
from app.ai_data_governance.inventory_writer import persist_inventory
from app.ai_data_governance.materialize import materialize_documentary_batch
from app.ai_data_governance.materialize import _materialization_context
from app.ai_data_governance.materialize import _materialization_records
from app.ai_data_governance.inventory import identity
from app.ai_data_governance.outcome_receipts import verified_documentary_receipt

from test_ai_dg_inventory_writer import MemoryRepository, observations, AT
from test_ai_dg_outcome_receipts import receipts
from test_ai_dg_documentary_hooks import BATCH


def fixture(asset_index=0):
    repository = MemoryRepository()
    records = observations()
    persist_inventory(repository, "scope", "run", records)
    agent_schema = repository.tables["AI_DG_AGENT"]
    values = {"STRING": "value", "INT": 1, "BIGINT": 1, "BOOLEAN": True, "TIMESTAMP": AT}
    agent = {c["name"]: values[c["type"]] if c["required"] else None for c in agent_schema["columns"]}
    agent.update(agent_id="documentary", platform_key="platform", workspace_key="value", agent_role="DOCUMENTATION")
    repository.insert_immutable("AI_DG_AGENT", [agent])
    producer = deepcopy(repository.data["AI_DG_RUN"][("run",)])
    producer.update(run_id="producer", agent_id="documentary", run_kind="AGENT", status="SUCCEEDED")
    work, version, evidence = records.work[asset_index], records.versions[asset_index], records.evidence[asset_index]
    row = DocumentationAdmission(scope_id="scope", run_id="producer", work_id=work["work_id"], asset_id=work["asset_id"],
        asset_version_id=version["version_id"], source_version=version["metadata_hash"], field_path="description",
        observed_description=None, context_hash="context", prompt_version="DGD-PROMPTS-2", model_ref="model",
        evidence_ids=(evidence["evidence_id"],), update_supported=True, max_description_length=512).model_dump(mode="json")
    journal, outcomes = receipts(row)
    execution = {"scope_id": "scope", "job_key": "job", "job_run_id": "native-run", "inventory_run_id": "inventory"}
    journal.rows["batches", BATCH]["execution"] = execution
    base = {"job_key": "job", "job_run_id": "native-run", "config_version": 1}
    writer = repository.data["AI_DG_RUN"][("run",)]
    writer.update(run_kind="TASK", agent_id=None, manifest_json=json.dumps({**base, "task_key": "publish_metadata"}))
    producer["manifest_json"] = json.dumps({**base, "task_key": "document_metadata", "batch_ref": BATCH,
        "inventory_run_id": "inventory", "component_revision": producer["component_version"],
        "outcomes_digest": identity("documentary-outcomes", journal.read("outcomes", BATCH))})
    repository.insert_immutable("AI_DG_RUN", [producer])
    repository.calls.clear()
    return repository, journal, outcomes[0], work


def materialize(repository, journal, **changes):
    return materialize_documentary_batch(repository, journal, BATCH, publisher_run_id="run",
        check_active=changes.pop("check_active", Mock()), observed_at=changes.pop("observed_at", AT), **changes)


def test_snapshot_documents_and_evidence_precede_selecting_document_on_work():
    repository, journal, outcome, work = fixture()
    result = materialize(repository, journal)
    assert result == {"documents_confirmed": 1, "outcomes_confirmed": 1, "catalog_writes": 0}
    changed = [(action, table) for action, table, rows in repository.calls if rows]
    assert changed == [("update", "AI_DG_RUN"), ("insert", "AI_DG_DOCUMENT"),
                       ("insert", "AI_DG_DOC_EVIDENCE"), ("update", "AI_DG_WORK")]
    target = repository.data["AI_DG_WORK"][(work["work_id"],)]
    assert target["phase"] == "PUBLISH" and target["state"] == "READY"
    assert target["selected_document_id"] == outcome["document_id"]
    manifest = json.loads(repository.data["AI_DG_RUN"][("producer",)]["manifest_json"])
    assert manifest["document_snapshots"][outcome["document_id"]] == outcome["manifest_snapshot"]
    assert journal.read("materialized", BATCH)


@pytest.mark.parametrize("failed_table", ["AI_DG_DOCUMENT", "AI_DG_DOC_EVIDENCE"])
def test_retry_after_document_commit_retains_original_provenance_and_does_not_regenerate(failed_table):
    repository, journal, outcome, work = fixture()
    repository.fail_table = failed_table
    with pytest.raises(TimeoutError):
        materialize(repository, journal)
    assert not journal.read("materialized", BATCH)
    assert repository.data["AI_DG_WORK"][(work["work_id"],)]["selected_document_id"] is None
    materialize(repository, journal, observed_at=AT.replace(hour=1))
    document = repository.data["AI_DG_DOCUMENT"][(outcome["document_id"],)]
    assert document["created_at"] == AT
    assert len(repository.data["AI_DG_DOCUMENT"]) == 1


def test_completed_replay_does_not_reset_work_already_in_publication_or_replication():
    repository, journal, outcome, work = fixture()
    materialize(repository, journal)
    selected = repository.data["AI_DG_WORK"][(work["work_id"],)]
    selected.update(phase="REPLICATE", state="RETRY", reason_code="REPLICA_PENDING")
    before = deepcopy(selected)
    materialize(repository, journal, observed_at=AT.replace(hour=1))
    assert repository.data["AI_DG_WORK"][(work["work_id"],)] == before


def reused_document_fixture():
    from dataclasses import asdict
    from app.ai_data_governance.documentation import admit_documentation_items

    repository, journal, outcome, work = fixture()
    materialize(repository, journal)
    original = deepcopy(repository.data["AI_DG_DOCUMENT"][(outcome["document_id"],)])
    producer = deepcopy(repository.data["AI_DG_RUN"][(original["producer_run_id"],)])
    producer["run_id"] = "next-producer"
    repository.insert_immutable("AI_DG_RUN", [producer])
    row = {**json.loads(outcome["manifest_snapshot"])["admission"], "run_id": "next-producer"}
    admitted = DocumentationAdmission.model_validate_json(json.dumps(row))
    response = deepcopy(journal.read("responses", identity("model-response", "document", BATCH))["response"])
    response["batch_ref"] = "b" * 64
    reused = asdict(admit_documentation_items("b" * 64, (admitted,), response)[0])
    target = repository.data["AI_DG_WORK"][(work["work_id"],)]
    target.update(phase="DOCUMENT", state="READY", selected_document_id=None)
    return repository, original, admitted, reused, outcome


def test_cross_run_reuse_retains_original_document_producer_and_snapshot():
    repository, original, admitted, reused, old = reused_document_fixture()
    documents, links, snapshots, desired, _ = _materialization_records(repository,
        {admitted.work_id: admitted}, [reused], repository.data["AI_DG_SCOPE"][("scope",)],
        repository.data["AI_DG_RUN"][("run",)], AT)
    assert documents == [original]
    assert snapshots == {original["producer_run_id"]: {original["document_id"]: old["manifest_snapshot"]}}
    assert desired[0]["selected_document_id"] == original["document_id"]
    assert {r["document_id"] for r in links} == {original["document_id"]}


@pytest.mark.parametrize("change", [{"status": "RUNNING"}, {"scope_id": "foreign"}])
def test_reuse_does_not_make_an_unadmitted_original_producer_valid(change):
    repository, original, admitted, reused, _ = reused_document_fixture()
    repository.data["AI_DG_RUN"][(original["producer_run_id"],)].update(change)
    with pytest.raises(ValueError):
        _materialization_records(repository, {admitted.work_id: admitted}, [reused],
            repository.data["AI_DG_SCOPE"][("scope",)], repository.data["AI_DG_RUN"][("run",)], AT)


@pytest.mark.parametrize("target", ["scope", "producer", "agent", "work", "evidence"])
def test_foreign_disabled_changed_or_expired_context_is_rejected_before_writes(target):
    repository, journal, outcome, work = fixture()
    if target == "scope":
        repository.data["AI_DG_SCOPE"][("scope",)]["enabled"] = False
    elif target == "producer":
        repository.data["AI_DG_RUN"][("producer",)]["status"] = "FAILED"
    elif target == "agent":
        repository.data["AI_DG_AGENT"][("documentary",)]["agent_role"] = "GOVERNANCE_ASSISTANT"
    elif target == "work":
        repository.data["AI_DG_WORK"][(work["work_id"],)]["expected_version_id"] = "changed"
    else:
        for row in repository.data["AI_DG_EVIDENCE"].values():
            row["expires_at"] = AT
    with pytest.raises(ValueError):
        materialize(repository, journal)
    assert not repository.calls
    assert not journal.read("materialized", BATCH)


def test_missing_semantic_receipt_or_cancellation_never_writes_delta():
    repository, journal, outcome, work = fixture()
    check = Mock(side_effect=RuntimeError("cancelled"))
    with pytest.raises(RuntimeError, match="cancelled"):
        materialize(repository, journal, check_active=check)
    assert not repository.calls


def test_uncertain_final_receipt_recovers_without_changing_the_document_or_work():
    repository, journal, outcome, work = fixture()
    create = journal.create
    def lost_marker(kind, key, value):
        result = create(kind, key, value)
        if kind == "materialized":
            raise TimeoutError("committed before acknowledgement")
        return result
    journal.create = lost_marker
    with pytest.raises(TimeoutError):
        materialize(repository, journal)
    previous = deepcopy(repository.data)
    journal.create = create
    materialize(repository, journal, observed_at=AT.replace(hour=1))
    assert repository.data == previous
    assert journal.read("materialized", BATCH)


def test_applied_materialization_can_reconcile_after_evidence_expires():
    repository, journal, outcome, work = fixture()
    for evidence in repository.data["AI_DG_EVIDENCE"].values():
        evidence["expires_at"] = AT.replace(hour=1)
    create = journal.create
    def lost(kind, key, value):
        create(kind, key, value)
        raise TimeoutError("lost receipt acknowledgement")
    journal.create = lost
    with pytest.raises(TimeoutError):
        materialize(repository, journal)
    before = deepcopy(repository.data)
    journal.create = create
    materialize(repository, journal, observed_at=AT.replace(hour=2))
    assert repository.data == before
    assert repository.data["AI_DG_DOCUMENT"][(outcome["document_id"],)]["created_at"] == AT


@pytest.mark.parametrize("coverage", ["FAILED", "UNSUPPORTED"])
def test_failed_coverage_cannot_be_promoted_to_valid_document(coverage):
    repository, journal, outcome, work = fixture()
    for evidence in repository.data["AI_DG_EVIDENCE"].values():
        evidence["coverage_state"] = coverage
    with pytest.raises(ValueError, match="Failed or unsupported"):
        materialize(repository, journal)
    assert not repository.calls


@pytest.mark.parametrize("kind,agent", [("AGENT", None), ("TOOL", None), ("TASK", "documentary")])
def test_only_the_admitted_python_task_can_materialize(kind, agent):
    repository, journal, outcome, work = fixture()
    repository.data["AI_DG_RUN"][("run",)].update(run_kind=kind, agent_id=agent)
    with pytest.raises(ValueError, match="Python run"):
        materialize(repository, journal)
    assert not repository.calls


@pytest.mark.parametrize("change", [{"job_run_id": "other"}, {"job_key": "other"},
    {"batch_ref": "other"}, {"outcomes_digest": "other"}, {"component_revision": "other"}, {"config_version": 2}])
def test_unrelated_completed_agent_run_cannot_supply_this_batch(change):
    repository, journal, outcome, work = fixture()
    producer = repository.data["AI_DG_RUN"][("producer",)]
    producer["manifest_json"] = json.dumps({**json.loads(producer["manifest_json"]), **change})
    with pytest.raises(ValueError):
        materialize(repository, journal)
    assert not repository.calls


def test_batch_context_reads_the_common_producer_and_agent_once():
    repository, journal, outcome, work = fixture()
    value = verified_documentary_receipt(journal, BATCH)
    original = value["admissions"][0]
    value["admissions"] = [{**original, "work_id": "work-" + str(index)} for index in range(16)]
    repository.read_by_keys = Mock(wraps=repository.read_by_keys)
    _materialization_context(repository, value, "run")
    calls = repository.read_by_keys.call_args_list
    assert sum(c.args == ("AI_DG_RUN", [{"run_id": "producer"}]) for c in calls) == 1
    assert sum(c.args[0] == "AI_DG_AGENT" for c in calls) == 1


def test_materialization_admits_inventory_in_four_bounded_reads_for_the_whole_batch():
    repository, _, first, _ = fixture(0)
    _, _, second, _ = fixture(1)
    admissions = {row.work_id: row for row in (DocumentationAdmission.model_validate_json(
        json.dumps(json.loads(outcome['manifest_snapshot'])['admission'])) for outcome in (first, second))}
    outcomes = [{'work_id':work_id,'status':'insufficient_context','reason_code':'USAGE_CONTEXT_MISSING',
                 'document_id':None} for work_id in admissions]
    repository.read_by_keys = Mock(wraps=repository.read_by_keys)
    result = _materialization_records(repository, admissions, outcomes,
        repository.data['AI_DG_SCOPE'][('scope',)], repository.data['AI_DG_RUN'][('run',)], AT)
    assert len(result[3]) == 2 and all(row['state'] == 'WAIT_CONTEXT' for row in result[3])
    calls = repository.read_by_keys.call_args_list
    assert len(calls) == 4
    assert {call.args[0] for call in calls} == {'AI_DG_WORK','AI_DG_ASSET_VERSION','AI_DG_ASSET','AI_DG_SCOPE_ASSET'}
    assert all(len(call.args[1]) == 2 for call in calls)
    assert repository.calls == []


@pytest.mark.parametrize('failure', ['missing','foreign','duplicate'])
def test_batched_materialization_rejects_incomplete_or_ambiguous_inventory_before_writes(failure):
    repository, _, outcome, _ = fixture()
    admission = DocumentationAdmission.model_validate_json(json.dumps(json.loads(outcome['manifest_snapshot'])['admission']))
    read = repository.read_by_keys
    def changed(name, keys):
        rows = read(name, keys)
        if name != 'AI_DG_ASSET_VERSION':
            return rows
        if failure == 'missing':
            return []
        if failure == 'duplicate':
            return rows + deepcopy(rows)
        return [{**rows[0], 'version_id':'foreign-version'}]
    repository.read_by_keys = changed
    with pytest.raises(ValueError, match='missing or ambiguous'):
        _materialization_records(repository, {admission.work_id: admission}, [outcome],
            repository.data['AI_DG_SCOPE'][('scope',)], repository.data['AI_DG_RUN'][('run',)], AT)
    assert repository.calls == []
