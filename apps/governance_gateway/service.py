from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any, Callable

from .policy import FreeSqlForbidden, GovernanceError, GovernedQuery, PolicyAction, Principal, govern_select
from .store import ControlStore


@dataclass(frozen=True)
class QueryResult:
    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    elapsed_ms: int
    affected_columns: tuple[str, ...]


class GovernanceService:
    def __init__(
        self,
        store: ControlStore,
        execute_sql: Callable[[str, dict[str, Any]], tuple[list[str], list[tuple[Any, ...]]]],
        tokenize: Callable[[str, Principal], str] | None = None,
    ) -> None:
        self.store = store
        self.execute_sql = execute_sql
        self.tokenize = tokenize

    def execute_registered(self, query_id: str, principal: Principal, parameters: dict[str, Any]) -> QueryResult:
        sql = self.store.registered_query(query_id)
        if sql is None:
            self._audit(principal, "DENY", query_id, ())
            raise GovernanceError("The requested query_id is not registered or is disabled.")
        validate_parameters(parameters, self.store.query_parameter_schema(query_id))
        governed = govern_select(sql, principal, self.store.catalog_columns(query_id), self.store.policies())
        return self._execute(governed, principal, query_id, parameters)

    def execute_free_sql(self, sql: str, principal: Principal, parameters: dict[str, Any]) -> QueryResult:
        query_id = f"free_sql:{hashlib.sha256(sql.encode('utf-8')).hexdigest()}"
        if not self.store.can_use_free_sql(principal):
            self._audit(principal, "DENY", query_id, ())
            raise FreeSqlForbidden("Free SQL has not been granted to this principal.")
        governed = govern_select(sql, principal, self.store.all_catalog_columns(), self.store.policies())
        return self._execute(governed, principal, query_id, parameters)

    def _execute(
        self, governed: GovernedQuery, principal: Principal, query_id: str, parameters: dict[str, Any]
    ) -> QueryResult:
        started = time.monotonic()
        try:
            columns, rows = self.execute_sql(governed.sql, parameters)
        except Exception:
            self._audit(principal, "ERROR", query_id, governed.affected_columns)
            raise GovernanceError("The governed query failed without returning data.") from None
        if len({column.casefold() for column in columns}) != len(columns):
            self._audit(principal, "ERROR", query_id, governed.affected_columns)
            raise GovernanceError("The governed query returned duplicate column names; use unique aliases.")
        rows = self._redact(columns, rows, dict(governed.transformations), principal)
        elapsed = int((time.monotonic() - started) * 1000)
        self._audit(principal, "ALLOW", query_id, governed.affected_columns)
        return QueryResult(tuple(columns), tuple(tuple(row) for row in rows), elapsed, governed.affected_columns)

    def explain(self, query_id: str, principal: Principal) -> GovernedQuery:
        sql = self.store.registered_query(query_id)
        if sql is None:
            raise GovernanceError("The requested query_id is not registered or is disabled.")
        return govern_select(sql, principal, self.store.catalog_columns(query_id), self.store.policies())

    def _audit(self, principal: Principal, decision: str, query_id: str, affected: tuple[str, ...]) -> None:
        self.store.audit({
            "principal": hashlib.sha256(principal.subject.encode("utf-8")).hexdigest(),
            "decision": decision,
            "query_id": query_id,
            "affected_columns": list(affected),
        })

    def _redact(
        self,
        columns: list[str],
        rows: list[tuple[Any, ...]],
        transformations: dict[str, PolicyAction],
        principal: Principal,
    ) -> list[tuple[Any, ...]]:
        indexes = {name.casefold(): index for index, name in enumerate(columns)}
        result: list[tuple[Any, ...]] = []
        for source in rows:
            row = list(source)
            for name, action in transformations.items():
                index = indexes.get(name.casefold())
                if index is None:
                    raise GovernanceError("The query result did not match the governed column contract.")
                value = row[index]
                if action == PolicyAction.NULL:
                    row[index] = None
                elif action == PolicyAction.MASK:
                    row[index] = None if value is None else "***"
                elif action == PolicyAction.TOKENIZE:
                    if self.tokenize is None:
                        raise GovernanceError("Tokenization is not configured.")
                    row[index] = None if value is None else self.tokenize(str(value), principal)
            result.append(tuple(row))
        return result


def validate_parameters(parameters: dict[str, Any], schema: dict[str, Any]) -> None:
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    if not isinstance(properties, dict) or not isinstance(required, list) or not all(
        isinstance(name, str) for name in required
    ):
        raise GovernanceError("The registered query has an invalid parameter schema.")
    unknown = sorted(set(parameters) - set(properties))
    missing = sorted(set(required) - set(parameters))
    if unknown:
        raise GovernanceError(f"Undeclared query parameters: {', '.join(unknown)}.")
    if missing:
        raise GovernanceError(f"Missing required query parameters: {', '.join(missing)}.")
    for name, value in parameters.items():
        rule = properties[name]
        if not isinstance(rule, dict) or not _matches_type(value, rule.get("type")):
            raise GovernanceError(f"Query parameter {name} has an invalid type.")
        if "enum" in rule and (not isinstance(rule["enum"], list) or value not in rule["enum"]):
            raise GovernanceError(f"Query parameter {name} has an invalid value.")


def _matches_type(value: Any, expected: Any) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return False
