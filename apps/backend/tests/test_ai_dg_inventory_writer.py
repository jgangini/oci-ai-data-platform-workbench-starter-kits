from copy import deepcopy
from datetime import datetime, timezone

import pytest

from app.ai_data_governance.inventory import table_records
from app.ai_data_governance.inventory_writer import persist_inventory
from app.ai_data_governance.repository import checked_rows
from app.ai_data_governance.schema import load_model


AT = datetime(2026, 10, 31, tzinfo=timezone.utc)


class MemoryRepository:
    max_batch_rows = 1000

    def __init__(self):
        self.tables = {t["name"]: t for t in load_model()["tables"]}
        self.data = {name: {} for name in self.tables}
        self.calls = []
        self.fail_table = None
        for name, overrides in [
            ("AI_DG_SCOPE", {"scope_id": "scope", "platform_key": "platform", "enabled": True,
                             "catalog_key": "catalog", "schema_key": "catalog.schema"}),
            ("AI_DG_RUN", {"run_id": "run", "scope_id": "scope", "status": "RUNNING"}),
        ]:
            values = {"STRING": "value", "INT": 1, "BIGINT": 1, "BOOLEAN": True, "TIMESTAMP": AT}
            record = {c["name"]: values[c["type"]] if c["required"] else None for c in self.tables[name]["columns"]}
            record.update(overrides)
            self.data[name][self.key(name, record)] = record

    def key(self, table, row):
        return tuple(row[name] for name in self.tables[table]["pk"])

    def read_by_keys(self, table, keys):
        return deepcopy([self.data[table][self.key(table, key)] for key in keys
                         if self.key(table, key) in self.data[table]])

    def insert_immutable(self, table, rows):
        checked_rows(table, rows)
        self.calls.append(("insert", table, deepcopy(rows)))
        for row in rows:
            key = self.key(table, row)
            assert key not in self.data[table] or self.data[table][key] == row
            self.data[table][key] = deepcopy(row)
        if rows and self.fail_table == table:
            self.fail_table = None
            raise TimeoutError("committed before disconnect")

    def update_current(self, table, rows, *, expected):
        checked_rows(table, rows)
        self.calls.append(("update", table, deepcopy(rows)))
        for old, row in zip(expected, rows):
            key = self.key(table, row)
            assert self.data[table][key] in (old, row)
            self.data[table][key] = deepcopy(row)


def observations(*, run="run", hour=0, version=None):
    table = {"table_key": "catalog.schema.users", "name": "users", "native_version": version,
             "description": None, "columns": ({"name": "ABC13", "native_key": None,
                  "ordinal": 0, "data_type": "STRING", "description": None},)}
    return table_records("platform", "scope", run, table, AT.replace(hour=hour))


def test_current_pointers_follow_persisted_versions_and_repeat_keeps_first_observation():
    target = MemoryRepository()
    first = observations()
    assert persist_inventory(target, "scope", "run", first)["assets_confirmed"] == 2
    operations = [table for action, table, rows in target.calls if rows]
    assert operations.index("AI_DG_ASSET_VERSION") < operations.index("AI_DG_SCOPE_ASSET")
    assert all(row["current_version_id"] is None for row in target.calls[0][2])
    versions = deepcopy(target.data["AI_DG_ASSET_VERSION"])
    evidence = deepcopy(target.data["AI_DG_EVIDENCE"])
    second = observations(hour=1)
    assert persist_inventory(target, "scope", "run", second)["work_retained"] == 2
    assert target.data["AI_DG_ASSET_VERSION"] == versions
    assert target.data["AI_DG_EVIDENCE"] == evidence
    assert all(row["first_seen_at"] == AT for row in target.data["AI_DG_ASSET"].values())
    assert all(row["last_seen_at"] == AT.replace(hour=1) for row in target.data["AI_DG_SCOPE_ASSET"].values())


