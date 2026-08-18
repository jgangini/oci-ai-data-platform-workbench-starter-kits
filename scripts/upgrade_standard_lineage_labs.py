"""Upgrade the four original lab packs to the participant-catalog lineage contract."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LABS_ROOT = ROOT / "apps" / "backend" / "app" / "labs"
LAB_IDS = ("banking", "telecommunications", "retail", "healthcare")

ENTITY_EDGES = {
    "banking": [
        "landing.customers->bronze.customers->silver.customers->gold.banking_customer_value",
        "landing.accounts->bronze.accounts->silver.accounts->gold.banking_customer_value",
        "landing.transactions->bronze.transactions->silver.transactions->gold.banking_branch_daily",
    ],
    "telecommunications": [
        "landing.subscribers->bronze.subscribers->silver.subscribers->gold.telecommunications_subscriber_monthly",
        "landing.usage_events->bronze.usage_events->silver.usage_events->gold.telecommunications_subscriber_monthly",
        "landing.network_sites->bronze.network_sites->silver.network_sites->gold.telecommunications_site_daily",
    ],
    "retail": [
        "landing.customers->bronze.customers->silver.customers->gold.retail_customer_value",
        "landing.orders->bronze.orders->silver.orders->gold.retail_customer_value",
        "landing.order_items->bronze.order_items->silver.order_items->gold.retail_product_daily",
        "landing.products->bronze.products->silver.products->gold.retail_product_daily",
    ],
    "healthcare": [
        "landing.patients->bronze.patients->silver.patients->gold.healthcare_patient_utilization",
        "landing.appointments->bronze.appointments->silver.appointments->gold.healthcare_provider_daily",
        "landing.encounters->bronze.encounters->silver.encounters->gold.healthcare_patient_utilization",
        "landing.providers->bronze.providers->silver.providers->gold.healthcare_provider_daily",
    ],
}

COLUMN_EDGES = {
    "banking": [
        "landing.customers.customer_id->bronze.customers.customer_id->silver.customers.customer_id->gold.banking_customer_value.customer_id",
        "landing.transactions.amount->bronze.transactions.amount->silver.transactions.amount->gold.banking_branch_daily.transaction_amount",
    ],
    "telecommunications": [
        "landing.subscribers.subscriber_id->bronze.subscribers.subscriber_id->silver.subscribers.subscriber_id->gold.telecommunications_subscriber_monthly.subscriber_id",
        "landing.usage_events.usage_value->bronze.usage_events.usage_value->silver.usage_events.usage_value->gold.telecommunications_site_daily.data_mb",
    ],
    "retail": [
        "landing.customers.customer_id->bronze.customers.customer_id->silver.customers.customer_id->gold.retail_customer_value.customer_id",
        "landing.order_items.quantity->bronze.order_items.quantity->silver.order_items.quantity->gold.retail_product_daily.units",
    ],
    "healthcare": [
        "landing.patients.patient_id->bronze.patients.patient_id->silver.patients.patient_id->gold.healthcare_patient_utilization.patient_id",
        "landing.encounters.cost_amount->bronze.encounters.cost_amount->silver.encounters.cost_amount->gold.healthcare_provider_daily.total_cost",
    ],
}


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _common(code: str) -> str:
    code = code.replace('_aidp_lab', '_aidp')
    if 'catalog_name = required_parameter("catalog_name")' not in code:
        code = code.replace(
            'objectstorage_namespace = required_parameter("objectstorage_namespace")\n',
            'objectstorage_namespace = required_parameter("objectstorage_namespace")\n'
            'catalog_name = required_parameter("catalog_name")\n',
        )
    marker = 'if not workspace_root.startswith("/Workspace/medallon/"):\n    raise ValueError("Invalid workspace_root")\n'
    addition = marker + '''if catalog_name != f"{participant_key}_aidp":
    raise ValueError("Invalid participant catalog")
spark.conf.set("spark.aidp.lineage.enabled", "true")

def table(layer, logical_name):
    prefix = f"{lab_id}_"
    physical_name = logical_name if logical_name.startswith(prefix) else prefix + logical_name
    return f"{catalog_name}.oci_{layer}.{participant_key}_{physical_name}"
'''
    if 'def table(layer, logical_name):' not in code:
        code = code.replace(marker, addition)
    return code


def _without_external_ddl(code: str) -> str:
    return re.sub(
        r'^spark\.sql\(f"""CREATE EXTERNAL TABLE.*?\)\s*$',
        "",
        code,
        flags=re.MULTILINE,
    )


