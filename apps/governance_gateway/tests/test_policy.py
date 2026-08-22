import pytest

from governance_gateway.policy import (
    ColumnPolicy, ColumnRef, ForbiddenColumns, PolicyAction, Principal, effective_action, govern_select,
)


COLUMNS = [
    ColumnRef("aidp_lab", "oci_gold", "customers", "customer_id"),
    ColumnRef("aidp_lab", "oci_gold", "customers", "email"),
    ColumnRef("aidp_lab", "oci_gold", "customers", "ssn"),
]
PRINCIPAL = Principal("user@example.com", roles=frozenset({"AIDP_DEVELOPER"}))
POLICIES = [
    ColumnPolicy("aidp_lab", "oci_gold", "customers", "customer_id", PolicyAction.ALLOW),
    ColumnPolicy("aidp_lab", "oci_gold", "customers", "email", PolicyAction.MASK),
    ColumnPolicy("aidp_lab", "oci_gold", "customers", "ssn", PolicyAction.DENY),
]


def test_select_star_omits_denied_and_masks_protected_columns() -> None:
    result = govern_select("SELECT * FROM aidp_lab.oci_gold.customers", PRINCIPAL, COLUMNS, POLICIES)
    assert "ssn" not in result.sql
    assert "email" in result.sql
    assert "'***' AS email" in result.sql
    assert result.affected_columns == ("email", "ssn")
    assert result.transformations == (("email", PolicyAction.MASK),)


def test_explicit_denied_column_returns_its_name() -> None:
    with pytest.raises(ForbiddenColumns, match="ssn") as error:
        govern_select("SELECT customer_id, ssn FROM aidp_lab.oci_gold.customers", PRINCIPAL, COLUMNS, POLICIES)
    assert error.value.columns == ("ssn",)


def test_unknown_column_fails_closed() -> None:
    with pytest.raises(ForbiddenColumns, match="new_unreviewed_column"):
        govern_select("SELECT new_unreviewed_column FROM aidp_lab.oci_gold.customers", PRINCIPAL, COLUMNS, POLICIES)


def test_non_select_is_rejected() -> None:
    with pytest.raises(RuntimeError, match="read-only SELECT"):
        govern_select("DROP TABLE aidp_lab.oci_gold.customers", PRINCIPAL, COLUMNS, POLICIES)


def test_multiple_statements_are_rejected() -> None:
    with pytest.raises(RuntimeError, match="one read-only SELECT"):
        govern_select("SELECT customer_id FROM aidp_lab.oci_gold.customers; SELECT 1", PRINCIPAL, COLUMNS, POLICIES)


@pytest.mark.parametrize("clause", [
    "WHERE email = 'x'", "GROUP BY email", "ORDER BY email", "HAVING max(email) = 'x'",
])
def test_masked_column_cannot_leak_through_predicates_or_grouping(clause: str) -> None:
    with pytest.raises(ForbiddenColumns, match="email"):
        govern_select(
            f"SELECT customer_id FROM aidp_lab.oci_gold.customers {clause}", PRINCIPAL, COLUMNS, POLICIES,
        )


def test_masked_alias_is_preserved_as_the_result_contract() -> None:
    result = govern_select("SELECT email AS contact FROM aidp_lab.oci_gold.customers", PRINCIPAL, COLUMNS, POLICIES)
    assert "'***' AS contact" in result.sql
    assert result.transformations == (("contact", PolicyAction.MASK),)


def test_unregistered_table_is_rejected_even_without_projected_columns() -> None:
    with pytest.raises(RuntimeError, match="outside its governed catalog"):
        govern_select("SELECT 1 FROM aidp_lab.oci_gold.secrets", PRINCIPAL, COLUMNS, POLICIES)


def test_cte_alias_is_allowed_but_its_source_table_remains_governed() -> None:
    result = govern_select(
        "WITH allowed AS (SELECT customer_id FROM aidp_lab.oci_gold.customers) SELECT customer_id FROM allowed",
        PRINCIPAL, COLUMNS, POLICIES,
    )
    assert "WITH allowed" in result.sql


def test_inherited_restriction_cannot_be_weakened_by_a_direct_allow() -> None:
    column = COLUMNS[1]
    policies = [
        ColumnPolicy(*column.key, PolicyAction.ALLOW, "USER", PRINCIPAL.subject, 100),
        ColumnPolicy(*column.key, PolicyAction.DENY, "DEFAULT", "*", 0, True),
    ]
    assert effective_action(column, PRINCIPAL, policies) == PolicyAction.DENY
