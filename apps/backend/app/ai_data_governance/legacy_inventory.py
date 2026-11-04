"""Exact column-identity migration; the retired rename heuristic is not reused."""

from __future__ import annotations

from .inventory import canonical


def legacy_column_ids(table_key: str, columns: tuple | list | None, rows: list[dict]) -> dict[str, str]:
    if len(rows) > 1000:
        raise ValueError("Legacy identity lookup exceeds its bound")
    result = {}
    used = set()
    for column in columns or ():
        key = column.get("native_key")
        matches = [row for row in rows if _legacy_matches(row, table_key, column)]
        if not matches:
            continue
        identifiers = {row.get("object_id") for row in matches}
        if len(matches) != 1 or len(identifiers) != 1:
            raise ValueError("Ambiguous legacy column identity")
        previous = next(iter(identifiers))
        if not isinstance(previous, str) or not previous or previous in used:
            raise ValueError("Invalid or reused legacy column identity")
        used.add(previous)
        native_key = key or canonical([table_key, column["name"]])
        result[canonical(["COLUMN", native_key])] = previous
    return result


def _legacy_matches(row: dict, table_key: str, column: dict) -> bool:
    if row.get("is_deleted") != 0 or row.get("table_key") != table_key:
        return False
    native_key = column.get("native_key")
    if native_key and row.get("column_key") == native_key:
        return True
    return row.get("column_name") == column["name"] and (not native_key or not row.get("column_key"))
