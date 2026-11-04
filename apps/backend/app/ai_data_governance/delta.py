"""Create the approved empty Delta model without replacing existing data."""

from __future__ import annotations

import inspect
import pprint
import re
from typing import Any

from .schema import NAMESPACE, load_model


def _literal(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def qualified_table(name: str) -> str:
    names = {table["name"] for table in load_model()["tables"]}
    if name not in names:
        raise ValueError("Unknown Governance table")
    return ".".join(f"`{part}`" for part in (*NAMESPACE.split("."), name))


def table_location(storage_root: str, name: str) -> str:
    """Use the explicitly selected artifacts bucket, never the catalog warehouse."""
    qualified_table(name)
    if not isinstance(storage_root, str) or not re.fullmatch(
        r"oci://[A-Za-z0-9][A-Za-z0-9_.-]{0,254}@[A-Za-z0-9]{1,128}/oci_artifacts", storage_root
    ):
        raise ValueError("An explicit artifacts storage root is required")
    return f"{storage_root}/{name.lower()}"


def create_statements(storage_root: str) -> tuple[str, ...]:
    """Use native NOT NULL/comments; PK, FK and UNIQUE remain writer contracts."""
    statements = []
    for table in load_model()["tables"]:
        columns = ",\n".join(
            f"  `{column['name']}` {column['type']}"
            + (" NOT NULL" if column["required"] else "")
            + f" COMMENT {_literal(column['comment'])}"
            for column in table["columns"]
        )
        statements.append(
            f"CREATE TABLE IF NOT EXISTS {qualified_table(table['name'])} (\n{columns}\n)"
            f" USING DELTA\nLOCATION {_literal(table_location(storage_root, table['name']))}"
            f"\nCOMMENT {_literal(table['grain'])}"
        )
    return tuple(statements)


def _verify_table(spark: Any, table: dict, storage_root: str) -> None:
    target = qualified_table(table["name"])
    actual = spark.table(target).schema.fields
    expected = table["columns"]
    aliases = {"integer": "INT", "long": "BIGINT"}
    signature = [
        (field.name, aliases.get(field.dataType.typeName(), field.dataType.typeName().upper()),
         not field.nullable)
        for field in actual
    ]
    if signature != [(c["name"], c["type"], c["required"]) for c in expected]:
        raise ValueError(f"Existing Governance schema differs: {table['name']}")
    # Delta DESCRIBE DETAIL takes an incompatible catalog route on AIDP; use
    # Spark's catalog-aware metadata command rather than resolving a storage path.
    rows = spark.sql(f"DESCRIBE TABLE EXTENDED {target}").collect()
    details = {}
    in_details = False
    for row in rows:
        key = str(row.col_name).strip()
        if key == "# Partition Information":
            raise ValueError(f"Unexpected Governance partition layout: {table['name']}")
        if key == "# Detailed Table Information":
            in_details = True
        elif in_details and key:
            if key in details:
                raise ValueError(f"Ambiguous Governance metadata: {table['name']}")
            details[key] = row.data_type
    if str(details.get("Provider", "")).casefold() != "delta":
        raise ValueError(f"Governance table is not Delta: {table['name']}")
    if details.get("Type") != "EXTERNAL" or details.get("Location") != table_location(storage_root, table["name"]):
        raise ValueError(f"Governance storage differs from the selected artifacts destination: {table['name']}")
    comments = {field.name: field.metadata.get("comment") for field in actual}
    if details.get("Comment") != table["grain"] or any(
        comments[column["name"]] != column["comment"] for column in expected
    ):
        raise ValueError(f"Governance documentation differs: {table['name']}")


def ensure_model(spark: Any, storage_root: str) -> dict[str, list[str]]:
    """Validate all existing destinations first; never evolve/drop/truncate them."""
    model = load_model()
    statements = dict(zip((t["name"] for t in model["tables"]), create_statements(storage_root)))
    # The separately selected artifacts namespace/storage must already be provisioned.
    spark.sql("DESCRIBE DATABASE EXTENDED `oci_medallion`.`oci_artifacts`").collect()
    existing = []
    missing = []
    for table in model["tables"]:
        if spark.catalog.tableExists(qualified_table(table["name"])):
            _verify_table(spark, table, storage_root)
            existing.append(table["name"])
        else:
            missing.append(table["name"])
    for table in model["tables"]:
        if table["name"] in missing:
            spark.sql(statements[table["name"]]).collect()
            # A concurrent creator can win IF NOT EXISTS; that is not compatibility.
            _verify_table(spark, table, storage_root)
    return {"created": missing, "verified_existing": existing}


def bootstrap_source(storage_root: str) -> bytes:
    """Assemble a readable standalone workspace script from this same implementation."""
    create_statements(storage_root)
    functions = (_literal, qualified_table, table_location, create_statements, _verify_table, ensure_model)
    header = (
        '"""Generated additive AI_DG bootstrap. Source: app/ai_data_governance/delta.py."""\n'
        "from __future__ import annotations\n\nimport json\nimport re\nfrom typing import Any\n\n"
        f"NAMESPACE = {NAMESPACE!r}\n\n"
        f"STORAGE_ROOT = {storage_root!r}\n\n"
        f"MODEL = {pprint.pformat(load_model(), sort_dicts=False, width=100)}\n\n"
        "def load_model():\n    return MODEL\n\n\n"
    )
    body = "\n\n".join(inspect.getsource(function) for function in functions)
    footer = (
        '\n\nif __name__ == "__main__":\n'
        "    from pyspark.sql import SparkSession\n"
        "    spark = SparkSession.builder.getOrCreate()\n"
        "    print(json.dumps(ensure_model(spark, STORAGE_ROOT), sort_keys=True))\n"
    )
    return (header + body + footer).encode("utf-8")
