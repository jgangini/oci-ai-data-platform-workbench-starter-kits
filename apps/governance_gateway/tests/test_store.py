from __future__ import annotations

from typing import Any

from governance_gateway.catalog import CatalogColumn, CatalogSnapshot, LineageEdge
from governance_gateway.store import JdbcControlStore


def _column() -> CatalogColumn:
    return CatalogColumn(
        "aidp_lab", "gold", "customers", "email", "aidp_lab", "catalog-guid", "aidp_lab.gold",
        "aidp_lab.gold.customers", "table-fingerprint", 0, "string", "", "STANDARD",
        "2026-01-01T00:00:00Z", "ocid1.user.oc1..owner",
    )


class Cursor:
    def __init__(self) -> None:
        self.executions: list[tuple[str, tuple[Any, ...]]] = []
        self.rowcount = 0

    def execute(self, statement: str, parameters: tuple[Any, ...] = ()) -> None:
        self.executions.append((statement, parameters))

    def fetchall(self) -> list[tuple[Any, ...]]:
        return []

    def close(self) -> None:
        return None


class Connection:
    def __init__(self) -> None:
        self.cursor_instance = Cursor()
        self.committed = False
        self.rolled_back = False

    def cursor(self) -> Cursor:
        return self.cursor_instance

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        return None


def test_catalog_merge_preserves_governance_fields_and_marks_absent_columns() -> None:
    connection = Connection()
    store = JdbcControlStore(lambda: connection, "aidp_lab")
    result = store.apply_catalog_snapshot(CatalogSnapshot("v1", "hash", (_column(),)))
    statements = [statement for statement, _ in connection.cursor_instance.executions]
    merge = next(statement for statement in statements if "MERGE INTO" in statement and "data_governance" in statement)
    matched = merge.split("WHEN NOT MATCHED", 1)[0]
    assert "sensitivity =" not in matched
    assert "review_state =" not in matched
    assert any("source_version <> ?" in statement for statement in statements)
    assert any("INSERT INTO `aidp_lab`.`oci_artifacts`.data_governance_sync_state" in statement for statement in statements)
    assert result == {"columns": 1, "new": 1, "renamed": 0, "deleted": 0, "lineage_edges": 0}
    assert connection.committed is True


def test_catalog_snapshot_materializes_only_exact_column_lineage() -> None:
    connection = Connection()
    store = JdbcControlStore(lambda: connection, "aidp_lab")
    source = _column()
    target = CatalogColumn(
        "aidp_lab", "gold", "customer_summary", "email", "aidp_lab", "catalog-guid", "aidp_lab.gold",
        "aidp_lab.gold.customer_summary", "target-fingerprint", 0, "string", "", "STANDARD",
        "2026-01-02T00:00:00Z", "ocid1.user.oc1..owner",
    )
    snapshot = CatalogSnapshot(
        "v1", "hash", (source, target),
        (LineageEdge("aidp_lab.gold.customers.email", "aidp_lab.gold.customer_summary.email"),),
    )
    result = store.apply_catalog_snapshot(snapshot)
    lineage_merges = [
        statement for statement, _ in connection.cursor_instance.executions
        if "MERGE INTO" in statement and "lineage_propagation" in statement
    ]
    assert len(lineage_merges) == 1
    assert result["lineage_edges"] == 1


class PolicyStore(JdbcControlStore):
    def __init__(self) -> None:
        super().__init__(lambda: None, "aidp_lab")

    def _read(self, statement: str, parameters: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        del parameters
        if "access_policy" in statement:
            return [("source", "aidp_lab", "bronze", "customers", "email", "DENY", "DEFAULT", "*", 10)]
        if "lineage_propagation" in statement:
            return [("source", "target", "INHERIT", 20)]
        return [
            ("source", "aidp_lab", "bronze", "customers", "email"),
            ("target", "aidp_lab", "gold", "customers", "email"),
        ]


def test_lineage_propagates_policy_with_explicit_rule_and_priority() -> None:
    policies = PolicyStore().policies()
    inherited = next(policy for policy in policies if policy.inherited)
    assert (inherited.schema, inherited.action, inherited.priority) == ("gold", "DENY", 20)
