from governance_gateway.policy import ColumnPolicy, ColumnRef, PolicyAction, Principal
from governance_gateway.service import GovernanceService
from governance_gateway.store import CONTROL_TABLES, MemoryControlStore, delta_schema_ddl


def test_registered_query_is_governed_and_audited_without_sql_or_rows() -> None:
    store = MemoryControlStore()
    store.initialize()
    column = ColumnRef("aidp_lab", "oci_gold", "sales", "amount")
    store.register_query("sales_summary", "SELECT * FROM aidp_lab.oci_gold.sales", [column])
    store.replace_policies([ColumnPolicy("aidp_lab", "oci_gold", "sales", "amount", PolicyAction.NULL)])
    service = GovernanceService(store, lambda sql, _: (["amount"], [(None,)]))
    result = service.execute_registered("sales_summary", Principal("developer", roles=frozenset({"AIDP_DEVELOPER"})), {})
    assert result.rows == ((None,),)
    assert result.affected_columns == ("amount",)
    assert "sql" not in store.audit_events[0]
    assert "rows" not in store.audit_events[0]


def test_delta_contract_contains_every_generic_control_table() -> None:
    ddl = "\n".join(delta_schema_ddl("aidp_lab"))
    assert all(f".data_governance.{table}" in ddl.replace("`", "") for table in CONTROL_TABLES)
    assert "referenced_object_ids ARRAY<STRING>" in ddl
    assert "classification STRING" in ddl
    assert "lineage_propagation" in ddl and "source_version STRING" in ddl


def test_delta_contract_uses_a_separate_object_storage_path_per_control_table() -> None:
    location = "oci://oci_artifact@namespace/data_governance"
    ddl = delta_schema_ddl("aidp_lab", location)
    for table in CONTROL_TABLES:
        assert any(f"LOCATION '{location}/{table}'" in statement for statement in ddl)
    unsafe_locations = (
        "oci://oci_artifact@namespace/data_governance'; DROP TABLE secrets; --",
        "oci://oci_artifact:secret@namespace/data_governance",
    )
    for unsafe_location in unsafe_locations:
        try:
            delta_schema_ddl("aidp_lab", unsafe_location)
        except ValueError as error:
            assert "safe OCI Object Storage URI" in str(error)
        else:
            raise AssertionError("An unsafe governance control location was accepted")


def test_masking_happens_before_rows_leave_the_service() -> None:
    store = MemoryControlStore()
    column = ColumnRef("aidp_lab", "oci_gold", "customers", "email")
    store.register_query(
        "emails", "SELECT email FROM aidp_lab.oci_gold.customers", [column],
        {"properties": {"email": {"type": "string"}}},
    )
    store.replace_policies([ColumnPolicy("aidp_lab", "oci_gold", "customers", "email", PolicyAction.MASK)])
    service = GovernanceService(store, lambda _sql, _parameters: (["email"], [("secret@example.com",)]))
    result = service.execute_registered("emails", Principal("developer"), {})
    assert result.rows == (("***",),)
    assert "secret@example.com" not in repr(store.audit_events)


def test_query_failures_do_not_return_driver_errors_or_bound_values() -> None:
    store = MemoryControlStore()
    column = ColumnRef("aidp_lab", "oci_gold", "customers", "email")
    store.register_query(
        "emails", "SELECT email FROM aidp_lab.oci_gold.customers", [column],
        {"properties": {"email": {"type": "string"}}},
    )
    store.replace_policies([ColumnPolicy("aidp_lab", "oci_gold", "customers", "email", PolicyAction.ALLOW)])

    def fail(_sql: str, _parameters: dict[str, object]) -> tuple[list[str], list[tuple[object, ...]]]:
        raise RuntimeError("driver echoed secret@example.com")

    service = GovernanceService(store, fail)
    try:
        service.execute_registered("emails", Principal("developer"), {"email": "secret@example.com"})
    except RuntimeError as error:
        assert str(error) == "The governed query failed without returning data."