def _upgrade_task(code: str, position: int) -> str:
    code = _without_external_ddl(_common(code))
    if position == 1:
        code = code.replace(
            'frame.write.mode("overwrite").option("header", True).csv(destinations[dataset])',
            '(frame.write.format("delta").mode("overwrite")\n'
            '        .option("overwriteSchema", "true").saveAsTable(table("landing", dataset)))',
        ).replace(
            'landing_count = spark.read.option("header", True).csv(destinations[dataset]).count()',
            'landing_count = spark.table(table("landing", dataset)).count()',
        )
    elif position == 2:
        code = re.sub(
            r'spark\.table\(f"(?:aidp_lab|\{catalog_name\})\.oci_landing\.\{landing_tables\[dataset\]\}"\)',
            'spark.table(table("landing", dataset))',
            code,
        ).replace(
            'frame.write.format("delta").mode("overwrite").option("overwriteSchema", "true").save(destinations[dataset])',
            'frame.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(table("bronze", dataset))',
        ).replace(
            'bronze_count = spark.read.format("delta").load(destinations[dataset]).count()',
            'bronze_count = spark.table(table("bronze", dataset)).count()',
        )
    elif position == 3:
        code = code.replace(
            'frame = spark.read.format("delta").load(sources[dataset])',
            'frame = spark.table(table("bronze", dataset))',
        ).replace(
            'clean.write.format("delta").mode("overwrite").option("overwriteSchema", "true").save(destinations[dataset])',
            'clean.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(table("silver", dataset))',
        ).replace(
            'quality.write.format("delta").mode("overwrite").option("overwriteSchema", "true").save(quality_uri)',
            'quality.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(table("silver", "quality_issues"))',
        )
    elif position == 4:
        code = re.sub(
            r'spark\.read\.format\("delta"\)\.load\(silver\["([^"]+)"\]\)',
            r'spark.table(table("silver", "\1"))',
            code,
        )
        code = re.sub(
            r'(\w+)\.write\.format\("delta"\)\.mode\("overwrite"\)\.option\("overwriteSchema", "true"\)\.save\(gold\["([^"]+)"\]\)',
            r'\1.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(table("gold", "\2"))',
            code,
        )
        code = re.sub(
            r'for table_name, location in gold\.items\(\):\n    row_count = spark\.read\.format\("delta"\)\.load\(location\)\.count\(\)',
            'for table_name in gold:\n    row_count = spark.table(table("gold", table_name)).count()',
            code,
        )
    return re.sub(r"\n{3,}", "\n\n", code).rstrip() + "\n"


def _validation_code(template: str, lab_id: str, tables: dict[str, list[str]]) -> str:
    bootstrap = _common(template)
    bootstrap = bootstrap[: bootstrap.index("\nfrom pyspark.sql import functions as F", bootstrap.index("def table"))]
    return bootstrap.rstrip() + f'''\n\nexpected_tables = {tables!r}
for layer, logical_names in expected_tables.items():
    for logical_name in logical_names:
        target = table(layer, logical_name)
        assert spark.table(target).count() > 0, f"{{target}} must not be empty"
        details = spark.sql(f"DESCRIBE FORMATTED {{target}}")
        formatted = {{
            str(row["col_name"]).strip().lower(): str(row["data_type"]).strip().lower()
            for row in details.collect()
        }}
        assert formatted.get("provider") == "delta", f"{{target}} must use Delta"
        assert formatted.get("type") == "managed", f"{{target}} must be managed"

assert spark.conf.get("spark.aidp.lineage.enabled", "true").lower() == "true"
print("{lab_id} validated across governed Landing, Bronze, Silver and Gold tables")
'''


def _upgrade_lab(lab_id: str, *, check: bool = False) -> bool:
    root = LABS_ROOT / lab_id
    stale = False
    metadata = json.loads((root / "lab.json").read_text(encoding="utf-8"))
    metadata["pack_version"] = "2.0.0"
    metadata["formats"] = {layer: "DELTA" for layer in ("landing", "bronze", "silver", "gold")}
    metadata.setdefault("table_storage", {layer: "MANAGED" for layer in metadata["tables"]})
    metadata["tables"]["gold"] = [
        name for name in metadata["tables"]["gold"] if name != "lineage_demo"
    ]

    for position, item in enumerate(metadata["notebooks"], start=1):
        path = root / "notebooks" / item["file"]
        notebook = json.loads(path.read_text(encoding="utf-8"))
        code_cell = next(cell for cell in notebook["cells"] if cell["cell_type"] == "code")
        code = "".join(code_cell["source"]).replace("_aidp_lab", "_aidp")
        if position == len(metadata["notebooks"]):
            if "validated across governed Landing, Bronze, Silver and Gold tables" not in code:
                code = _validation_code(code, lab_id, metadata["tables"])
        else:
            code = _upgrade_task(code, position)
        code_cell["source"] = code.splitlines(keepends=True)
        for cell in notebook["cells"]:
            if cell["cell_type"] == "code":
                cell["execution_count"] = None
                cell["outputs"] = []
        if position == len(metadata["notebooks"]):
            markdown = next(cell for cell in notebook["cells"] if cell["cell_type"] == "markdown")
            markdown["source"] = [
                f"# Validate {metadata['display_name']} lineage\n",
                "\n",
                "Validate the real governed Landing → Bronze → Silver → Gold flow without creating synthetic lineage tables.\n",
            ]
        content = (json.dumps(notebook, ensure_ascii=False, indent=1) + "\n").encode("utf-8")
        stale = stale or path.read_bytes() != content
        if not check:
            path.write_bytes(content)
        item["sha256"] = _sha256(content)

    metadata["expected_results"]["lineage"] = {
        "target_tables": metadata["tables"]["gold"],
        "expected_entity_edges": ENTITY_EDGES[lab_id],
        "expected_column_edges": COLUMN_EDGES[lab_id],
        "qualified_node_template": "{participant_key}_aidp.oci_{layer}.{table_name}",
        "required_schema_paths": [
            "{participant_key}_aidp.oci_landing.",
            "{participant_key}_aidp.oci_bronze.",
            "{participant_key}_aidp.oci_silver.",
            "{participant_key}_aidp.oci_gold.",
        ],
        "forbidden_table_suffixes": ["lineage_demo"],
        "levels": ["ENTITY", "COLUMN"],
        "direction": "BOTH",
        "max_depth": 8,
        "should_include_edges": True,
    }
    metadata.pop("pack_sha256", None)
    unsigned = json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("utf-8")
    metadata["pack_sha256"] = _sha256(unsigned)
    manifest = (json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    manifest_path = root / "lab.json"
    stale = stale or manifest_path.read_bytes() != manifest
    if not check:
        manifest_path.write_bytes(manifest)
    return stale


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    stale = False
    for lab_id in LAB_IDS:
        stale = _upgrade_lab(lab_id, check=args.check) or stale
    if args.check and stale:
        raise SystemExit("standard lineage packages are stale")


if __name__ == "__main__":
    main()
