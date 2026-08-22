from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Callable, Iterable, Protocol
from urllib.parse import urlparse

from .policy import ColumnPolicy, ColumnRef, PolicyAction, Principal
from .catalog import CatalogColumn, CatalogSnapshot, new_object_id


CONTROL_TABLES = (
    "data_governance", "access_policy", "lineage_propagation", "query_registry",
    "token_vault", "governance_audit", "sync_state", "sql_access",
)


class ControlStore(Protocol):
    def initialize(self) -> None: ...
    def catalog_columns(self, query_id: str) -> list[ColumnRef]: ...
    def policies(self) -> list[ColumnPolicy]: ...
    def registered_query(self, query_id: str) -> str | None: ...
    def query_parameter_schema(self, query_id: str) -> dict[str, Any]: ...
    def audit(self, event: dict[str, Any]) -> None: ...
    def apply_catalog_snapshot(self, snapshot: CatalogSnapshot) -> dict[str, int]: ...
    def record_sync_failure(self, source: str) -> None: ...
    def sync_status(self) -> dict[str, str]: ...
    def catalog_records(self) -> list[dict[str, Any]]: ...
    def update_classification(
        self, object_id: str, classification: str, sensitivity: str, owner: str, review_state: str,
        principal_hash: str,
    ) -> None: ...
    def list_access_policies(self, object_id: str | None = None) -> list[dict[str, Any]]: ...
    def save_access_policy(
        self, policy_id: str | None, object_id: str, principal_type: str, principal_name: str,
        action: str, priority: int, enabled: bool, principal_hash: str,
    ) -> dict[str, Any]: ...
    def disable_access_policy(self, policy_id: str, principal_hash: str) -> None: ...
    def save_token(self, token_id: str, ciphertext: str, key_version: str) -> None: ...
    def load_token(self, token_id: str) -> tuple[str, str]: ...
    def all_catalog_columns(self) -> list[ColumnRef]: ...
    def registered_queries(self) -> list[dict[str, Any]]: ...
    def can_use_free_sql(self, principal: Principal) -> bool: ...
    def list_sql_access(self) -> list[dict[str, Any]]: ...
    def save_sql_access(
        self, grant_id: str | None, principal_type: str, principal_name: str, enabled: bool, principal_hash: str,
    ) -> dict[str, Any]: ...
    def disable_sql_access(self, grant_id: str, principal_hash: str) -> None: ...


