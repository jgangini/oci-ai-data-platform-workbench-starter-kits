from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable

from sqlglot import exp, parse
from sqlglot.errors import ParseError


class PolicyAction(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    NULL = "NULL"
    MASK = "MASK"
    TOKENIZE = "TOKENIZE"


class GovernanceError(RuntimeError):
    status_code = 400


class ForbiddenColumns(GovernanceError):
    status_code = 403

    def __init__(self, columns: Iterable[str]) -> None:
        self.columns = tuple(sorted(set(columns)))
        super().__init__(f"Access denied to column(s): {', '.join(self.columns)}")


class FreeSqlForbidden(GovernanceError):
    status_code = 403


@dataclass(frozen=True)
class Principal:
    subject: str
    groups: frozenset[str] = frozenset()
    roles: frozenset[str] = frozenset()
    scopes: frozenset[str] = frozenset()

    @property
    def is_admin(self) -> bool:
        return "AI_DATA_PLATFORM_ADMIN" in self.roles


@dataclass(frozen=True)
class ColumnPolicy:
    catalog: str
    schema: str
    table: str
    column: str
    action: PolicyAction
    principal_type: str = "DEFAULT"
    principal_name: str = "*"
    priority: int = 0
    inherited: bool = False

    def applies_to(self, principal: Principal) -> bool:
        kind = self.principal_type.upper()
        return (
            kind == "DEFAULT"
            or (kind == "USER" and self.principal_name == principal.subject)
            or (kind == "GROUP" and self.principal_name in principal.groups)
            or (kind == "ROLE" and self.principal_name in principal.roles)
        )


@dataclass(frozen=True)
class ColumnRef:
    catalog: str
    schema: str
    table: str
    column: str

    @property
    def key(self) -> tuple[str, str, str, str]:
        return tuple(value.casefold() for value in (self.catalog, self.schema, self.table, self.column))


@dataclass(frozen=True)
class GovernedQuery:
    sql: str
    affected_columns: tuple[str, ...]
    transformations: tuple[tuple[str, PolicyAction], ...]


_PRECEDENCE = {"USER": 4, "GROUP": 3, "ROLE": 2, "DEFAULT": 1}
_RESTRICTIVENESS = {
    PolicyAction.DENY: 5,
    PolicyAction.NULL: 4,
    PolicyAction.TOKENIZE: 3,
    PolicyAction.MASK: 2,
    PolicyAction.ALLOW: 1,
}


def effective_action(column: ColumnRef, principal: Principal, policies: Iterable[ColumnPolicy]) -> PolicyAction:
    matches = [
        policy
        for policy in policies
        if policy.applies_to(principal)
        and tuple(value.casefold() for value in (policy.catalog, policy.schema, policy.table, policy.column)) == column.key
    ]
    if not matches:
        return PolicyAction.DENY
    direct = [item for item in matches if not item.inherited]
    inherited = [item for item in matches if item.inherited]
    winners: list[ColumnPolicy] = []
    if direct:
        winners.append(max(direct, key=lambda item: (
            _PRECEDENCE.get(item.principal_type.upper(), 0),
            item.priority,
            _RESTRICTIVENESS[item.action],
        )))
    if inherited:
        winners.append(max(inherited, key=lambda item: (
            _RESTRICTIVENESS[item.action],
            item.priority,
            _PRECEDENCE.get(item.principal_type.upper(), 0),
        )))
    return max(winners, key=lambda item: _RESTRICTIVENESS[item.action]).action


def govern_select(
    sql: str,
    principal: Principal,
    columns: Iterable[ColumnRef],
    policies: Iterable[ColumnPolicy],
) -> GovernedQuery:
    """Validate and rewrite one SELECT. Unknown columns fail closed."""
    statement = _parse_select(sql)
    catalog_columns = list(columns)
    catalog_policies = list(policies)
    _validate_tables(statement, catalog_columns)

    denied = _forbidden_references(statement, principal, catalog_columns, catalog_policies)
    if denied:
        raise ForbiddenColumns(denied)

    rewritten, affected, transformations = _rewrite_projections(
        statement,
        principal,
        catalog_columns,
        catalog_policies,
    )
    statement.set("expressions", rewritten)
    return GovernedQuery(
        statement.sql(dialect="spark"),
        tuple(sorted(affected)),
        tuple(sorted(transformations.items())),
    )


def _parse_select(sql: str) -> exp.Select:
    try:
        statements = [statement for statement in parse(sql, read="spark") if statement is not None]
    except ParseError as exc:
        raise GovernanceError("The SQL statement could not be parsed.") from exc
    if len(statements) != 1 or not isinstance(statements[0], exp.Select) or statements[0].find(exp.Command):
        raise GovernanceError("Only one read-only SELECT statement is allowed.")
    return statements[0]


def _forbidden_references(
    statement: exp.Select,
    principal: Principal,
    columns: list[ColumnRef],
    policies: list[ColumnPolicy],
) -> set[str]:
    simple_projections = {
        id(identifier)
        for projection in statement.expressions
        if (identifier := _simple_projection_column(projection)) is not None
    }
    denied: set[str] = set()
    for identifier in statement.find_all(exp.Column):
        if identifier.is_star:
            continue
        column = _resolve_column(identifier, columns)
        if column is None:
            denied.add(identifier.name)
            continue
        action = effective_action(column, principal, policies)
        if action == PolicyAction.DENY or (action != PolicyAction.ALLOW and id(identifier) not in simple_projections):
            denied.add(identifier.name)
    return denied


def _rewrite_projections(
    statement: exp.Select,
    principal: Principal,
    columns: list[ColumnRef],
    policies: list[ColumnPolicy],
) -> tuple[list[exp.Expression], set[str], dict[str, PolicyAction]]:
    rewritten: list[exp.Expression] = []
    affected: set[str] = set()
    transformations: dict[str, PolicyAction] = {}
    for projection in statement.expressions:
        if isinstance(projection, exp.Star) or (isinstance(projection, exp.Column) and projection.is_star):
            rewritten.extend(
                _rewrite_star_projection(projection, principal, columns, policies, affected, transformations)
            )
            continue
        rewritten.append(
            _rewrite_simple_projection(projection, principal, columns, policies, affected, transformations)
        )

    if not rewritten:
        raise ForbiddenColumns(column.column for column in columns)
    return rewritten, affected, transformations


def _rewrite_star_projection(
    projection: exp.Expression,
    principal: Principal,
    columns: list[ColumnRef],
    policies: list[ColumnPolicy],
    affected: set[str],
    transformations: dict[str, PolicyAction],
) -> list[exp.Expression]:
    rewritten: list[exp.Expression] = []
    for column in _star_columns(projection, columns):
        action = effective_action(column, principal, policies)
        if action == PolicyAction.DENY:
            affected.add(column.column)
            continue
        rewritten.append(_protected_projection(_qualified_column(column), column.column, action))
        if action != PolicyAction.ALLOW:
            affected.add(column.column)
            _add_transformation(transformations, column.column, action)
    return rewritten


def _rewrite_simple_projection(
    projection: exp.Expression,
    principal: Principal,
    columns: list[ColumnRef],
    policies: list[ColumnPolicy],
    affected: set[str],
    transformations: dict[str, PolicyAction],
) -> exp.Expression:
    identifier = _simple_projection_column(projection)
    if identifier is None:
        return projection
    column = _resolve_column(identifier, columns)
    if column is None:
        raise ForbiddenColumns([identifier.name])
    action = effective_action(column, principal, policies)
    if action == PolicyAction.ALLOW:
        return projection
    output_name = projection.alias_or_name or identifier.name
    affected.add(column.column)
    _add_transformation(transformations, output_name, action)
    return _protected_projection(identifier.copy(), output_name, action)


def _protected_projection(identifier: exp.Column, output_name: str, action: PolicyAction) -> exp.Expression:
    if action == PolicyAction.ALLOW:
        return identifier
    if action == PolicyAction.NULL:
        return exp.alias_(exp.Null(), output_name)
    if action == PolicyAction.MASK:
        return exp.alias_(exp.Literal.string("***"), output_name)
    return exp.alias_(identifier, output_name)


def _simple_projection_column(projection: exp.Expression) -> exp.Column | None:
    if isinstance(projection, exp.Column) and not projection.is_star:
        return projection
    if isinstance(projection, exp.Alias) and isinstance(projection.this, exp.Column) and not projection.this.is_star:
        return projection.this
    return None


def _resolve_column(identifier: exp.Column, columns: list[ColumnRef]) -> ColumnRef | None:
    candidates = [column for column in columns if column.column.casefold() == identifier.name.casefold()]
    qualifiers = [identifier.table, identifier.db, identifier.catalog]
    expected = ["table", "schema", "catalog"]
    for qualifier, attribute in zip(qualifiers, expected):
        if qualifier:
            candidates = [
                column for column in candidates
                if getattr(column, attribute).casefold() == qualifier.casefold()
            ]
    return candidates[0] if len(candidates) == 1 else None


def _validate_tables(statement: exp.Select, columns: list[ColumnRef]) -> None:
    known = {(column.catalog.casefold(), column.schema.casefold(), column.table.casefold()) for column in columns}
    cte_names = {cte.alias_or_name.casefold() for cte in statement.find_all(exp.CTE)}
    for table in statement.find_all(exp.Table):
        if not table.db and not table.catalog and table.name.casefold() in cte_names:
            continue
        matches = [item for item in known if item[2] == table.name.casefold()]
        if table.db:
            matches = [item for item in matches if item[1] == table.db.casefold()]
        if table.catalog:
            matches = [item for item in matches if item[0] == table.catalog.casefold()]
        if not matches:
            raise GovernanceError("The registered query references a table outside its governed catalog contract.")


def _star_columns(projection: exp.Expression, columns: list[ColumnRef]) -> list[ColumnRef]:
    if not isinstance(projection, exp.Column):
        return columns
    matches = columns
    if projection.table:
        matches = [column for column in matches if column.table.casefold() == projection.table.casefold()]
    if projection.db:
        matches = [column for column in matches if column.schema.casefold() == projection.db.casefold()]
    if projection.catalog:
        matches = [column for column in matches if column.catalog.casefold() == projection.catalog.casefold()]
    if not matches:
        raise GovernanceError("The wildcard references a table outside its governed catalog contract.")
    return matches


def _qualified_column(column: ColumnRef) -> exp.Column:
    return exp.column(column.column, table=column.table, db=column.schema, catalog=column.catalog)


def _add_transformation(
    transformations: dict[str, PolicyAction], output_name: str, action: PolicyAction
) -> None:
    if output_name.casefold() in {name.casefold() for name in transformations}:
        raise GovernanceError("Protected result columns must use unique aliases.")
    transformations[output_name] = action
