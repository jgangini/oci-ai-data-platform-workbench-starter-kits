import asyncio
from copy import deepcopy
from datetime import timedelta
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.modules.ai_data_governance.catalog import CatalogReader
from app.modules.ai_data_governance.documentary_graph import build_documentary_graph
from app.modules.ai_data_governance.documentary_hooks import JournalDocumentaryHooks
from app.modules.ai_data_governance.documentation import DocumentationAdmission
from app.modules.ai_data_governance.inventory import canonical, identity
from app.modules.ai_data_governance.inventory_writer import persist_inventory
from app.modules.ai_data_governance.materialize import materialize_documentary_batch
import app.modules.ai_data_governance.native_pipeline as native_pipeline
from app.modules.ai_data_governance.publication import publish_work
from app.modules.ai_data_governance.replication import replicate_work
from tests.modules.ai_data_governance.test_ai_dg_inventory_writer import AT, observations
from tests.modules.ai_data_governance.test_ai_dg_materialize import fixture
from tests.modules.ai_data_governance.test_ai_dg_publication import NativeTable
from tests.modules.ai_data_governance.test_ai_dg_publication_controller import put_count


async def documentary_cycle(repository, journal, batch, row, context, execution, provider):
    from langgraph.checkpoint.memory import InMemorySaver
    journal.create("batches", batch, {"schema_version": 1, "batch": {"batch_ref": batch,
        "admissions": [row], "contexts": {row["work_id"]: context}}, "execution": execution})
    hooks = JournalDocumentaryHooks(journal, call_model=provider, authorize=AsyncMock())
    graph = build_documentary_graph(load_batch=hooks.load_batch, claim_generation=hooks.claim_generation,
        generate=hooks.generate, review=hooks.review, save_outcomes=hooks.save_outcomes,
        check_active=hooks.check_active, checkpointer=InMemorySaver())
    result = await graph.ainvoke({"batch_ref": batch}, config={"configurable": {"thread_id": batch}, "recursion_limit": 12})
    producer = repository.data["AI_DG_RUN"][(row["run_id"],)]
    manifest = {"job_key": execution["job_key"], "job_run_id": execution["job_run_id"],
        "config_version": 1, "task_key": "document_metadata", "batch_ref": batch,
        "inventory_run_id": execution["inventory_run_id"], "component_revision": producer["component_version"],
        "outcomes_digest": identity("documentary-outcomes", journal.read("outcomes", batch))}
    producer.update(status="SUCCEEDED", manifest_json=canonical(manifest))
    assert result["completed"] and result["outcomes"][0]["status"] == "documented"


