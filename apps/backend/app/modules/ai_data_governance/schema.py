"""Read the approved Delta model and preserve its descriptive/key contracts."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any


AIDP_ROOT = Path(__file__).resolve().parent / "aidp"
MODEL_ROOT = AIDP_ROOT / "master_catalog/oci_medallion/schemas/oci_artifacts"
NAMESPACE = "oci_medallion.oci_artifacts"
SQL_TYPES = frozenset({"STRING", "INT", "BIGINT", "TIMESTAMP", "BOOLEAN"})


def validate_model(model: dict[str, Any]) -> None:
    """Check logical integrity without claiming Delta enforces PK/FK/UNIQUE."""
    if model.get("namespace") != NAMESPACE or model.get("version") != 1:
        raise ValueError("Unsupported Governance model namespace or version")
    rows = model.get("tables")
    if not isinstance(rows, list) or len(rows) != 18:
        raise ValueError("The approved Governance model requires eighteen tables")
    tables = {row["name"]: row for row in rows}
    if len(tables) != len(rows):
        raise ValueError("Duplicate Governance table")
    for table in tables.values():
        _validate_table(table)
        for foreign in table["foreign_keys"]:
            _validate_foreign_key(table, foreign, tables)


def _validate_table(table: dict[str, Any]) -> None:
    if not re.fullmatch(r"AI_DG_[A-Z0-9_]+", table["name"]) or not table["grain"].strip():
        raise ValueError("Invalid table identity or missing table description")
    columns = {column["name"]: column for column in table["columns"]}
    if len(columns) != len(table["columns"]):
        raise ValueError("Duplicate Governance column")
    for column in columns.values():
        if not re.fullmatch(r"[a-z][a-z0-9_]*", column["name"]) or column["type"] not in SQL_TYPES:
            raise ValueError("Invalid column name or SQL type")
        if type(column["required"]) is not bool or not column["comment"].strip():
            raise ValueError("Missing column nullability or description")
    for key in [table["pk"], *table["unique"]]:
        if not key or len(set(key)) != len(key) or not set(key).issubset(columns):
            raise ValueError("Invalid logical key")
    if not table["pk"] or not all(columns[name]["required"] for name in table["pk"]):
        raise ValueError("Primary-key contracts must use required columns")


def _validate_foreign_key(table: dict, foreign: dict, tables: dict) -> None:
    parent = tables[foreign["table"]]
    if foreign["target"] not in [parent["pk"], *parent["unique"]]:
        raise ValueError("Foreign key must refer to a complete candidate key")
    if not foreign["columns"] or len(foreign["columns"]) != len(foreign["target"]):
        raise ValueError("Invalid foreign-key arity")
    source = {column["name"]: column for column in table["columns"]}
    target = {column["name"]: column for column in parent["columns"]}
    for name, reference in zip(foreign["columns"], foreign["target"]):
        if source[name]["type"] != target[reference]["type"]:
            raise ValueError("Incompatible foreign-key types")
        if source[name]["required"] == foreign.get("optional", False):
            raise ValueError("Foreign-key nullability disagrees with its contract")


def load_model() -> dict[str, Any]:
    manifest = json.loads((AIDP_ROOT / "manifest.json").read_text(encoding="utf-8"))
    metadata = manifest["model"].get("metadata")
    canonical_metadata = (MODEL_ROOT / "schema.json").relative_to(AIDP_ROOT.parent).as_posix()
    if metadata != canonical_metadata:
        raise ValueError("Governance model metadata must use its canonical Master Catalog path")
    model = json.loads((AIDP_ROOT.parent / metadata).read_text(encoding="utf-8"))
    model["tables"] = [json.loads((AIDP_ROOT.parent / path).read_text(encoding="utf-8"))
                       for path in manifest["model"]["tables"]]
    validate_model(model)
    return model


def model_digest() -> str:
    return hashlib.sha256(json.dumps(load_model(), sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def descriptions() -> dict[str, dict[str, str]]:
    """Return documentation for all tables/columns; never update a live catalog."""
    return {
        table["name"]: {"table": table["grain"], **{
            column["name"]: column["comment"] for column in table["columns"]
        }}
        for table in load_model()["tables"]
    }