def test_partial_commit_recovers_without_replacing_original_provenance():
    target = MemoryRepository()
    target.fail_table = "AI_DG_ASSET_VERSION"
    with pytest.raises(TimeoutError):
        persist_inventory(target, "scope", "run", observations())
    assert not target.data["AI_DG_SCOPE_ASSET"]
    assert all(row["current_version_id"] is None for row in target.data["AI_DG_ASSET"].values())
    persist_inventory(target, "scope", "run", observations(hour=1))
    assert all(row["observed_at"] == AT for row in target.data["AI_DG_ASSET_VERSION"].values())
    assert len(target.data["AI_DG_ASSET"]) == len(target.data["AI_DG_WORK"]) == 2


def test_changed_version_advances_same_waiting_work_but_preserves_publication_work():
    target = MemoryRepository()
    first = observations()
    persist_inventory(target, "scope", "run", first)
    selected = target.key("AI_DG_WORK", first.work[0])
    target.data["AI_DG_WORK"][selected].update(phase="REPLICATE", state="RETRY", selected_document_id="document")
    second = observations(version="new-version")
    persist_inventory(target, "scope", "run", second)
    assert len(target.data["AI_DG_WORK"]) == 2
    assert target.data["AI_DG_WORK"][selected]["selected_document_id"] == "document"
    column = target.key("AI_DG_WORK", second.work[1])
    assert target.data["AI_DG_WORK"][column]["expected_version_id"] == second.work[1]["expected_version_id"]


def test_catalog_scan_does_not_hide_pending_replication_or_replace_verified_projection():
    target = MemoryRepository()
    first = observations()
    persist_inventory(target, "scope", "run", first)
    key = target.key("AI_DG_SCOPE_ASSET", first.projections[0])
    target.data["AI_DG_SCOPE_ASSET"][key]["consistency_state"] = "REPLICA_PENDING"
    original = deepcopy(target.data["AI_DG_SCOPE_ASSET"][key])
    second = observations(hour=1, version="new-version")
    persist_inventory(target, "scope", "run", second)
    actual = target.data["AI_DG_SCOPE_ASSET"][key]
    assert actual == {**original, "last_seen_at": AT.replace(hour=1)}
    old_work_key = target.key("AI_DG_WORK", first.work[0])
    assert target.data["AI_DG_WORK"][old_work_key]["expected_version_id"] == first.work[0]["expected_version_id"]


def test_closed_work_reopens_when_a_new_revision_is_empty():
    target = MemoryRepository()
    first = observations()
    persist_inventory(target, "scope", "run", first)
    key = target.key("AI_DG_WORK", first.work[0])
    target.data["AI_DG_WORK"][key].update(phase="REPLICATE", state="DONE", selected_document_id="old-document")
    second = observations(hour=1, version="new-version")
    persist_inventory(target, "scope", "run", second)
    actual = target.data["AI_DG_WORK"][key]
    assert actual["phase"] == "DOCUMENT" and actual["state"] == "WAIT_CONTEXT"
    assert actual["selected_document_id"] is None
    assert actual["expected_version_id"] == second.work[0]["expected_version_id"]


@pytest.mark.parametrize("change", [
    lambda t, r: t.data["AI_DG_RUN"][("run",)].update(status="CANCELLED"),
    lambda t, r: t.data["AI_DG_SCOPE"][("scope",)].update(enabled=False),
    lambda t, r: r.projections[0].update(scope_id="other"),
    lambda t, r: r.versions[0].update(observed_run_id="other"),
    lambda t, r: r.evidence[0].update(asset_version_id="unknown"),
    lambda t, r: r.versions[0].update(qualified_name="catalog.other.users"),
    lambda t, r: r.work[0].update(asset_id=r.assets[1]["asset_id"]),
    lambda t, r: r.projections[0].update(description="not in its snapshot"),
    lambda t, r: r.projections.clear(),
    lambda t, r: r.evidence[0].update(facts_json="{}"),
])
def test_scope_run_and_relationship_failures_prevent_all_writes(change):
    target = MemoryRepository()
    records = observations()
    change(target, records)
    with pytest.raises(ValueError):
        persist_inventory(target, "scope", "run", records)
    assert target.calls == []