def test_tokenization_policy_returns_only_tokens() -> None:
    store = MemoryControlStore()
    column = ColumnRef("aidp_lab", "oci_gold", "customers", "email")
    store.register_query("emails", "SELECT email FROM aidp_lab.oci_gold.customers", [column])
    store.replace_policies([ColumnPolicy("aidp_lab", "oci_gold", "customers", "email", PolicyAction.TOKENIZE)])
    seen: list[tuple[str, str]] = []

    def tokenize(value: str, principal: Principal) -> str:
        seen.append((value, principal.subject))
        return "aidptok_v1_example"

    service = GovernanceService(
        store, lambda _sql, _parameters: (["email"], [("secret@example.com",)]), tokenize,
    )
    result = service.execute_registered("emails", Principal("developer"), {})
    assert result.rows == (("aidptok_v1_example",),)
    assert seen == [("secret@example.com", "developer")]


def test_free_sql_audit_uses_only_a_hash_identifier() -> None:
    store = MemoryControlStore()
    column = ColumnRef("aidp_lab", "oci_gold", "customers", "customer_id")
    store.catalog["column-id"] = _catalog_column(column)
    store.replace_policies([ColumnPolicy(*column.key, PolicyAction.ALLOW)])
    store.save_sql_access(None, "USER", "developer", True, "admin-hash")
    statement = "SELECT customer_id FROM aidp_lab.oci_gold.customers"
    service = GovernanceService(store, lambda _sql, _parameters: (["customer_id"], [(101,)]))
    service.execute_free_sql(statement, Principal("developer"), {})
    query_event = next(event for event in store.audit_events if event["decision"] == "ALLOW")
    assert query_event["query_id"].startswith("free_sql:")
    assert statement not in repr(store.audit_events)


def test_registered_query_rejects_undeclared_missing_and_wrong_type_parameters() -> None:
    store = MemoryControlStore()
    column = ColumnRef("aidp_lab", "oci_gold", "customers", "customer_id")
    store.register_query(
        "customer", "SELECT customer_id FROM aidp_lab.oci_gold.customers WHERE customer_id = ?", [column],
        {"properties": {"customer_id": {"type": "integer"}}, "required": ["customer_id"]},
    )
    store.replace_policies([ColumnPolicy(*column.key, PolicyAction.ALLOW)])
    service = GovernanceService(store, lambda _sql, _parameters: (["customer_id"], [(101,)]))
    for parameters, expected in (
        ({}, "Missing required"), ({"customer_id": "101"}, "invalid type"),
        ({"customer_id": 101, "extra": True}, "Undeclared"),
    ):
        try:
            service.execute_registered("customer", Principal("developer"), parameters)
        except RuntimeError as error:
            assert expected in str(error)
        else:
            raise AssertionError("The invalid registered query parameters were accepted.")


def test_duplicate_result_columns_fail_before_data_leaves_the_gateway() -> None:
    store = MemoryControlStore()
    column = ColumnRef("aidp_lab", "oci_gold", "customers", "customer_id")
    store.register_query("duplicate", "SELECT customer_id FROM aidp_lab.oci_gold.customers", [column])
    store.replace_policies([ColumnPolicy(*column.key, PolicyAction.ALLOW)])
    service = GovernanceService(store, lambda _sql, _parameters: (["id", "ID"], [(1, 2)]))
    try:
        service.execute_registered("duplicate", Principal("developer"), {})
    except RuntimeError as error:
        assert "unique aliases" in str(error)
    else:
        raise AssertionError("Duplicate governed output columns were accepted.")


def _catalog_column(column: ColumnRef):
    from governance_gateway.catalog import CatalogColumn

    return CatalogColumn(
        column.catalog, column.schema, column.table, column.column, column.catalog, "guid",
        f"{column.catalog}.{column.schema}", f"{column.catalog}.{column.schema}.{column.table}",
        "fingerprint", 0, "long", "", "STANDARD", "2026-01-01T00:00:00Z", "owner",
    )
