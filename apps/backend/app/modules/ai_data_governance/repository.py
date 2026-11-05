"""Bounded Delta writes for an externally serialized Governance workflow.

This repository does not acquire a lock. The caller must hold exclusive writer
admission across validation and commit; Delta MERGE does not enforce uniqueness.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from .delta import _verify_table, qualified_table, table_location
from .schema import load_model


def checked_rows(table_name: str, rows: list[dict], *, limit: int = 1000) -> list[dict]:
    """Reject the whole batch before constructing a DataFrame or changing Delta."""
    tables = {table["name"]: table for table in load_model()["tables"]}
    if table_name not in tables or type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("Unknown Governance destination or invalid write bound")
    if not isinstance(rows, list) or len(rows) > limit:
        raise ValueError("Governance write batch exceeds its bound")
    table = tables[table_name]
    names = {column["name"] for column in table["columns"]}
    key_sets = [table["pk"], *table["unique"]]
    result, seen = [], [set() for _ in key_sets]
    for row in rows:
        if not isinstance(row, dict) or set(row) != names:
            raise ValueError("Governance row does not match the approved columns")
        checked = {column["name"]: _value(column, row[column["name"]]) for column in table["columns"]}
        for key_columns, keys in zip(key_sets, seen):
            key = tuple(checked[name] for name in key_columns)
            if None not in key:
                if key in keys:
                    raise ValueError("Duplicate logical keys in Governance write batch")
                keys.add(key)
        result.append(checked)
    return result


def _value(column: dict, value: Any) -> Any:
    if value is None:
        if column["required"]:
            raise ValueError("A required Governance value is null")
        return None
    kind = column["type"]
    if kind == "STRING" and isinstance(value, str):
        return value
    if kind == "BOOLEAN" and type(value) is bool:
        return value
    if kind in {"INT", "BIGINT"} and type(value) is int:
        bits = 32 if kind == "INT" else 64
        if -(2 ** (bits - 1)) <= value < 2 ** (bits - 1):
            return value
    if kind == "TIMESTAMP" and isinstance(value, datetime) and value.utcoffset() is not None:
        return value.astimezone(timezone.utc)
    raise ValueError("Governance value has the wrong type or range")


def _spark_schema(table: dict):
    from pyspark.sql import types

    constructors = {"STRING": types.StringType, "INT": types.IntegerType,
                    "BIGINT": types.LongType, "BOOLEAN": types.BooleanType,
                    "TIMESTAMP": types.TimestampType}
    return types.StructType([
        types.StructField(column["name"], constructors[column["type"]](), not column["required"])
        for column in table["columns"]
    ])


class DeltaRepository:
    """One selected model; no DDL, schema evolution, deletes or implicit writer lock."""

    def __init__(self, spark: Any, storage_root: str, *, max_batch_rows: int = 1000, before_write=None):
        if type(max_batch_rows) is not int or not 1 <= max_batch_rows <= 1000:
            raise ValueError("Invalid Governance repository batch bound")
        if before_write is not None and not callable(before_write):
            raise ValueError("The write admission hook must be callable")
        self.before_write = before_write
        self.spark = spark
        self.max_batch_rows = max_batch_rows
        self.tables = {table["name"]: table for table in load_model()["tables"]}
        table_location(storage_root, "AI_DG_RUN")
        self.storage_root = storage_root

    def read_by_keys(self, table_name: str, keys: list[dict]) -> list[dict]:
        """Read only an admitted bounded PK set, never an arbitrary SQL filter."""
        table = self.tables.get(table_name)
        if table is None or not isinstance(keys, list) or len(keys) > self.max_batch_rows:
            raise ValueError("Invalid Governance lookup")
        if not keys:
            return []
        columns = [c for c in table["columns"] if c["name"] in table["pk"]]
        seen, checked = set(), []
        for key in keys:
            if not isinstance(key, dict) or set(key) != set(table["pk"]):
                raise ValueError("A complete Governance primary key is required")
            record = {c["name"]: _value(c, key[c["name"]]) for c in columns}
            identity = tuple(record[name] for name in table["pk"])
            if identity in seen:
                raise ValueError("Duplicate Governance lookup key")
            seen.add(identity)
            checked.append(record)
        self._verify_destination(table)
        source = self.spark.createDataFrame(checked, schema=_spark_schema({**table, "columns": columns}))
        matching = self.spark.table(qualified_table(table_name)).join(source, on=table["pk"], how="inner")
        rows = _comparison_frame(table, matching).limit(len(keys) + 1).collect()
        result = [_restore_timestamps(table, row.asDict(recursive=True)) for row in rows]
        checked_rows(table_name, result, limit=self.max_batch_rows)
        if any(tuple(row[name] for name in table["pk"]) not in seen for row in result):
            raise ValueError("Governance lookup returned an unrequested identity")
        return result

    def update_current(self, table_name: str, rows: list[dict], *, expected: list[dict]) -> int:
        """Advance admitted current controls under the caller's writer lock.

        This read-before-write precondition is not a database CAS. No immutable
        version/evidence/document rows are editable here.
        """
        table, fixed, checked, previous = self._prepare_current_update(table_name, rows, expected)
        if not checked:
            return 0
        self._verify_destination(table)
        source = self.spark.createDataFrame(checked, schema=_spark_schema(table))
        matching = self.spark.table(qualified_table(table_name)).join(
            source.select(*table["pk"]), on=table["pk"], how="inner")
        pending = self._pending_updates(table, matching, checked, previous)
        if not pending:
            return len(checked)
        if len(pending) != len(checked):
            source = self.spark.createDataFrame(pending, schema=_spark_schema(table))
        view = "ai_dg_batch_" + uuid4().hex
        source.createOrReplaceTempView(view)
        try:
            condition = " AND ".join(f"t.`{name}` = s.`{name}`" for name in table["pk"])
            changes = ", ".join(f"t.`{c['name']}` = s.`{c['name']}`" for c in table["columns"]
                                if c["name"] not in fixed)
            self._admit_write(table)
            self.spark.sql(f"MERGE INTO {qualified_table(table_name)} t USING `{view}` s ON {condition}"
                           + f" WHEN MATCHED THEN UPDATE SET {changes}").collect()
        finally:
            self.spark.catalog.dropTempView(view)
        confirmed = self.spark.table(qualified_table(table_name)).join(
            self.spark.createDataFrame(checked, schema=_spark_schema(table)).select(*table["pk"]),
            on=table["pk"], how="inner")
        self._check_matching(table, confirmed, checked, require_complete=True)
        return len(checked)

    def _prepare_current_update(self, table_name: str, rows: list[dict], expected: list[dict]) -> tuple:
        fixed = {
            "AI_DG_AGENT": {"agent_id", "platform_key", "workspace_key", "agent_role",
                            "native_agent_key", "code_path", "capabilities_json", "enabled"},
            "AI_DG_ASSET": {"asset_id", "platform_key", "asset_kind", "native_key",
                            "parent_asset_id", "first_seen_at"},
            "AI_DG_SCOPE_ASSET": {"scope_id", "asset_id"},
            "AI_DG_WORK": {"work_id", "scope_id", "asset_id", "field_path"},
            "AI_DG_RUN": {"run_id", "scope_id", "parent_run_id", "agent_id", "run_kind",
                          "execution_key", "attempt_no", "component_version", "native_run_ref", "started_at"},
            "AI_DG_PUBLICATION": {"publication_id", "document_id", "initiating_run_id", "expected_native_version"},
            "AI_DG_CASE": {"case_id", "scope_id", "opened_run_id", "created_at"},
        }
        if table_name not in fixed:
            raise ValueError("Destination is not a current inventory projection")
        checked = checked_rows(table_name, rows, limit=self.max_batch_rows)
        previous = checked_rows(table_name, expected, limit=self.max_batch_rows)
        table = self.tables[table_name]
        key = lambda record: tuple(record[name] for name in table["pk"])
        before = {key(record): record for record in previous}
        if {key(record) for record in checked} != set(before):
            raise ValueError("Inventory updates require the same expected key set")
        for record in checked:
            if any(record[name] != before[key(record)][name] for name in fixed[table_name]):
                raise ValueError("Inventory update changes immutable identity or provenance")
        return table, fixed[table_name], checked, previous

    @staticmethod
    def _pending_updates(table: dict, matching: Any, checked: list[dict], previous: list[dict]) -> list[dict]:
        key = lambda record: tuple(record[name] for name in table["pk"])
        before = {key(record): record for record in previous}
        observed = _comparison_frame(table, matching).limit(len(checked) + 1).collect()
        actual = {}
        desired = {key(record): record for record in checked}
        for row in observed:
            record = row.asDict(recursive=True)
            identity = key(record)
            if identity in actual or identity not in desired:
                raise ValueError("Inventory update has duplicate or unexpected destination keys")
            if record not in (_comparable(before[identity]), _comparable(desired[identity])):
                raise ValueError("Inventory snapshot has different content")
            actual[identity] = record
        if len(actual) != len(checked):
            raise ValueError("Inventory update could not be confirmed completely")
        return [record for record in checked if actual[key(record)] != _comparable(record)]

    def _verify_destination(self, table: dict) -> None:
        if self.spark.conf.get("spark.sql.session.timeZone") != "UTC":
            raise ValueError("Governance access requires a UTC Spark session")
        _verify_table(self.spark, table, self.storage_root)

    def insert_immutable(self, table_name: str, rows: list[dict]) -> int:
        """Insert missing rows only; an existing identity with different content fails.

        Separate table commits are not a transaction. The workflow's durable
        receipts and final reconciliation must establish cross-table visibility.
        """
        checked = checked_rows(table_name, rows, limit=self.max_batch_rows)
        if not checked:
            return 0
        table = self.tables[table_name]
        if self.spark.conf.get("spark.sql.session.timeZone") != "UTC":
            raise ValueError("Governance writes require a UTC Spark session")
        _verify_table(self.spark, table, self.storage_root)
        source = self.spark.createDataFrame(checked, schema=_spark_schema(table))
        destination = self.spark.table(qualified_table(table_name))
        for keys in [table["pk"], *table["unique"]]:
            matching = destination.join(source.select(*keys), on=keys, how="inner")
            self._check_matching(table, matching, checked, keys=keys)
        self._insert_frame(table, source)
        confirmed = self.spark.table(qualified_table(table_name)).join(
            source.select(*table["pk"]), on=table["pk"], how="inner")
        self._check_matching(table, confirmed, checked, require_complete=True)
        return len(checked)

    @staticmethod
    def _check_matching(table: dict, matching: Any, checked: list[dict], *,
                        keys: list[str] | None = None, require_complete: bool = False) -> None:
        keys = keys or table["pk"]
        existing = _comparison_frame(table, matching).limit(len(checked) + 1).collect()
        by_key = {}
        for row in existing:
            record = row.asDict(recursive=True)
            key = tuple(record[name] for name in keys)
            if key in by_key:
                raise ValueError("Governance destination contains duplicate logical keys")
            by_key[key] = record
        if len(existing) > len(checked):
            raise ValueError("Governance destination exceeds the admitted logical key set")
        if require_complete and len(existing) != len(checked):
            raise ValueError("Governance commit could not be confirmed completely")
        for record in checked:
            previous = by_key.get(tuple(record[name] for name in keys))
            if previous is not None and previous != _comparable(record):
                raise ValueError("An immutable Governance record already has different content")

    def _admit_write(self, table: dict) -> None:
        if self.before_write is not None:
            self.before_write(table["name"])

    def _insert_frame(self, table: dict, source: Any) -> None:
        view = "ai_dg_batch_" + uuid4().hex
        source.createOrReplaceTempView(view)
        try:
            names = [column["name"] for column in table["columns"]]
            condition = " AND ".join(f"t.`{name}` = s.`{name}`" for name in table["pk"])
            columns = ", ".join(f"`{name}`" for name in names)
            values = ", ".join(f"s.`{name}`" for name in names)
            self._admit_write(table)
            self.spark.sql(
                f"MERGE INTO {qualified_table(table['name'])} t USING `{view}` s ON {condition}"
                + f" WHEN NOT MATCHED THEN INSERT ({columns}) VALUES ({values})"
            ).collect()
        finally:
            self.spark.catalog.dropTempView(view)


def _comparison_frame(table: dict, frame: Any):
    from pyspark.sql import functions as functions

    # Convert in Spark before collect: Python's timestamp deserializer otherwise
    # uses the driver's local timezone, independently of the SQL session timezone.
    return frame.select(*[
        functions.expr(f"unix_micros(`{column['name']}`)").alias(column["name"])
        if column["type"] == "TIMESTAMP" else functions.col(column["name"])
        for column in table["columns"]
    ])


def _comparable(record: dict) -> dict:
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    result = dict(record)
    for name, value in record.items():
        if isinstance(value, datetime):
            delta = value - epoch
            result[name] = (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds
    return result


def _restore_timestamps(table: dict, record: dict) -> dict:
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    return {c["name"]: epoch + timedelta(microseconds=record[c["name"]])
            if c["type"] == "TIMESTAMP" and record[c["name"]] is not None else record[c["name"]]
            for c in table["columns"]}