class MemoryControlStore:
    def __init__(self) -> None:
        self._lock = RLock()
        self._policies: list[ColumnPolicy] = []
        self._queries: dict[str, tuple[str, list[ColumnRef], dict[str, Any]]] = {}
        self.audit_events: list[dict[str, Any]] = []
        self.catalog: dict[str, CatalogColumn] = {}
        self._sync: dict[str, str] = {}
        self._classifications: dict[str, dict[str, str]] = {}
        self._access_policies: dict[str, dict[str, Any]] = {}
        self._tokens: dict[str, tuple[str, str]] = {}
        self._sql_access: dict[str, dict[str, Any]] = {}

    def initialize(self) -> None:
        return None

    def register_query(
        self, query_id: str, sql: str, columns: Iterable[ColumnRef], parameter_schema: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            self._queries[query_id] = (sql, list(columns), dict(parameter_schema or {}))

    def replace_policies(self, policies: Iterable[ColumnPolicy]) -> None:
        with self._lock:
            self._policies = list(policies)

    def catalog_columns(self, query_id: str) -> list[ColumnRef]:
        with self._lock:
            return list(self._queries.get(query_id, ("", [], {}))[1])

    def policies(self) -> list[ColumnPolicy]:
        with self._lock:
            return list(self._policies)

    def registered_query(self, query_id: str) -> str | None:
        with self._lock:
            item = self._queries.get(query_id)
            return item[0] if item else None

    def query_parameter_schema(self, query_id: str) -> dict[str, Any]:
        with self._lock:
            item = self._queries.get(query_id)
            return dict(item[2]) if item else {}

    def audit(self, event: dict[str, Any]) -> None:
        safe = {key: value for key, value in event.items() if key not in {"sql", "rows", "token", "credential"}}
        safe["time"] = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self.audit_events.append(safe)

    def apply_catalog_snapshot(self, snapshot: CatalogSnapshot) -> dict[str, int]:
        with self._lock:
            self.catalog = {new_object_id(column): column for column in snapshot.columns}
            self._sync = {
                "source": snapshot.source,
                "snapshot_version": snapshot.version,
                "content_hash": snapshot.content_hash,
                "status": "SUCCESS",
            }
            return {
                "columns": len(snapshot.columns), "new": len(snapshot.columns), "renamed": 0,
                "deleted": 0, "lineage_edges": len(snapshot.lineage),
            }

    def record_sync_failure(self, source: str) -> None:
        with self._lock:
            self._sync = {"source": source, "status": "FAILED"}

    def sync_status(self) -> dict[str, str]:
        with self._lock:
            return dict(self._sync)

    def catalog_records(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "object_id": object_id,
                    "catalog": column.catalog_name,
                    "schema": column.schema_name,
                    "table": column.table_name,
                    "column": column.column_name,
                    **self._classifications.get(object_id, {
                        "classification": "UNCLASSIFIED", "sensitivity": "UNCLASSIFIED",
                        "owner": "", "review_state": "UNREVIEWED",
                    }),
                }
                for object_id, column in self.catalog.items()
            ]

    def update_classification(
        self, object_id: str, classification: str, sensitivity: str, owner: str, review_state: str,
        principal_hash: str,
    ) -> None:
        del principal_hash
        with self._lock:
            if object_id not in self.catalog:
                raise KeyError("The catalog column does not exist or is deleted.")
            self._classifications[object_id] = {
                "classification": classification, "sensitivity": sensitivity,
                "owner": owner, "review_state": review_state,
            }

    def list_access_policies(self, object_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            return [
                dict(item) for item in self._access_policies.values()
                if object_id is None or item["object_id"] == object_id
            ]

    def save_access_policy(
        self, policy_id: str | None, object_id: str, principal_type: str, principal_name: str,
        action: str, priority: int, enabled: bool, principal_hash: str,
    ) -> dict[str, Any]:
        with self._lock:
            column = self.catalog.get(object_id)
            if column is None:
                raise KeyError("The catalog column does not exist or is deleted.")
            if policy_id is not None and policy_id not in self._access_policies:
                raise KeyError("The access policy does not exist.")
            identifier = policy_id or str(uuid.uuid4())
            record = {
                "policy_id": identifier, "object_id": object_id, "principal_type": principal_type,
                "principal_name": principal_name, "action": action, "priority": priority, "enabled": enabled,
            }
            self._access_policies[identifier] = record
            self._policies = [
                ColumnPolicy(
                    item.catalog_name, item.schema_name, item.table_name, item.column_name,
                    _policy_action(policy["action"]), policy["principal_type"], policy["principal_name"],
                    policy["priority"], False,
                )
                for policy in self._access_policies.values()
                if policy["enabled"] and (item := self.catalog.get(policy["object_id"])) is not None
            ]
            self.audit({
                "principal": principal_hash, "decision": "POLICY_UPDATED" if policy_id else "POLICY_CREATED",
                "affected_columns": [object_id], "policy_version": identifier,
            })
            return dict(record)

    def disable_access_policy(self, policy_id: str, principal_hash: str) -> None:
        with self._lock:
            current = self._access_policies.get(policy_id)
            if current is None:
                raise KeyError("The access policy does not exist.")
            current["enabled"] = False
            self._policies = [
                ColumnPolicy(
                    item.catalog_name, item.schema_name, item.table_name, item.column_name,
                    _policy_action(policy["action"]), policy["principal_type"], policy["principal_name"],
                    policy["priority"], False,
                )
                for policy in self._access_policies.values()
                if policy["enabled"] and (item := self.catalog.get(policy["object_id"])) is not None
            ]
            self.audit({
                "principal": principal_hash, "decision": "POLICY_DISABLED",
                "affected_columns": [current["object_id"]], "policy_version": policy_id,
            })

    def save_token(self, token_id: str, ciphertext: str, key_version: str) -> None:
        with self._lock:
            if token_id in self._tokens:
                raise RuntimeError("The generated token identifier already exists.")
            self._tokens[token_id] = (ciphertext, key_version)

    def load_token(self, token_id: str) -> tuple[str, str]:
        with self._lock:
            try:
                return self._tokens[token_id]
            except KeyError as exc:
                raise KeyError("The token does not exist.") from exc

    def all_catalog_columns(self) -> list[ColumnRef]:
        with self._lock:
            return [
                ColumnRef(item.catalog_name, item.schema_name, item.table_name, item.column_name)
                for item in self.catalog.values()
            ]

    def registered_queries(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {"query_id": query_id, "parameter_schema": dict(value[2])}
                for query_id, value in sorted(self._queries.items())
            ]

    def can_use_free_sql(self, principal: Principal) -> bool:
        with self._lock:
            return any(
                item["enabled"] and _principal_matches(
                    item["principal_type"], item["principal_name"], principal,
                )
                for item in self._sql_access.values()
            )

    def list_sql_access(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._sql_access.values()]

    def save_sql_access(
        self, grant_id: str | None, principal_type: str, principal_name: str, enabled: bool, principal_hash: str,
    ) -> dict[str, Any]:
        with self._lock:
            if grant_id is not None and grant_id not in self._sql_access:
                raise KeyError("The free SQL grant does not exist.")
            identifier = grant_id or str(uuid.uuid4())
            record = {
                "grant_id": identifier, "principal_type": principal_type,
                "principal_name": principal_name, "enabled": enabled,
            }
            self._sql_access[identifier] = record
            self.audit({
                "principal": principal_hash, "decision": "FREE_SQL_UPDATED" if grant_id else "FREE_SQL_GRANTED",
                "affected_columns": [], "policy_version": identifier,
            })
            return dict(record)

    def disable_sql_access(self, grant_id: str, principal_hash: str) -> None:
        with self._lock:
            current = self._sql_access.get(grant_id)
            if current is None:
                raise KeyError("The free SQL grant does not exist.")
            current["enabled"] = False
            self.audit({
                "principal": principal_hash, "decision": "FREE_SQL_DISABLED",
                "affected_columns": [], "policy_version": grant_id,
            })


def delta_schema_ddl(catalog: str, control_location: str = "") -> tuple[str, ...]:
    prefix = f"`{catalog}`.`oci_control`"
    location = _control_location(control_location)

    def table(name: str, columns: str) -> str:
        target = f" LOCATION '{location}/{name}'" if location else ""
        return f"CREATE TABLE IF NOT EXISTS {prefix}.{name} ({columns}) USING DELTA{target}"

    return (
        f"CREATE SCHEMA IF NOT EXISTS {prefix}",
        table("data_governance", "object_id STRING, catalog_name STRING, schema_name STRING, table_name STRING, column_name STRING, classification STRING, sensitivity STRING, owner STRING, review_state STRING, deleted BOOLEAN, source_version STRING, updated_at TIMESTAMP, source_system STRING, source_catalog_key STRING, source_catalog_guid STRING, source_schema_key STRING, source_table_key STRING, source_table_fingerprint STRING, source_column_ordinal INT, source_column_type STRING, source_column_description STRING, source_entity_type STRING, source_created_at STRING, source_created_by STRING, identity_status STRING, first_seen_at TIMESTAMP"),
        table("access_policy", "policy_id STRING, object_id STRING, principal_type STRING, principal_name STRING, action STRING, priority INT, enabled BOOLEAN, updated_by STRING, updated_at TIMESTAMP"),
        table("lineage_propagation", "rule_id STRING, source_object_id STRING, target_object_id STRING, action STRING, priority INT, enabled BOOLEAN, updated_at TIMESTAMP, source_system STRING, source_version STRING, deleted BOOLEAN, updated_by STRING"),
        table("query_registry", "query_id STRING, statement STRING, parameter_schema STRING, referenced_object_ids ARRAY<STRING>, enabled BOOLEAN, updated_by STRING, updated_at TIMESTAMP"),
        table("token_vault", "token_id STRING, ciphertext STRING, key_version STRING, created_at TIMESTAMP"),
        table("governance_audit", "event_id STRING, principal STRING, decision STRING, query_id STRING, affected_columns ARRAY<STRING>, policy_version STRING, event_time TIMESTAMP"),
        table("sync_state", "source STRING, snapshot_version STRING, content_hash STRING, status STRING, updated_at TIMESTAMP"),
        table("sql_access", "grant_id STRING, principal_type STRING, principal_name STRING, enabled BOOLEAN, updated_by STRING, updated_at TIMESTAMP"),
    )


class JdbcControlStore:
    """Persist the control plane in AIDP Delta tables through the Simba JDBC endpoint."""

    def __init__(self, connect: Callable[[], Any], catalog: str, control_location: str = "") -> None:
        self._connect = connect
        self._catalog = _identifier(catalog)
        self._control_location = _control_location(control_location)
        self._prefix = f"`{self._catalog}`.`oci_control`"

    def initialize(self) -> None:
        self._write_many(delta_schema_ddl(self._catalog, self._control_location))
        self._migrate_data_governance()
        self._migrate_lineage()

    def apply_catalog_snapshot(self, snapshot: CatalogSnapshot) -> dict[str, int]:
        connection = self._connect()
        cursor = connection.cursor()
        try:
            existing = self._existing_catalog_columns(cursor, snapshot.source)
            resolved, new_count, renamed_count = _resolve_object_ids(snapshot.columns, existing)
            for column, object_id, identity_status in resolved:
                cursor.execute(
                    f"""
                    MERGE INTO {self._prefix}.data_governance target
                    USING (SELECT ? object_id) source
                    ON target.object_id = source.object_id
                    WHEN MATCHED THEN UPDATE SET
                      catalog_name = ?, schema_name = ?, table_name = ?, column_name = ?, deleted = false,
                      source_version = ?, updated_at = current_timestamp(), source_system = ?,
                      source_catalog_key = ?, source_catalog_guid = ?, source_schema_key = ?, source_table_key = ?,
                      source_table_fingerprint = ?, source_column_ordinal = ?, source_column_type = ?,
                      source_column_description = ?, source_entity_type = ?, source_created_at = ?,
                      source_created_by = ?, identity_status = ?
                    WHEN NOT MATCHED THEN INSERT (
                      object_id, catalog_name, schema_name, table_name, column_name, classification, sensitivity, owner,
                      review_state, deleted, source_version, updated_at, source_system, source_catalog_key,
                      source_catalog_guid, source_schema_key, source_table_key, source_table_fingerprint,
                      source_column_ordinal, source_column_type, source_column_description, source_entity_type,
                      source_created_at, source_created_by, identity_status, first_seen_at
                    ) VALUES (
                      ?, ?, ?, ?, ?, 'UNCLASSIFIED', 'UNCLASSIFIED', NULL, 'UNREVIEWED', false, ?, current_timestamp(), ?,
                      ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, current_timestamp()
                    )
                    """,
                    _merge_parameters(column, object_id, identity_status, snapshot),
                )

            lineage_count = self._apply_lineage_snapshot(cursor, snapshot, resolved)

            cursor.execute(
                f"""
                UPDATE {self._prefix}.data_governance
                SET deleted = true, updated_at = current_timestamp()
                WHERE source_system = ? AND source_version <> ? AND deleted = false
                """,
                (snapshot.source, snapshot.version),
            )
            deleted_count = max(int(getattr(cursor, "rowcount", 0) or 0), 0)
            self._insert_sync_state(cursor, snapshot.source, snapshot.version, snapshot.content_hash, "SUCCESS")
            connection.commit()
            return {
                "columns": len(snapshot.columns),
                "new": new_count,
                "renamed": renamed_count,
                "deleted": deleted_count,
                "lineage_edges": lineage_count,
            }
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def record_sync_failure(self, source: str) -> None:
        connection = self._connect()
        cursor = connection.cursor()
        try:
            self._insert_sync_state(
                cursor, source, datetime.now(timezone.utc).isoformat(timespec="microseconds"), "", "FAILED"
            )
            connection.commit()
        finally:
            cursor.close()
            connection.close()

    def sync_status(self) -> dict[str, str]:
        rows = self._read(
            f"""
            SELECT source, snapshot_version, content_hash, status
            FROM {self._prefix}.sync_state
            WHERE source = ?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            ("AIDP_MASTER_CATALOG",),
        )
        if not rows:
            return {"status": "NOT_STARTED"}
        source, version, content_hash, status = rows[0]
        return {
            "source": str(source or ""),
            "snapshot_version": str(version or ""),
            "content_hash": str(content_hash or ""),
            "status": str(status or ""),
        }

    def catalog_columns(self, query_id: str) -> list[ColumnRef]:
        rows = self._read(
            f"""
            SELECT DISTINCT g.catalog_name, g.schema_name, g.table_name, g.column_name
            FROM {self._prefix}.query_registry q
            JOIN {self._prefix}.data_governance g
              ON array_contains(q.referenced_object_ids, g.object_id)
            WHERE q.query_id = ? AND q.enabled = true AND g.deleted = false
            ORDER BY g.catalog_name, g.schema_name, g.table_name, g.column_name
            """,
            (query_id,),
        )
        return [ColumnRef(*(str(value) for value in row)) for row in rows]

    def policies(self) -> list[ColumnPolicy]:
        rows = self._read(
            f"""
            SELECT g.object_id, g.catalog_name, g.schema_name, g.table_name, g.column_name,
                   p.action, p.principal_type, p.principal_name, p.priority
            FROM {self._prefix}.access_policy p
            JOIN {self._prefix}.data_governance g ON p.object_id = g.object_id
            WHERE p.enabled = true AND g.deleted = false
            """
        )
        direct: list[tuple[str, ColumnPolicy]] = []
        for object_id, catalog, schema, table, column, action, principal_type, principal_name, priority in rows:
            direct.append((str(object_id), ColumnPolicy(
                str(catalog), str(schema), str(table), str(column), _policy_action(action),
                str(principal_type), str(principal_name), int(priority), False,
            )))
        return [policy for _, policy in direct] + self._propagated_policies(direct)

    def catalog_records(self) -> list[dict[str, Any]]:
        rows = self._read(
            f"""
            SELECT object_id, catalog_name, schema_name, table_name, column_name, classification,
                   sensitivity, owner, review_state, source_column_type, identity_status, deleted
            FROM {self._prefix}.data_governance
            WHERE source_system = ?
            ORDER BY catalog_name, schema_name, table_name, source_column_ordinal, column_name
            """,
            ("AIDP_MASTER_CATALOG",),
        )
        names = (
            "object_id", "catalog", "schema", "table", "column", "classification", "sensitivity",
            "owner", "review_state", "data_type", "identity_status", "deleted",
        )
        return [{name: value for name, value in zip(names, row)} for row in rows]

    def update_classification(
        self, object_id: str, classification: str, sensitivity: str, owner: str, review_state: str,
        principal_hash: str,
    ) -> None:
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute(
                f"SELECT object_id FROM {self._prefix}.data_governance WHERE object_id = ? AND deleted = false",
                (object_id,),
            )
            rows = cursor.fetchall()
            if len(rows) != 1:
                raise KeyError("The catalog column does not exist or is deleted.")
            cursor.execute(
                f"""
                UPDATE {self._prefix}.data_governance
                SET classification = ?, sensitivity = ?, owner = ?, review_state = ?, updated_at = current_timestamp()
                WHERE object_id = ? AND deleted = false
                """,
                (classification, sensitivity, owner, review_state, object_id),
            )
            cursor.execute(
                f"""
                INSERT INTO {self._prefix}.governance_audit
                SELECT ?, ?, 'CLASSIFICATION_UPDATED', '', from_json(?, 'array<string>'), 'current', current_timestamp()
                """,
                (str(uuid.uuid4()), principal_hash, json.dumps([object_id], separators=(",", ":"))),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def list_access_policies(self, object_id: str | None = None) -> list[dict[str, Any]]:
        parameters: tuple[Any, ...] = () if object_id is None else (object_id,)
        where = "" if object_id is None else "WHERE p.object_id = ?"
        rows = self._read(
            f"""
            SELECT p.policy_id, p.object_id, g.catalog_name, g.schema_name, g.table_name, g.column_name,
                   p.principal_type, p.principal_name, p.action, p.priority, p.enabled, p.updated_at
            FROM {self._prefix}.access_policy p
            JOIN {self._prefix}.data_governance g ON p.object_id = g.object_id
            {where}
            ORDER BY g.catalog_name, g.schema_name, g.table_name, g.column_name,
                     p.principal_type, p.principal_name, p.priority DESC, p.policy_id
            """,
            parameters,
        )
        names = (
            "policy_id", "object_id", "catalog", "schema", "table", "column", "principal_type",
            "principal_name", "action", "priority", "enabled", "updated_at",
        )
        return [{name: value for name, value in zip(names, row)} for row in rows]

    def save_access_policy(
        self, policy_id: str | None, object_id: str, principal_type: str, principal_name: str,
        action: str, priority: int, enabled: bool, principal_hash: str,
    ) -> dict[str, Any]:
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute(
                f"SELECT object_id FROM {self._prefix}.data_governance WHERE object_id = ? AND deleted = false",
                (object_id,),
            )
            if len(cursor.fetchall()) != 1:
                raise KeyError("The catalog column does not exist or is deleted.")
            if policy_id is not None:
                cursor.execute(
                    f"SELECT policy_id FROM {self._prefix}.access_policy WHERE policy_id = ?",
                    (policy_id,),
                )
                if len(cursor.fetchall()) != 1:
                    raise KeyError("The access policy does not exist.")
            identifier = policy_id or str(uuid.uuid4())
            cursor.execute(
                f"""
                MERGE INTO {self._prefix}.access_policy target
                USING (SELECT ? policy_id) source
                ON target.policy_id = source.policy_id
                WHEN MATCHED THEN UPDATE SET object_id = ?, principal_type = ?, principal_name = ?, action = ?,
                  priority = ?, enabled = ?, updated_by = ?, updated_at = current_timestamp()
                WHEN NOT MATCHED THEN INSERT (
                  policy_id, object_id, principal_type, principal_name, action, priority, enabled, updated_by, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, current_timestamp())
                """,
                (
                    identifier, object_id, principal_type, principal_name, action, priority, enabled, principal_hash,
                    identifier, object_id, principal_type, principal_name, action, priority, enabled, principal_hash,
                ),
            )
            cursor.execute(
                f"""
                INSERT INTO {self._prefix}.governance_audit
                SELECT ?, ?, ?, '', from_json(?, 'array<string>'), ?, current_timestamp()
                """,
                (
                    str(uuid.uuid4()), principal_hash, "POLICY_UPDATED" if policy_id else "POLICY_CREATED",
                    json.dumps([object_id], separators=(",", ":")), identifier,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()
        return {
            "policy_id": identifier, "object_id": object_id, "principal_type": principal_type,
            "principal_name": principal_name, "action": action, "priority": priority, "enabled": enabled,
        }

    def disable_access_policy(self, policy_id: str, principal_hash: str) -> None:
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute(
                f"SELECT object_id FROM {self._prefix}.access_policy WHERE policy_id = ?",
                (policy_id,),
            )
            rows = cursor.fetchall()
            if len(rows) != 1:
                raise KeyError("The access policy does not exist.")
            object_id = str(rows[0][0])
            cursor.execute(
                f"""
                UPDATE {self._prefix}.access_policy
                SET enabled = false, updated_by = ?, updated_at = current_timestamp()
                WHERE policy_id = ?
                """,
                (principal_hash, policy_id),
            )
            cursor.execute(
                f"""
                INSERT INTO {self._prefix}.governance_audit
                SELECT ?, ?, 'POLICY_DISABLED', '', from_json(?, 'array<string>'), ?, current_timestamp()
                """,
                (
                    str(uuid.uuid4()), principal_hash, json.dumps([object_id], separators=(",", ":")), policy_id,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def save_token(self, token_id: str, ciphertext: str, key_version: str) -> None:
        self._write(
            f"INSERT INTO {self._prefix}.token_vault VALUES (?, ?, ?, current_timestamp())",
            (token_id, ciphertext, key_version),
        )

    def load_token(self, token_id: str) -> tuple[str, str]:
        rows = self._read(
            f"SELECT ciphertext, key_version FROM {self._prefix}.token_vault WHERE token_id = ?",
            (token_id,),
        )
        if len(rows) != 1:
            raise KeyError("The token does not exist.")
        return str(rows[0][0]), str(rows[0][1])

    def all_catalog_columns(self) -> list[ColumnRef]:
        rows = self._read(
            f"""
            SELECT catalog_name, schema_name, table_name, column_name
            FROM {self._prefix}.data_governance
            WHERE deleted = false
            ORDER BY catalog_name, schema_name, table_name, source_column_ordinal, column_name
            """
        )
        return [ColumnRef(*(str(value) for value in row)) for row in rows]

    def registered_queries(self) -> list[dict[str, Any]]:
        rows = self._read(
            f"""
            SELECT query_id, parameter_schema
            FROM {self._prefix}.query_registry
            WHERE enabled = true
            ORDER BY query_id
            """
        )
        result: list[dict[str, Any]] = []
        for query_id, parameter_schema in rows:
            try:
                schema = json.loads(str(parameter_schema or "{}"))
            except json.JSONDecodeError as exc:
                raise RuntimeError("The query registry contains an invalid parameter schema.") from exc
            result.append({"query_id": str(query_id), "parameter_schema": schema})
        return result

    def can_use_free_sql(self, principal: Principal) -> bool:
        return any(
            bool(enabled) and _principal_matches(str(kind), str(name), principal)
            for kind, name, enabled in self._read(
                f"SELECT principal_type, principal_name, enabled FROM {self._prefix}.sql_access"
            )
        )

    def list_sql_access(self) -> list[dict[str, Any]]:
        rows = self._read(
            f"""
            SELECT grant_id, principal_type, principal_name, enabled, updated_at
            FROM {self._prefix}.sql_access
            ORDER BY principal_type, principal_name, grant_id
            """
        )
        names = ("grant_id", "principal_type", "principal_name", "enabled", "updated_at")
        return [{name: value for name, value in zip(names, row)} for row in rows]

    def save_sql_access(
        self, grant_id: str | None, principal_type: str, principal_name: str, enabled: bool, principal_hash: str,
    ) -> dict[str, Any]:
        identifier = grant_id or str(uuid.uuid4())
        connection = self._connect()
        cursor = connection.cursor()
        try:
            if grant_id is not None:
                cursor.execute(f"SELECT grant_id FROM {self._prefix}.sql_access WHERE grant_id = ?", (grant_id,))
                if len(cursor.fetchall()) != 1:
                    raise KeyError("The free SQL grant does not exist.")
            cursor.execute(
                f"""
                MERGE INTO {self._prefix}.sql_access target
                USING (SELECT ? grant_id) source ON target.grant_id = source.grant_id
                WHEN MATCHED THEN UPDATE SET principal_type = ?, principal_name = ?, enabled = ?,
                  updated_by = ?, updated_at = current_timestamp()
                WHEN NOT MATCHED THEN INSERT (grant_id, principal_type, principal_name, enabled, updated_by, updated_at)
                  VALUES (?, ?, ?, ?, ?, current_timestamp())
                """,
                (
                    identifier, principal_type, principal_name, enabled, principal_hash,
                    identifier, principal_type, principal_name, enabled, principal_hash,
                ),
            )
            self._insert_audit(cursor, {
                "principal": principal_hash, "decision": "FREE_SQL_UPDATED" if grant_id else "FREE_SQL_GRANTED",
                "affected_columns": [], "policy_version": identifier,
            })
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()
        return {
            "grant_id": identifier, "principal_type": principal_type,
            "principal_name": principal_name, "enabled": enabled,
        }

    def disable_sql_access(self, grant_id: str, principal_hash: str) -> None:
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute(f"SELECT grant_id FROM {self._prefix}.sql_access WHERE grant_id = ?", (grant_id,))
            if len(cursor.fetchall()) != 1:
                raise KeyError("The free SQL grant does not exist.")
            cursor.execute(
                f"""
                UPDATE {self._prefix}.sql_access
                SET enabled = false, updated_by = ?, updated_at = current_timestamp()
                WHERE grant_id = ?
                """,
                (principal_hash, grant_id),
            )
            self._insert_audit(cursor, {
                "principal": principal_hash, "decision": "FREE_SQL_DISABLED",
                "affected_columns": [], "policy_version": grant_id,
            })
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def registered_query(self, query_id: str) -> str | None:
        rows = self._read(
            f"SELECT statement FROM {self._prefix}.query_registry WHERE query_id = ? AND enabled = true",
            (query_id,),
        )
        if len(rows) > 1:
            raise RuntimeError("The control plane contains duplicate registered query identifiers.")
        return str(rows[0][0]) if rows else None

    def query_parameter_schema(self, query_id: str) -> dict[str, Any]:
        rows = self._read(
            f"SELECT parameter_schema FROM {self._prefix}.query_registry WHERE query_id = ? AND enabled = true",
            (query_id,),
        )
        if len(rows) > 1:
            raise RuntimeError("The control plane contains duplicate registered query identifiers.")
        if not rows:
            return {}
        try:
            schema = json.loads(str(rows[0][0] or "{}"))
        except json.JSONDecodeError as exc:
            raise RuntimeError("The registered query has an invalid parameter schema.") from exc
        if not isinstance(schema, dict):
            raise RuntimeError("The registered query has an invalid parameter schema.")
        return schema

    def audit(self, event: dict[str, Any]) -> None:
        connection = self._connect()
        cursor = connection.cursor()
        try:
            self._insert_audit(cursor, event)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def _insert_audit(self, cursor: Any, event: dict[str, Any]) -> None:
        affected = event.get("affected_columns") or []
        if not isinstance(affected, list) or not all(isinstance(item, str) for item in affected):
            raise TypeError("affected_columns must contain only strings")
        cursor.execute(
            f"""
            INSERT INTO {self._prefix}.governance_audit
            SELECT ?, ?, ?, ?, from_json(?, 'array<string>'), ?, current_timestamp()
            """,
            (
                str(uuid.uuid4()),
                str(event.get("principal") or ""),
                str(event.get("decision") or ""),
                str(event.get("query_id") or ""),
                json.dumps(affected, separators=(",", ":")),
                str(event.get("policy_version") or "current"),
            ),
        )

    def _migrate_data_governance(self) -> None:
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute(f"DESCRIBE TABLE {self._prefix}.data_governance")
            existing = {str(row[0]).casefold() for row in cursor.fetchall() if row and row[0]}
            missing = [definition for name, definition in _CATALOG_COLUMNS if name.casefold() not in existing]
            if missing:
                cursor.execute(f"ALTER TABLE {self._prefix}.data_governance ADD COLUMNS ({', '.join(missing)})")
            connection.commit()
        finally:
            cursor.close()
            connection.close()

    def _migrate_lineage(self) -> None:
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute(f"DESCRIBE TABLE {self._prefix}.lineage_propagation")
            existing = {str(row[0]).casefold() for row in cursor.fetchall() if row and row[0]}
            missing = [definition for name, definition in _LINEAGE_COLUMNS if name.casefold() not in existing]
            if missing:
                cursor.execute(f"ALTER TABLE {self._prefix}.lineage_propagation ADD COLUMNS ({', '.join(missing)})")
            connection.commit()
        finally:
            cursor.close()
            connection.close()

    def _apply_lineage_snapshot(
        self, cursor: Any, snapshot: CatalogSnapshot,
        resolved: list[tuple[CatalogColumn, str, str]],
    ) -> int:
        names: dict[str, str] = {}
        ambiguous: set[str] = set()
        for column, object_id, _ in resolved:
            for name in (
                f"{column.catalog_name}.{column.schema_name}.{column.table_name}.{column.column_name}",
                f"{column.table_key}.{column.column_name}",
            ):
                key = name.casefold()
                if key in names and names[key] != object_id:
                    ambiguous.add(key)
                else:
                    names[key] = object_id
        for key in ambiguous:
            names.pop(key, None)

        applied = 0
        for edge in snapshot.lineage:
            source = names.get(edge.source_qualified_name.casefold())
            target = names.get(edge.target_qualified_name.casefold())
            if not source or not target or source == target:
                continue
            rule_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{snapshot.source}:{source}:{target}"))
            cursor.execute(
                f"""
                MERGE INTO {self._prefix}.lineage_propagation target
                USING (SELECT ? rule_id) source
                ON target.rule_id = source.rule_id
                WHEN MATCHED THEN UPDATE SET source_object_id = ?, target_object_id = ?, action = 'INHERIT',
                  priority = 0, enabled = true, updated_at = current_timestamp(), source_system = ?,
                  source_version = ?, deleted = false, updated_by = 'AIDP_MASTER_CATALOG'
                WHEN NOT MATCHED THEN INSERT (
                  rule_id, source_object_id, target_object_id, action, priority, enabled, updated_at,
                  source_system, source_version, deleted, updated_by
                ) VALUES (?, ?, ?, 'INHERIT', 0, true, current_timestamp(), ?, ?, false, 'AIDP_MASTER_CATALOG')
                """,
                (
                    rule_id, source, target, snapshot.source, snapshot.version,
                    rule_id, source, target, snapshot.source, snapshot.version,
                ),
            )
            applied += 1
        cursor.execute(
            f"""
            UPDATE {self._prefix}.lineage_propagation
            SET deleted = true, updated_at = current_timestamp()
            WHERE source_system = ? AND source_version <> ? AND coalesce(deleted, false) = false
            """,
            (snapshot.source, snapshot.version),
        )
        return applied

    def _existing_catalog_columns(self, cursor: Any, source: str) -> list["_ExistingColumn"]:
        cursor.execute(
            f"""
            SELECT object_id, source_table_fingerprint, source_table_key, column_name,
                   source_column_ordinal, source_column_type, deleted
            FROM {self._prefix}.data_governance
            WHERE source_system = ?
            """,
            (source,),
        )
        return [
            _ExistingColumn(
                object_id=str(row[0]),
                table_fingerprint=str(row[1] or ""),
                table_key=str(row[2] or ""),
                column_name=str(row[3] or ""),
                ordinal=int(row[4]) if row[4] is not None else -1,
                column_type=str(row[5] or ""),
                deleted=bool(row[6]),
            )
            for row in cursor.fetchall()
        ]

    def _propagated_policies(self, direct: list[tuple[str, ColumnPolicy]]) -> list[ColumnPolicy]:
        object_rows = self._read(
            f"""
            SELECT object_id, catalog_name, schema_name, table_name, column_name
            FROM {self._prefix}.data_governance
            WHERE deleted = false
            """
        )
        objects = {
            str(object_id): ColumnRef(str(catalog), str(schema), str(table), str(column))
            for object_id, catalog, schema, table, column in object_rows
        }
        edge_rows = self._read(
            f"""
            SELECT source_object_id, target_object_id, action, priority
            FROM {self._prefix}.lineage_propagation
            WHERE enabled = true AND coalesce(deleted, false) = false
            ORDER BY source_object_id, target_object_id, priority DESC
            """
        )
        edges: dict[str, list[tuple[str, int]]] = {}
        for source, target, action, priority in edge_rows:
            if str(action).upper() != "INHERIT":
                raise RuntimeError("The control plane contains an unsupported lineage action.")
            if str(source) in objects and str(target) in objects:
                edges.setdefault(str(source), []).append((str(target), int(priority)))

        inherited: list[ColumnPolicy] = []
        for source, policy in direct:
            best = {source: policy.priority}
            queue = [source]
            while queue:
                current = queue.pop(0)
                for target, rule_priority in edges.get(current, []):
                    priority = max(best[current], rule_priority)
                    if target in best and best[target] >= priority:
                        continue
                    best[target] = priority
                    queue.append(target)
            for target, priority in sorted(best.items()):
                if target == source:
                    continue
                column = objects[target]
                inherited.append(ColumnPolicy(
                    column.catalog, column.schema, column.table, column.column, policy.action,
                    policy.principal_type, policy.principal_name, priority, True,
                ))
        return inherited

    def _insert_sync_state(
        self, cursor: Any, source: str, snapshot_version: str, content_hash: str, status: str
    ) -> None:
        cursor.execute(
            f"""
            INSERT INTO {self._prefix}.sync_state
            VALUES (?, ?, ?, ?, current_timestamp())
            """,
            (source, snapshot_version, content_hash, status),
        )

    def _read(self, statement: str, parameters: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute(statement, parameters)
            return [tuple(row) for row in cursor.fetchall()]
        finally:
            cursor.close()
            connection.close()

    def _write(self, statement: str, parameters: tuple[Any, ...] = ()) -> None:
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute(statement, parameters)
            connection.commit()
        finally:
            cursor.close()
            connection.close()

    def _write_many(self, statements: Iterable[str]) -> None:
        connection = self._connect()
        cursor = connection.cursor()
        try:
            for statement in statements:
                cursor.execute(statement)
            connection.commit()
        finally:
            cursor.close()
            connection.close()


def _identifier(value: str) -> str:
    if not value or not value.replace("_", "a").isalnum():
        raise ValueError("The governance catalog must be a simple identifier.")
    return value


def _control_location(value: str) -> str:
    if not value:
        return ""
    parsed = urlparse(value.rstrip("/"))

    def valid_name(name: str) -> bool:
        return bool(name) and all(character.isalnum() or character in "._-" for character in name)

    safe = (
        parsed.scheme == "oci",
        valid_name(parsed.username or ""),
        not parsed.password,
        valid_name(parsed.hostname or ""),
        parsed.path.startswith("/") and parsed.path != "/",
        not parsed.params,
        not parsed.query,
        not parsed.fragment,
        not any(character in parsed.path for character in ("'", ";", "\n", "\r")),
    )
    if not all(safe):
        raise ValueError("The governance control location must be a safe OCI Object Storage URI.")
    return value.rstrip("/")


_CATALOG_COLUMNS = (
    ("classification", "classification STRING"),
    ("source_system", "source_system STRING"),
    ("source_catalog_key", "source_catalog_key STRING"),
    ("source_catalog_guid", "source_catalog_guid STRING"),
    ("source_schema_key", "source_schema_key STRING"),
    ("source_table_key", "source_table_key STRING"),
    ("source_table_fingerprint", "source_table_fingerprint STRING"),
    ("source_column_ordinal", "source_column_ordinal INT"),
    ("source_column_type", "source_column_type STRING"),
    ("source_column_description", "source_column_description STRING"),
    ("source_entity_type", "source_entity_type STRING"),
    ("source_created_at", "source_created_at STRING"),
    ("source_created_by", "source_created_by STRING"),
    ("identity_status", "identity_status STRING"),
    ("first_seen_at", "first_seen_at TIMESTAMP"),
)


_LINEAGE_COLUMNS = (
    ("source_system", "source_system STRING"),
    ("source_version", "source_version STRING"),
    ("deleted", "deleted BOOLEAN"),
    ("updated_by", "updated_by STRING"),
)


@dataclass(frozen=True)
class _ExistingColumn:
    object_id: str
    table_fingerprint: str
    table_key: str
    column_name: str
    ordinal: int
    column_type: str
    deleted: bool


def _resolve_object_ids(
    columns: tuple[CatalogColumn, ...], existing: list[_ExistingColumn]
) -> tuple[list[tuple[CatalogColumn, str, str]], int, int]:
    by_exact: dict[tuple[str, str], list[_ExistingColumn]] = {}
    by_table: dict[str, list[_ExistingColumn]] = {}
    for item in existing:
        by_exact.setdefault((item.table_fingerprint, item.column_name.casefold()), []).append(item)
        by_table.setdefault(item.table_fingerprint, []).append(item)

    resolved: list[tuple[CatalogColumn, str, str]] = []
    unmatched: dict[str, list[CatalogColumn]] = {}
    used_ids: set[str] = set()
    for column in columns:
        candidates = by_exact.get((column.table_fingerprint, column.column_name.casefold()), [])
        if len(candidates) > 1:
            raise RuntimeError("The control plane contains ambiguous column identities.")
        if candidates:
            resolved.append((column, candidates[0].object_id, "EXACT"))
            used_ids.add(candidates[0].object_id)
        else:
            unmatched.setdefault(column.table_fingerprint, []).append(column)

    new_count = 0
    renamed_count = 0
    for fingerprint, incoming in unmatched.items():
        old = [item for item in by_table.get(fingerprint, []) if item.object_id not in used_ids and not item.deleted]
        if len(incoming) == 1 and len(old) == 1 and _same_column_shape(incoming[0], old[0]):
            resolved.append((incoming[0], old[0].object_id, "INFERRED_RENAME"))
            used_ids.add(old[0].object_id)
            renamed_count += 1
            continue
        for column in incoming:
            resolved.append((column, new_object_id(column), "NEW"))
            new_count += 1
    return resolved, new_count, renamed_count


def _same_column_shape(column: CatalogColumn, existing: _ExistingColumn) -> bool:
    return column.column_ordinal == existing.ordinal and column.column_type.casefold() == existing.column_type.casefold()


def _merge_parameters(
    column: CatalogColumn, object_id: str, identity_status: str, snapshot: CatalogSnapshot
) -> tuple[Any, ...]:
    source_values = (
        column.catalog_name, column.schema_name, column.table_name, column.column_name,
        snapshot.version, snapshot.source, column.catalog_key, column.catalog_guid, column.schema_key,
        column.table_key, column.table_fingerprint, column.column_ordinal, column.column_type,
        column.column_description, column.entity_type, column.table_created_at, column.table_created_by,
        identity_status,
    )
    return (object_id, *source_values, object_id, *source_values[:4], *source_values[4:])


def _policy_action(value: Any) -> PolicyAction:
    try:
        return PolicyAction(str(value).upper())
    except ValueError as exc:
        raise RuntimeError("The control plane contains an unsupported policy action.") from exc


def _principal_matches(principal_type: str, principal_name: str, principal: Principal) -> bool:
    kind = principal_type.upper()
    return (
        (kind == "USER" and principal_name == principal.subject)
        or (kind == "GROUP" and principal_name in principal.groups)
        or (kind == "ROLE" and principal_name in principal.roles)
    )