def test_external_clear_starts_a_fresh_documentary_cycle_and_replica_retry_never_republishes(monkeypatch):
    async def check():
        repository, journal, old_outcome, work = fixture()
        # Retain the same installation/repository and journal throughout both cycles.
        journal.rows.clear()
        native = NativeTable()
        native.detail["key"] = "catalog.schema.users"
        native.detail["tableFields"][0]["fieldName"] = "ABC13"
        version = deepcopy(repository.data["AI_DG_ASSET_VERSION"][(work["expected_version_id"],)])
        asset = deepcopy(repository.data["AI_DG_ASSET"][(work["asset_id"],)])
        binding = {"platform_id": "platform", "workspace_key": "11111111-1111-4111-8111-111111111111",
                   "source_scopes": [{"catalog_key": "catalog", "schema_key": "catalog.schema"}]}
        reader = Mock()
        reader.read.return_value = SimpleNamespace(nodes=({"qualifiedName": "catalog.schema.users"},),
            links=({"source": "input", "target": "output"},), pagination_complete=True, omitted_nodes=0, unresolved_links=0)
        monkeypatch.setattr(native_pipeline, "LineageReader", Mock(return_value=reader))
        now = [AT]
        monkeypatch.setattr(native_pipeline, "datetime", SimpleNamespace(now=lambda tz: now[0]))
        async def model(phase, payload, *_):
            if phase == "document":
                return canonical({"batch_ref": payload["batch_ref"], "items": [{"work_id": r["work_id"],
                    "status": "documented", "description_draft": "Valor de la transformación observada.",
                    "supported_facts": ["Transformación presente en la evidencia."], "evidence_ids": r["evidence_ids"]}
                    for r in payload["admissions"]]})
            return canonical({"batch_ref": payload["batch_ref"], "reviews": [{"work_id": r["work_id"],
                "verdict": "supported", "unsupported_claims": [], "reason_code": None,
                "evidence_ids": json.loads(r["manifest_snapshot"])["used_evidence_ids"]} for r in payload["candidates"]]})
        provider = AsyncMock(side_effect=model)
        original_row = json.loads(old_outcome["manifest_snapshot"])["admission"]
        first_evidence = None
        saved = None
        for index in range(2):
            now[0] = AT + timedelta(hours=index)
            batch = ("a" if index == 0 else "b") * 64
            inventory_id, producer_id, writer_id, replica_id = (
                ("inventory", "producer", "run", "replica") if index == 0 else
                ("inventory-next", "producer-next", "publish-next", "replica-next"))
            execution = {"scope_id": "scope", "job_key": "job", "job_run_id": "native-" + str(index),
                "inventory_run_id": inventory_id}
            if index:
                native.detail["description"] = None
                inventory = deepcopy(repository.data["AI_DG_RUN"][("run",)])
                inventory.update(run_id=inventory_id, status="RUNNING")
                repository.insert_immutable("AI_DG_RUN", [inventory])
                incoming = observations(run=inventory_id, hour=1)
                assert incoming.work[0]["expected_version_id"] == work["expected_version_id"]
                persist_inventory(repository, "scope", inventory_id, incoming)
                reopened = repository.data["AI_DG_WORK"][(work["work_id"],)]
                assert reopened["phase"] == "DOCUMENT" and reopened["selected_document_id"] is None
                producer = deepcopy(repository.data["AI_DG_RUN"][("producer",)])
                producer.update(run_id=producer_id, manifest_json="{}")
                repository.insert_immutable("AI_DG_RUN", [producer])
            context, evidence = native_pipeline.collect_usage_context(repository, Mock(), binding, asset, version, "scope", inventory_id)
            repository.insert_immutable("AI_DG_EVIDENCE", evidence)
            if index == 0:
                first_evidence = evidence[0]["evidence_id"]
            else:
                assert evidence[0]["evidence_id"] != first_evidence
            row = DocumentationAdmission.model_validate_json(canonical({**original_row, "run_id": producer_id,
                "context_hash": identity("usage-context", context), "evidence_ids": [evidence[0]["evidence_id"]]})).model_dump(mode="json")
            await documentary_cycle(repository, journal, batch, row, context, execution, provider)
            writer = deepcopy(repository.data["AI_DG_RUN"][("run",)])
            writer.update(run_id=writer_id, status="RUNNING", manifest_json=canonical({"job_key": "job",
                "job_run_id": execution["job_run_id"], "config_version": 1, "task_key": "publish_metadata"}))
            repository.data["AI_DG_RUN"][(writer_id,)] = writer
            materialize_documentary_batch(repository, journal, batch, publisher_run_id=writer_id,
                check_active=Mock(), observed_at=now[0])
            result = publish_work(repository, journal, native.request, "scope", work["work_id"], writer_id,
                check_active=Mock(), observed_at=now[0])
            assert result["catalog_state"] == "VERIFIED" and put_count(native) == index + 1
            replica = deepcopy(writer)
            replica.update(run_id=replica_id, manifest_json=canonical({"job_key": "job",
                "job_run_id": execution["job_run_id"], "config_version": 1, "task_key": "replicate_model"}))
            repository.insert_immutable("AI_DG_RUN", [replica])
            if index:
                repository.fail_table = "AI_DG_ASSET_VERSION"
                with pytest.raises(TimeoutError):
                    replicate_work(repository, journal, CatalogReader(native.request), "scope", work["work_id"], replica_id,
                        check_active=Mock(), observed_at=now[0] + timedelta(minutes=1))
                publish_work(repository, journal, native.request, "scope", work["work_id"], writer_id,
                    check_active=Mock(), observed_at=now[0] + timedelta(minutes=1))
                assert put_count(native) == 2
            final = replicate_work(repository, journal, CatalogReader(native.request), "scope", work["work_id"], replica_id,
                check_active=Mock(), observed_at=now[0] + timedelta(minutes=2))
            assert final["replica_state"] == "VERIFIED"
            assert repository.data["AI_DG_WORK"][(work["work_id"],)]["state"] == "DONE"
            if index == 0:
                saved = (deepcopy(repository.data["AI_DG_DOCUMENT"]), deepcopy(repository.data["AI_DG_RUN"][("producer",)]),
                    deepcopy(repository.data["AI_DG_PUBLICATION"]), deepcopy(journal.read("publications", result["publication_id"])))
            else:
                for key, document in saved[0].items():
                    assert repository.data["AI_DG_DOCUMENT"][key] == document
                assert repository.data["AI_DG_RUN"][("producer",)] == saved[1]
                for key, publication in saved[2].items():
                    assert repository.data["AI_DG_PUBLICATION"][key] == publication
                    assert journal.read("publications", key[0]) == saved[3]
        assert len(repository.data["AI_DG_DOCUMENT"]) == len(repository.data["AI_DG_PUBLICATION"]) == 2
        assert [c.args[0] for c in provider.await_args_list] == ["document", "review", "document", "review"]
        assert put_count(native) == 2
    asyncio.run(check())
