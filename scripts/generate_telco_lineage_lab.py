"""Generate the deterministic Telco Customer 360 Lineage lab package."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
from collections import defaultdict
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LAB_ROOT = ROOT / "apps" / "backend" / "app" / "labs" / "telco_lineage"
SOURCE_ROOT = LAB_ROOT / "source"
NOTEBOOK_ROOT = LAB_ROOT / "notebooks"
LAB_ID = "telco_lineage"

LAYER_PREFIXES = {
    "landing": "01_landing",
    "bronze": "02_bronze",
    "silver": "03_silver",
    "gold": "04_gold",
}

DATASET_COLUMNS = {
    "crm_customers": [
        "source_row_id", "customer_id", "first_name", "last_name",
        "document_number", "segment", "status", "updated_at",
    ],
    "crm_addresses": [
        "source_row_id", "address_id", "customer_id", "address_type",
        "address_line", "city", "province", "region", "is_primary",
        "updated_at",
    ],
    "product_catalog": [
        "source_row_id", "product_id", "service_type", "product_name",
        "product_family", "monthly_fee", "status", "updated_at",
    ],
    "prepaid_lines": [
        "source_row_id", "line_id", "customer_id", "product_id", "msisdn",
        "activation_date", "status", "updated_at",
    ],
    "prepaid_recharges": [
        "source_row_id", "recharge_id", "line_id", "recharge_date", "channel",
        "amount", "updated_at",
    ],
    "postpaid_accounts": [
        "source_row_id", "account_id", "customer_id", "billing_cycle", "status",
        "updated_at",
    ],
    "postpaid_lines": [
        "source_row_id", "line_id", "account_id", "product_id", "msisdn",
        "activation_date", "status", "updated_at",
    ],
    "postpaid_invoices": [
        "source_row_id", "invoice_id", "account_id", "invoice_month", "amount",
        "status", "updated_at",
    ],
    "home_services": [
        "source_row_id", "service_id", "customer_id", "product_id",
        "service_number", "activation_date", "status", "updated_at",
    ],
    "home_installations": [
        "source_row_id", "installation_id", "service_id", "address_id",
        "technology", "installed_at", "updated_at",
    ],
}

EXPECTED_SOURCE_COUNTS = {
    "crm_customers": 500,
    "crm_addresses": 620,
    "product_catalog": 15,
    "prepaid_lines": 650,
    "prepaid_recharges": 3000,
    "postpaid_accounts": 280,
    "postpaid_lines": 400,
    "postpaid_invoices": 1500,
    "home_services": 220,
    "home_installations": 220,
}

EXPECTED_TABLE_COUNTS = {
    "customer_master": 493,
    "customer_addresses": 617,
    "product_catalog": 15,
    "prepaid_service": 645,
    "postpaid_service": 398,
    "home_service": 218,
    "service_ownership": 1261,
    "quality_issues": 30,
    "customer_360": 493,
    "customer_service_portfolio": 1261,
    "geographic_service_summary": 12,
}

TABLES = {
    "landing": list(DATASET_COLUMNS),
    "bronze": list(DATASET_COLUMNS),
    "silver": [
        "customer_master", "customer_addresses", "product_catalog",
        "prepaid_service", "postpaid_service", "home_service",
        "service_ownership", "quality_issues",
    ],
    "gold": [
        "customer_360", "customer_service_portfolio",
        "geographic_service_summary",
    ],
}

TASKS = [
    ("01_landing_telco_lineage", [], "Landing: canonical sources"),
    ("02_bronze_crm_products", ["01_landing_telco_lineage"], "Bronze: CRM and products"),
    ("03_bronze_prepaid", ["01_landing_telco_lineage"], "Bronze: prepaid"),
    ("04_bronze_postpaid", ["01_landing_telco_lineage"], "Bronze: postpaid"),
    ("05_bronze_home", ["01_landing_telco_lineage"], "Bronze: home"),
    ("06_silver_customer", ["02_bronze_crm_products"], "Silver: governed customer"),
    ("07_silver_prepaid", ["03_bronze_prepaid", "06_silver_customer"], "Silver: prepaid services"),
    ("08_silver_postpaid", ["04_bronze_postpaid", "06_silver_customer"], "Silver: postpaid services"),
    ("09_silver_home", ["05_bronze_home", "06_silver_customer"], "Silver: home services"),
    ("10_silver_ownership_quality", ["06_silver_customer", "07_silver_prepaid", "08_silver_postpaid", "09_silver_home"], "Silver: service ownership and quality"),
    ("11_gold_customer_360", ["10_silver_ownership_quality"], "Gold: Customer 360"),
    ("12_gold_service_portfolio", ["10_silver_ownership_quality"], "Gold: service portfolio"),
    ("13_gold_geographic_summary", ["11_gold_customer_360", "12_gold_service_portfolio"], "Gold: geographic summary"),
    ("14_validate_lineage", ["13_gold_geographic_summary"], "Validate results and Delta lineage"),
]


def _row(source_row_id: str, **values: object) -> dict[str, object]:
    return {"source_row_id": source_row_id, **values}


def _source_data() -> dict[str, list[dict[str, object]]]:
    first_names = ["Ana", "Bruno", "Carla", "Diego", "Elena", "Fabian", "Gloria", "Hugo"]
    last_names = ["Alba", "Bello", "Cruz", "Duarte", "Estevez", "Flores", "Guerrero", "Herrera"]
    regions = ["Norte", "Centro", "Sur", "Costa"]
    cities = [(region, f"Provincia {region} {number}", f"Ciudad {region} {number}") for region in regions for number in range(1, 4)]

    customers = []
    for number in range(1, 497):
        customers.append(_row(
            f"SRC-C-{number:04d}", customer_id=f"C{number:04d}",
            first_name=first_names[(number - 1) % len(first_names)],
            last_name=last_names[(number - 1) % len(last_names)],
            document_number=f"{10_000_000 + number:08d}" if number <= 493 else f"BAD{number}",
            segment=("consumer", "family", "business")[(number - 1) % 3],
            status="active", updated_at="2026-01-01T00:00:00Z",
        ))
    for offset, customer in enumerate(customers[:4], start=497):
        duplicate = dict(customer)
        duplicate["source_row_id"] = f"SRC-C-{offset:04d}"
        duplicate["updated_at"] = "2026-02-01T00:00:00Z"
        customers.append(duplicate)

    addresses = []
    for number in range(1, 618):
        region, province, city = cities[(number - 1) % len(cities)]
        addresses.append(_row(
            f"SRC-A-{number:04d}", address_id=f"A{number:04d}",
            customer_id=f"C{((number - 1) % 493) + 1:04d}",
            address_type=("home", "billing", "work")[(number - 1) % 3],
            address_line=f"Avenida Sintetica {number}", city=city, province=province,
            region=region, is_primary="true" if number <= 493 else "false",
            updated_at="2026-01-02T00:00:00Z",
        ))
    for number in range(618, 621):
        region, province, city = cities[(number - 1) % len(cities)]
        addresses.append(_row(
            f"SRC-A-{number:04d}", address_id=f"A{number:04d}", customer_id="C9999",
            address_type="home", address_line=f"Avenida Sintetica {number}",
            city=city, province=province, region=region, is_primary="false",
            updated_at="2026-01-02T00:00:00Z",
        ))

    products = []
    product_specs = {
        "PREPAID": (["Basico", "Social", "Datos", "Voz", "Total"], ["0.00"] * 5),
        "POSTPAID": (["Inicio", "Plus", "Familia", "Pro", "Max"], ["29.90", "39.90", "49.90", "69.90", "89.90"]),
        "HOME": (["Fibra 100", "Fibra 300", "Fibra 600", "Duo", "Premium"], ["39.90", "49.90", "59.90", "69.90", "89.90"]),
    }
    prefixes = {"PREPAID": "PRE", "POSTPAID": "POST", "HOME": "HOME"}
    row_number = 1
    for service_type, (names, fees) in product_specs.items():
        for number, (name, fee) in enumerate(zip(names, fees), start=1):
            products.append(_row(
                f"SRC-PROD-{row_number:03d}", product_id=f"{prefixes[service_type]}{number:02d}",
                service_type=service_type, product_name=name,
                product_family=f"{service_type.title()} Portfolio", monthly_fee=fee,
                status="active", updated_at="2026-01-03T00:00:00Z",
            ))
            row_number += 1

    prepaid_lines = []
    for number in range(1, 651):
        prepaid_lines.append(_row(
            f"SRC-PL-{number:04d}", line_id=f"PL{number:04d}",
            customer_id=f"C{((number - 1) % 493) + 1:04d}" if number <= 645 else "C9999",
            product_id=f"PRE{((number - 1) % 5) + 1:02d}",
            msisdn=f"700{number:06d}", activation_date=f"2025-{((number - 1) % 12) + 1:02d}-01",
            status="active" if number % 11 else "suspended", updated_at="2026-01-04T00:00:00Z",
        ))

    prepaid_recharges = []
    recharge_amounts = [Decimal("5.00"), Decimal("10.00"), Decimal("15.00"), Decimal("20.00"), Decimal("25.00")]
    for number in range(1, 3001):
        amount = recharge_amounts[(number - 1) % len(recharge_amounts)] if number <= 2997 else Decimal("-10.00")
        prepaid_recharges.append(_row(
            f"SRC-PR-{number:05d}", recharge_id=f"R{number:05d}",
            line_id=f"PL{((number - 1) % 645) + 1:04d}",
            recharge_date=f"2026-{((number - 1) % 6) + 1:02d}-{((number - 1) % 28) + 1:02d}",
            channel=("app", "store", "bank")[(number - 1) % 3], amount=f"{amount:.2f}",
            updated_at="2026-07-01T00:00:00Z",
        ))

    accounts = []
    for number in range(1, 281):
        accounts.append(_row(
            f"SRC-PA-{number:04d}", account_id=f"PA{number:04d}",
            customer_id=f"C{((number - 1) % 493) + 1:04d}" if number <= 278 else "C9999",
            billing_cycle=str(((number - 1) % 28) + 1), status="active",
            updated_at="2026-01-05T00:00:00Z",
        ))

    postpaid_lines = []
    for number in range(1, 401):
        postpaid_lines.append(_row(
            f"SRC-POL-{number:04d}", line_id=f"POL{number:04d}",
            account_id=f"PA{((number - 1) % 278) + 1:04d}" if number <= 398 else "PA9999",
            product_id=f"POST{((number - 1) % 5) + 1:02d}",
            msisdn=f"800{number:06d}", activation_date=f"2024-{((number - 1) % 12) + 1:02d}-15",
            status="active" if number % 17 else "suspended", updated_at="2026-01-06T00:00:00Z",
        ))

    invoices = []
    invoice_amounts = [Decimal("45.90"), Decimal("59.90"), Decimal("79.90"), Decimal("99.90")]
    for number in range(1, 1501):
        amount = invoice_amounts[(number - 1) % len(invoice_amounts)] if number <= 1497 else Decimal("-20.00")
        invoices.append(_row(
            f"SRC-INV-{number:05d}", invoice_id=f"INV{number:05d}",
            account_id=f"PA{((number - 1) % 278) + 1:04d}",
            invoice_month=f"2026-{((number - 1) % 6) + 1:02d}-01", amount=f"{amount:.2f}",
            status="paid", updated_at="2026-07-02T00:00:00Z",
        ))

    home_services = []
    for number in range(1, 221):
        home_services.append(_row(
            f"SRC-HS-{number:04d}", service_id=f"HS{number:04d}",
            customer_id=f"C{((number - 1) % 493) + 1:04d}" if number <= 218 else "C9999",
            product_id=f"HOME{((number - 1) % 5) + 1:02d}", service_number=f"HOG{number:06d}",
            activation_date=f"2023-{((number - 1) % 12) + 1:02d}-10", status="active",
            updated_at="2026-01-07T00:00:00Z",
        ))

    installations = []
    for number in range(1, 218):
        installations.append(_row(
            f"SRC-HI-{number:04d}", installation_id=f"HI{number:04d}",
            service_id=f"HS{number:04d}", address_id=f"A{((number - 1) % 617) + 1:04d}",
            technology=("fiber", "fixed-wireless")[(number - 1) % 2], installed_at="2025-01-15",
            updated_at="2026-01-08T00:00:00Z",
        ))
    installations.extend([
        _row("SRC-HI-0218", installation_id="HI0218", service_id="HS0218", address_id="A9999", technology="fiber", installed_at="2025-01-15", updated_at="2026-01-08T00:00:00Z"),
        _row("SRC-HI-0219", installation_id="HI0219", service_id="HS9999", address_id="A0001", technology="fiber", installed_at="2025-01-15", updated_at="2026-01-08T00:00:00Z"),
        _row("SRC-HI-0220", installation_id="HI0001", service_id="HS0001", address_id="A0001", technology="fiber", installed_at="2025-01-15", updated_at="2026-02-08T00:00:00Z"),
    ])

    return {
        "crm_customers": customers,
        "crm_addresses": addresses,
        "product_catalog": products,
        "prepaid_lines": prepaid_lines,
        "prepaid_recharges": prepaid_recharges,
        "postpaid_accounts": accounts,
        "postpaid_lines": postpaid_lines,
        "postpaid_invoices": invoices,
        "home_services": home_services,
        "home_installations": installations,
    }


def _csv_bytes(columns: list[str], rows: list[dict[str, object]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def _validate_source_contract(data: dict[str, list[dict[str, object]]]) -> dict[str, int]:
    """Prove the generated anomalies and downstream row counts without Spark."""
    issues: dict[str, int] = {}

    customer_groups: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in data["crm_customers"]:
        customer_groups[str(row["customer_id"])].append(row)
    selected_customers = [
        max(rows, key=lambda row: (str(row["updated_at"]), str(row["source_row_id"])))
        for rows in customer_groups.values()
    ]
    issues["crm_customers"] = sum(len(rows) - 1 for rows in customer_groups.values()) + sum(
        re.fullmatch(r"[0-9]{8}", str(row["document_number"])) is None
        for row in selected_customers
    )
    valid_customers = {
        str(row["customer_id"])
        for row in selected_customers
        if re.fullmatch(r"[0-9]{8}", str(row["document_number"]))
    }

    valid_addresses = {
        str(row["address_id"])
        for row in data["crm_addresses"]
        if str(row["customer_id"]) in valid_customers
    }
    issues["crm_addresses"] = len(data["crm_addresses"]) - len(valid_addresses)
    products = {str(row["product_id"]): str(row["service_type"]) for row in data["product_catalog"]}
    issues["product_catalog"] = 0

    valid_prepaid = {
        str(row["line_id"])
        for row in data["prepaid_lines"]
        if str(row["customer_id"]) in valid_customers
        and products.get(str(row["product_id"])) == "PREPAID"
    }
    issues["prepaid_lines"] = len(data["prepaid_lines"]) - len(valid_prepaid)
    issues["prepaid_recharges"] = sum(
        str(row["line_id"]) not in valid_prepaid or Decimal(str(row["amount"])) <= 0
        for row in data["prepaid_recharges"]
    )

    valid_accounts = {
        str(row["account_id"])
        for row in data["postpaid_accounts"]
        if str(row["customer_id"]) in valid_customers
    }
    issues["postpaid_accounts"] = len(data["postpaid_accounts"]) - len(valid_accounts)
    valid_postpaid = {
        str(row["line_id"])
        for row in data["postpaid_lines"]
        if str(row["account_id"]) in valid_accounts
        and products.get(str(row["product_id"])) == "POSTPAID"
    }
    issues["postpaid_lines"] = len(data["postpaid_lines"]) - len(valid_postpaid)
    issues["postpaid_invoices"] = sum(
        str(row["account_id"]) not in valid_accounts or Decimal(str(row["amount"])) <= 0
        for row in data["postpaid_invoices"]
    )

    valid_home = {
        str(row["service_id"])
        for row in data["home_services"]
        if str(row["customer_id"]) in valid_customers
        and products.get(str(row["product_id"])) == "HOME"
    }
    issues["home_services"] = len(data["home_services"]) - len(valid_home)
    installation_groups: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in data["home_installations"]:
        installation_groups[str(row["installation_id"])].append(row)
    selected_installations = [
        max(rows, key=lambda row: (str(row["updated_at"]), str(row["source_row_id"])))
        for rows in installation_groups.values()
    ]
    issues["home_installations"] = sum(len(rows) - 1 for rows in installation_groups.values()) + sum(
        str(row["service_id"]) not in valid_home or str(row["address_id"]) not in valid_addresses
        for row in selected_installations
    )

    assert issues == {
        "crm_customers": 7, "crm_addresses": 3, "product_catalog": 0,
        "prepaid_lines": 5, "prepaid_recharges": 3,
        "postpaid_accounts": 2, "postpaid_lines": 2, "postpaid_invoices": 3,
        "home_services": 2, "home_installations": 3,
    }
    assert len(valid_customers) == 493 and len(valid_addresses) == 617
    assert len(valid_prepaid) == 645 and len(valid_postpaid) == 398 and len(valid_home) == 218
    assert len(valid_prepaid | valid_postpaid | valid_home) == 1261
    assert sum(issues.values()) == 30
    return issues


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _bootstrap_code() -> str:
    return '''import re
from functools import reduce
from pyspark.sql import Window, functions as F

# oidlUtils is injected by AIDP Workbench; no import is required.
def required_parameter(name):
    value = oidlUtils.parameters.getParameter(name, "")
    if value is None or not str(value).strip():
        raise ValueError(f"Missing AIDP job parameter: {name}")
    return str(value).strip()

participant_key = required_parameter("participant_key")
lab_id = required_parameter("lab_id")
workspace_root = required_parameter("workspace_root")
bucket_name = required_parameter("bucket_name")
objectstorage_namespace = required_parameter("objectstorage_namespace")

participant_match = re.fullmatch(r"u([1-9][0-9]*)", participant_key)
if participant_match is None or int(participant_match.group(1)) < 101:
    raise ValueError("Invalid participant_key")
if lab_id != "telco_lineage":
    raise ValueError("This notebook belongs to a different lab")
if not workspace_root.startswith("/Workspace/medallon/"):
    raise ValueError("Invalid workspace_root")

layer_prefixes = {"landing": "01_landing", "bronze": "02_bronze", "silver": "03_silver", "gold": "04_gold"}

def table(layer, logical_name):
    return f"aidp_lab.oci_{layer}.{participant_key}_{lab_id}_{logical_name}"

def location(layer, logical_name):
    return f"oci://{bucket_name}@{objectstorage_namespace}/{layer_prefixes[layer]}/users/{participant_key}/{lab_id}/{logical_name}/"

def write_delta(frame, layer, logical_name, _ddl):
    target = table(layer, logical_name)
    (frame.write.format("delta").mode("overwrite")
        .option("overwriteSchema", "true")
        .saveAsTable(target))
    actual = spark.table(target).count()
    assert actual == frame.count(), f"Delta count mismatch for {logical_name}"
    print(f"Delta {layer}.{logical_name}: {actual} rows")
'''


def _bronze_code(datasets: list[str]) -> str:
    return f'''dataset_columns = {DATASET_COLUMNS!r}
datasets = {datasets!r}
for dataset in datasets:
    source = spark.table(table("landing", dataset))
    bronze = (source
        .withColumn("_source_file", F.input_file_name())
        .withColumn("_ingested_at", F.current_timestamp()))
    target = table("bronze", dataset)
    (bronze.write.format("delta").mode("overwrite")
        .option("overwriteSchema", "true")
        .saveAsTable(target))
    assert spark.table(target).count() == source.count()
    print(f"Delta bronze.{{dataset}}: {{source.count()}} rows")
'''


def _task_code() -> dict[str, str]:
    landing = f'''from pathlib import Path
from pyspark.sql.types import StringType, StructField, StructType

dataset_columns = {DATASET_COLUMNS!r}
expected_counts = {EXPECTED_SOURCE_COUNTS!r}
source_root = Path(workspace_root) / "source"
assert {{path.name for path in source_root.glob("*.csv")}} == {{f"{{name}}.csv" for name in dataset_columns}}

for dataset, columns in dataset_columns.items():
    schema = StructType([StructField(name, StringType(), True) for name in columns])
    source = (spark.read.option("header", True).schema(schema)
        .csv(str(source_root / f"{{dataset}}.csv"))
        .withColumn("participant_key", F.lit(participant_key))
        .select("participant_key", *columns))
    assert source.count() == expected_counts[dataset]
    target = table("landing", dataset)
    (source.write.format("csv").mode("overwrite")
        .option("header", "true").saveAsTable(target))
    assert spark.table(table("landing", dataset)).count() == expected_counts[dataset]
    print(f"CSV landing.{{dataset}}: {{expected_counts[dataset]}} rows")
'''

    customer = '''customers = spark.table(table("bronze", "crm_customers"))
customer_window = Window.partitionBy("customer_id").orderBy(F.col("updated_at").desc(), F.col("source_row_id").desc())
ranked_customers = customers.withColumn("_rank", F.row_number().over(customer_window))
customer_reason = (F.when(F.col("_rank") > 1, F.lit("duplicate_customer"))
    .when(~F.col("document_number").rlike("^[0-9]{8}$"), F.lit("invalid_document")))
ranked_customers = ranked_customers.withColumn("_reason", customer_reason)
customer_master = (ranked_customers.filter(F.col("_reason").isNull())
    .select("participant_key", "customer_id",
        F.concat_ws(" ", "first_name", "last_name").alias("full_name"),
        "document_number", F.lower("segment").alias("segment"),
        F.lower("status").alias("status"), F.to_timestamp("updated_at").alias("updated_at")))

customer_issues = (ranked_customers.filter(F.col("_reason").isNotNull())
    .select(F.lit(participant_key).alias("participant_key"), F.lit("crm_customers").alias("dataset"),
        "source_row_id", F.col("customer_id").alias("record_key"),
        F.col("_reason").alias("reason_code"), F.current_timestamp().alias("quarantined_at")))

addresses = spark.table(table("bronze", "crm_addresses"))
address_check = addresses.join(customer_master.select("customer_id").withColumn("_customer_ok", F.lit(True)), "customer_id", "left")
address_issues = (address_check.filter(F.col("_customer_ok").isNull())
    .select(F.lit(participant_key).alias("participant_key"), F.lit("crm_addresses").alias("dataset"),
        "source_row_id", F.col("address_id").alias("record_key"),
        F.lit("orphan_customer").alias("reason_code"), F.current_timestamp().alias("quarantined_at")))
customer_addresses = (address_check.filter(F.col("_customer_ok").isNotNull())
    .select("participant_key", "address_id", "customer_id", F.lower("address_type").alias("address_type"),
        "address_line", "city", "province", "region",
        F.col("is_primary").cast("boolean").alias("is_primary"),
        F.to_timestamp("updated_at").alias("updated_at")))

products = spark.table(table("bronze", "product_catalog"))
product_catalog = products.select(
    "participant_key", "product_id", F.upper("service_type").alias("service_type"),
    "product_name", "product_family", F.col("monthly_fee").cast("decimal(12,2)").alias("monthly_fee"),
    F.lower("status").alias("status"), F.to_timestamp("updated_at").alias("updated_at"))

write_delta(customer_master, "silver", "customer_master", "participant_key STRING, customer_id STRING, full_name STRING, document_number STRING, segment STRING, status STRING, updated_at TIMESTAMP")
write_delta(customer_addresses, "silver", "customer_addresses", "participant_key STRING, address_id STRING, customer_id STRING, address_type STRING, address_line STRING, city STRING, province STRING, region STRING, is_primary BOOLEAN, updated_at TIMESTAMP")
write_delta(product_catalog, "silver", "product_catalog", "participant_key STRING, product_id STRING, service_type STRING, product_name STRING, product_family STRING, monthly_fee DECIMAL(12,2), status STRING, updated_at TIMESTAMP")
customer_issues.unionByName(address_issues).write.format("delta").mode("overwrite").save(location("silver", "_quality/customer"))
assert customer_master.count() == 493 and customer_addresses.count() == 617 and product_catalog.count() == 15
'''

    prepaid = '''customers = spark.table(table("silver", "customer_master")).select("customer_id").withColumn("_customer_ok", F.lit(True))
products = (spark.table(table("silver", "product_catalog")).filter(F.col("service_type") == "PREPAID")
    .select("product_id", "product_name", "product_family").withColumn("_product_ok", F.lit(True)))
lines = spark.table(table("bronze", "prepaid_lines"))
checked_lines = lines.join(customers, "customer_id", "left").join(products, "product_id", "left")
line_reason = (F.when(F.col("_customer_ok").isNull(), F.lit("orphan_customer"))
    .when(F.col("_product_ok").isNull(), F.lit("invalid_product")))
checked_lines = checked_lines.withColumn("_reason", line_reason)
line_issues = (checked_lines.filter(F.col("_reason").isNotNull())
    .select(F.lit(participant_key).alias("participant_key"), F.lit("prepaid_lines").alias("dataset"),
        "source_row_id", F.col("line_id").alias("record_key"), F.col("_reason").alias("reason_code"),
        F.current_timestamp().alias("quarantined_at")))
valid_lines = checked_lines.filter(F.col("_reason").isNull())

recharges = (spark.table(table("bronze", "prepaid_recharges"))
    .withColumn("amount_value", F.col("amount").cast("decimal(12,2)")))
checked_recharges = recharges.join(valid_lines.select("line_id").withColumn("_line_ok", F.lit(True)), "line_id", "left")
recharge_reason = (F.when(F.col("_line_ok").isNull(), F.lit("orphan_line"))
    .when(F.col("amount_value") <= 0, F.lit("negative_amount")))
checked_recharges = checked_recharges.withColumn("_reason", recharge_reason)
recharge_issues = (checked_recharges.filter(F.col("_reason").isNotNull())
    .select(F.lit(participant_key).alias("participant_key"), F.lit("prepaid_recharges").alias("dataset"),
        "source_row_id", F.col("recharge_id").alias("record_key"), F.col("_reason").alias("reason_code"),
        F.current_timestamp().alias("quarantined_at")))
recharge_value = (checked_recharges.filter(F.col("_reason").isNull()).groupBy("line_id")
    .agg(F.sum("amount_value").alias("monthly_value")))
prepaid_service = (valid_lines.join(recharge_value, "line_id", "left")
    .select("participant_key", F.col("line_id").alias("service_id"), F.col("msisdn").alias("service_number"),
        F.lit("PREPAID").alias("service_type"), "customer_id", "product_id",
        F.lower("status").alias("status"), F.coalesce("monthly_value", F.lit(0)).cast("decimal(14,2)").alias("monthly_value")))
write_delta(prepaid_service, "silver", "prepaid_service", "participant_key STRING, service_id STRING, service_number STRING, service_type STRING, customer_id STRING, product_id STRING, status STRING, monthly_value DECIMAL(14,2)")
line_issues.unionByName(recharge_issues).write.format("delta").mode("overwrite").save(location("silver", "_quality/prepaid"))
assert prepaid_service.count() == 645
'''

    postpaid = '''customers = spark.table(table("silver", "customer_master")).select("customer_id").withColumn("_customer_ok", F.lit(True))
products = (spark.table(table("silver", "product_catalog")).filter(F.col("service_type") == "POSTPAID")
    .select("product_id").withColumn("_product_ok", F.lit(True)))
accounts = spark.table(table("bronze", "postpaid_accounts")).join(customers, "customer_id", "left")
accounts = accounts.withColumn("_reason", F.when(F.col("_customer_ok").isNull(), F.lit("orphan_customer")))
account_issues = (accounts.filter(F.col("_reason").isNotNull())
    .select(F.lit(participant_key).alias("participant_key"), F.lit("postpaid_accounts").alias("dataset"),
        "source_row_id", F.col("account_id").alias("record_key"), F.col("_reason").alias("reason_code"),
        F.current_timestamp().alias("quarantined_at")))
valid_accounts = accounts.filter(F.col("_reason").isNull()).select("account_id", "customer_id")

lines = spark.table(table("bronze", "postpaid_lines"))
checked_lines = (lines.join(valid_accounts.withColumn("_account_ok", F.lit(True)), "account_id", "left")
    .join(products, "product_id", "left"))
line_reason = (F.when(F.col("_account_ok").isNull(), F.lit("orphan_account"))
    .when(F.col("_product_ok").isNull(), F.lit("invalid_product")))
checked_lines = checked_lines.withColumn("_reason", line_reason)
line_issues = (checked_lines.filter(F.col("_reason").isNotNull())
    .select(F.lit(participant_key).alias("participant_key"), F.lit("postpaid_lines").alias("dataset"),
        "source_row_id", F.col("line_id").alias("record_key"), F.col("_reason").alias("reason_code"),
        F.current_timestamp().alias("quarantined_at")))
valid_lines = checked_lines.filter(F.col("_reason").isNull())

invoices = (spark.table(table("bronze", "postpaid_invoices"))
    .withColumn("amount_value", F.col("amount").cast("decimal(12,2)"))
    .join(valid_accounts.select("account_id").withColumn("_account_ok", F.lit(True)), "account_id", "left"))
invoice_reason = (F.when(F.col("_account_ok").isNull(), F.lit("orphan_account"))
    .when(F.col("amount_value") <= 0, F.lit("negative_amount")))
invoices = invoices.withColumn("_reason", invoice_reason)
invoice_issues = (invoices.filter(F.col("_reason").isNotNull())
    .select(F.lit(participant_key).alias("participant_key"), F.lit("postpaid_invoices").alias("dataset"),
        "source_row_id", F.col("invoice_id").alias("record_key"), F.col("_reason").alias("reason_code"),
        F.current_timestamp().alias("quarantined_at")))
account_value = (invoices.filter(F.col("_reason").isNull()).groupBy("account_id")
    .agg(F.sum("amount_value").alias("account_value")))
line_count = valid_lines.groupBy("account_id").agg(F.count("line_id").alias("line_count"))
postpaid_service = (valid_lines.join(account_value, "account_id", "left").join(line_count, "account_id")
    .select("participant_key", F.col("line_id").alias("service_id"), F.col("msisdn").alias("service_number"),
        F.lit("POSTPAID").alias("service_type"), "customer_id", "product_id",
        F.lower("status").alias("status"),
        (F.coalesce("account_value", F.lit(0)) / F.col("line_count")).cast("decimal(14,2)").alias("monthly_value")))
write_delta(postpaid_service, "silver", "postpaid_service", "participant_key STRING, service_id STRING, service_number STRING, service_type STRING, customer_id STRING, product_id STRING, status STRING, monthly_value DECIMAL(14,2)")
reduce(lambda left, right: left.unionByName(right), [account_issues, line_issues, invoice_issues]).write.format("delta").mode("overwrite").save(location("silver", "_quality/postpaid"))
assert postpaid_service.count() == 398
'''

    home = '''customers = spark.table(table("silver", "customer_master")).select("customer_id").withColumn("_customer_ok", F.lit(True))
products = (spark.table(table("silver", "product_catalog")).filter(F.col("service_type") == "HOME")
    .select("product_id", "monthly_fee").withColumn("_product_ok", F.lit(True)))
services = spark.table(table("bronze", "home_services"))
checked_services = services.join(customers, "customer_id", "left").join(products, "product_id", "left")
service_reason = (F.when(F.col("_customer_ok").isNull(), F.lit("orphan_customer"))
    .when(F.col("_product_ok").isNull(), F.lit("invalid_product")))
checked_services = checked_services.withColumn("_reason", service_reason)
service_issues = (checked_services.filter(F.col("_reason").isNotNull())
    .select(F.lit(participant_key).alias("participant_key"), F.lit("home_services").alias("dataset"),
        "source_row_id", F.col("service_id").alias("record_key"), F.col("_reason").alias("reason_code"),
        F.current_timestamp().alias("quarantined_at")))
valid_services = checked_services.filter(F.col("_reason").isNull())

installations = spark.table(table("bronze", "home_installations"))
installation_window = Window.partitionBy("installation_id").orderBy(F.col("updated_at").desc(), F.col("source_row_id").desc())
checked_installations = (installations.withColumn("_rank", F.row_number().over(installation_window))
    .join(valid_services.select("service_id").withColumn("_service_ok", F.lit(True)), "service_id", "left")
    .join(spark.table(table("silver", "customer_addresses")).select("address_id").withColumn("_address_ok", F.lit(True)), "address_id", "left"))
installation_reason = (F.when(F.col("_rank") > 1, F.lit("duplicate_installation"))
    .when(F.col("_service_ok").isNull(), F.lit("orphan_service"))
    .when(F.col("_address_ok").isNull(), F.lit("orphan_address")))
checked_installations = checked_installations.withColumn("_reason", installation_reason)
installation_issues = (checked_installations.filter(F.col("_reason").isNotNull())
    .select(F.lit(participant_key).alias("participant_key"), F.lit("home_installations").alias("dataset"),
        "source_row_id", F.col("installation_id").alias("record_key"), F.col("_reason").alias("reason_code"),
        F.current_timestamp().alias("quarantined_at")))
valid_installations = checked_installations.filter(F.col("_reason").isNull()).select("service_id", "address_id", "technology")
home_service = (valid_services.join(valid_installations, "service_id", "left")
    .select("participant_key", "service_id", F.col("service_number"), F.lit("HOME").alias("service_type"),
        "customer_id", "product_id", F.lower("status").alias("status"),
        F.col("monthly_fee").cast("decimal(14,2)").alias("monthly_value"), "address_id", "technology"))
write_delta(home_service, "silver", "home_service", "participant_key STRING, service_id STRING, service_number STRING, service_type STRING, customer_id STRING, product_id STRING, status STRING, monthly_value DECIMAL(14,2), address_id STRING, technology STRING")
service_issues.unionByName(installation_issues).write.format("delta").mode("overwrite").save(location("silver", "_quality/home"))
assert home_service.count() == 218
'''

    ownership = '''prepaid = spark.table(table("silver", "prepaid_service")).withColumn("address_id", F.lit(None).cast("string")).withColumn("technology", F.lit(None).cast("string"))
postpaid = spark.table(table("silver", "postpaid_service")).withColumn("address_id", F.lit(None).cast("string")).withColumn("technology", F.lit(None).cast("string"))
home = spark.table(table("silver", "home_service"))
columns = ["participant_key", "service_id", "service_number", "service_type", "customer_id", "product_id", "status", "monthly_value", "address_id", "technology"]
service_ownership = reduce(lambda left, right: left.unionByName(right), [prepaid.select(*columns), postpaid.select(*columns), home.select(*columns)])
write_delta(service_ownership, "silver", "service_ownership", "participant_key STRING, service_id STRING, service_number STRING, service_type STRING, customer_id STRING, product_id STRING, status STRING, monthly_value DECIMAL(14,2), address_id STRING, technology STRING")
quality = reduce(lambda left, right: left.unionByName(right), [
    spark.read.format("delta").load(location("silver", "_quality/customer")),
    spark.read.format("delta").load(location("silver", "_quality/prepaid")),
    spark.read.format("delta").load(location("silver", "_quality/postpaid")),
    spark.read.format("delta").load(location("silver", "_quality/home")),
])
write_delta(quality, "silver", "quality_issues", "participant_key STRING, dataset STRING, source_row_id STRING, record_key STRING, reason_code STRING, quarantined_at TIMESTAMP")
assert service_ownership.count() == 1261 and quality.count() == 30
'''

    customer_360 = '''customers = spark.table(table("silver", "customer_master"))
addresses = spark.table(table("silver", "customer_addresses"))
address_window = Window.partitionBy("customer_id").orderBy(F.col("is_primary").desc(), F.col("updated_at").desc(), F.col("address_id"))
primary_address = (addresses.withColumn("_rank", F.row_number().over(address_window)).filter(F.col("_rank") == 1)
    .select("customer_id", "address_id", "address_line", "city", "province", "region"))
ownership = spark.table(table("silver", "service_ownership"))
service_summary = (ownership.groupBy("participant_key", "customer_id")
    .agg(F.sum(F.when(F.col("service_type") == "PREPAID", 1).otherwise(0)).alias("prepaid_lines"),
        F.sum(F.when(F.col("service_type") == "POSTPAID", 1).otherwise(0)).alias("postpaid_lines"),
        F.sum(F.when(F.col("service_type") == "HOME", 1).otherwise(0)).alias("home_services"),
        F.count("service_id").alias("total_services"),
        F.sum("monthly_value").cast("decimal(16,2)").alias("monthly_value_total")))
customer_360 = (customers.join(primary_address, "customer_id", "left")
    .join(service_summary, ["participant_key", "customer_id"], "left")
    .select("participant_key", "customer_id", "full_name", "document_number", "segment", "status",
        "address_id", "address_line", "city", "province", "region",
        F.coalesce("prepaid_lines", F.lit(0)).cast("bigint").alias("prepaid_lines"),
        F.coalesce("postpaid_lines", F.lit(0)).cast("bigint").alias("postpaid_lines"),
        F.coalesce("home_services", F.lit(0)).cast("bigint").alias("home_services"),
        F.coalesce("total_services", F.lit(0)).cast("bigint").alias("total_services"),
        F.coalesce("monthly_value_total", F.lit(0)).cast("decimal(16,2)").alias("monthly_value_total")))
write_delta(customer_360, "gold", "customer_360", "participant_key STRING, customer_id STRING, full_name STRING, document_number STRING, segment STRING, status STRING, address_id STRING, address_line STRING, city STRING, province STRING, region STRING, prepaid_lines BIGINT, postpaid_lines BIGINT, home_services BIGINT, total_services BIGINT, monthly_value_total DECIMAL(16,2)")
assert customer_360.count() == 493
'''

    portfolio = '''ownership = spark.table(table("silver", "service_ownership"))
customers = spark.table(table("silver", "customer_master")).select("customer_id", "full_name", "document_number")
products = spark.table(table("silver", "product_catalog")).select("product_id", "product_name", "product_family")
addresses = spark.table(table("silver", "customer_addresses"))
address_window = Window.partitionBy("customer_id").orderBy(F.col("is_primary").desc(), F.col("updated_at").desc(), F.col("address_id"))
primary_address = (addresses.withColumn("_rank", F.row_number().over(address_window)).filter(F.col("_rank") == 1)
    .select("customer_id", F.col("address_id").alias("customer_address_id"), "city", "province", "region"))
customer_service_portfolio = (ownership.join(customers, "customer_id")
    .join(products, "product_id").join(primary_address, "customer_id", "left")
    .withColumn("service_address_id", F.coalesce("address_id", "customer_address_id"))
    .select("participant_key", "customer_id", "full_name", "document_number", "service_id", "service_number",
        "service_type", "product_id", "product_name", "product_family", "status", "monthly_value",
        "service_address_id", "city", "province", "region", "technology"))
write_delta(customer_service_portfolio, "gold", "customer_service_portfolio", "participant_key STRING, customer_id STRING, full_name STRING, document_number STRING, service_id STRING, service_number STRING, service_type STRING, product_id STRING, product_name STRING, product_family STRING, status STRING, monthly_value DECIMAL(14,2), service_address_id STRING, city STRING, province STRING, region STRING, technology STRING")
assert customer_service_portfolio.count() == 1261
'''

    geography = '''customer_360 = spark.table(table("gold", "customer_360"))
portfolio = spark.table(table("gold", "customer_service_portfolio"))
customer_counts = customer_360.groupBy("participant_key", "region", "province", "city").agg(
    F.countDistinct("customer_id").alias("customers"),
    F.sum("monthly_value_total").cast("decimal(18,2)").alias("monthly_value_total"))
service_counts = (portfolio.groupBy("participant_key", "region", "province", "city")
    .agg(F.count("service_id").alias("services"),
        F.sum(F.when(F.col("service_type") == "PREPAID", 1).otherwise(0)).alias("prepaid_services"),
        F.sum(F.when(F.col("service_type") == "POSTPAID", 1).otherwise(0)).alias("postpaid_services"),
        F.sum(F.when(F.col("service_type") == "HOME", 1).otherwise(0)).alias("home_services")))
geographic_service_summary = customer_counts.join(service_counts, ["participant_key", "region", "province", "city"], "inner")
write_delta(geographic_service_summary, "gold", "geographic_service_summary", "participant_key STRING, region STRING, province STRING, city STRING, customers BIGINT, services BIGINT, prepaid_services BIGINT, postpaid_services BIGINT, home_services BIGINT, monthly_value_total DECIMAL(18,2)")
assert geographic_service_summary.count() == 12
'''

    validate = f'''expected_counts = {EXPECTED_TABLE_COUNTS!r}
for logical_name, expected in expected_counts.items():
    layer = "gold" if logical_name in {{"customer_360", "customer_service_portfolio", "geographic_service_summary"}} else "silver"
    actual = spark.table(table(layer, logical_name)).count()
    assert actual == expected, f"{{layer}}.{{logical_name}} expected {{expected}}, got {{actual}}"

for layer, logical_names in {TABLES!r}.items():
    for logical_name in logical_names:
        details = spark.sql(f"DESCRIBE FORMATTED {{table(layer, logical_name)}}")
        formatted = {{
            str(row["col_name"]).strip().lower(): str(row["data_type"]).strip().lower()
            for row in details.collect()
        }}
        expected_provider = "csv" if layer == "landing" else "delta"
        assert formatted.get("provider") == expected_provider, f"{{table(layer, logical_name)}} provider mismatch: {{formatted.get('provider')}}"
        assert formatted.get("type") == "managed", f"{{table(layer, logical_name)}} must be managed: {{formatted.get('type')}}"
        if layer != "landing":
            assert spark.sql(f"DESCRIBE HISTORY {{table(layer, logical_name)}}").count() >= 1

portfolio = spark.table(table("gold", "customer_service_portfolio"))
service_mix = {{row["service_type"]: row["count"] for row in portfolio.groupBy("service_type").count().collect()}}
assert service_mix == {{"PREPAID": 645, "POSTPAID": 398, "HOME": 218}}
customer_total = spark.table(table("gold", "customer_360")).agg(F.sum("monthly_value_total").alias("value")).first()["value"]
portfolio_total = portfolio.agg(F.sum("monthly_value").alias("value")).first()["value"]
geographic_total = spark.table(table("gold", "geographic_service_summary")).agg(F.sum("monthly_value_total").alias("value")).first()["value"]
assert abs(float(customer_total) - float(portfolio_total)) < 0.01
assert abs(float(customer_total) - float(geographic_total)) < 0.01
assert spark.conf.get("spark.aidp.lineage.enabled", "true").lower() == "true"
print("Telco Customer 360 validated: 31 managed tables, 30 quality issues, governed medallion lineage")
'''

    return {
        "01_landing_telco_lineage": landing,
        "02_bronze_crm_products": _bronze_code(["crm_customers", "crm_addresses", "product_catalog"]),
        "03_bronze_prepaid": _bronze_code(["prepaid_lines", "prepaid_recharges"]),
        "04_bronze_postpaid": _bronze_code(["postpaid_accounts", "postpaid_lines", "postpaid_invoices"]),
        "05_bronze_home": _bronze_code(["home_services", "home_installations"]),
        "06_silver_customer": customer,
        "07_silver_prepaid": prepaid,
        "08_silver_postpaid": postpaid,
        "09_silver_home": home,
        "10_silver_ownership_quality": ownership,
        "11_gold_customer_360": customer_360,
        "12_gold_service_portfolio": portfolio,
        "13_gold_geographic_summary": geography,
        "14_validate_lineage": validate,
    }


def _notebook(title: str, code: str) -> bytes:
    cells = [
        {
            "cell_type": "markdown", "metadata": {},
            "source": [
                f"# {title}\n", "\n",
                "**Audience:** data engineers validating medallion architecture and AIDP lineage.\n",
                "\n", "**Prerequisites:** the canonical lab assets, shared compute and five job parameters.\n",
                "\n", "**Learning goals:** trace governed transformations, verify isolation, and inspect deterministic results.\n",
            ],
        },
        {"cell_type": "code", "execution_count": None, "metadata": {}, "outputs": [], "source": _bootstrap_code().splitlines(keepends=True)},
        {
            "cell_type": "markdown", "metadata": {},
            "source": ["## Transformation\n", "\n", "Run this cell once. It is idempotent and checks its row-level contract.\n"],
        },
        {"cell_type": "code", "execution_count": None, "metadata": {}, "outputs": [], "source": code.splitlines(keepends=True)},
        {
            "cell_type": "markdown", "metadata": {},
            "source": [
                "## Exercise and common pitfall\n", "\n",
                "**Exercise:** follow one customer or service identifier into the next task and explain every derived column.\n",
                "\n", "**Answer scaffold:** identify the source table, join key, transformation and target column.\n",
                "\n", "**Pitfall:** never replace the job parameters with participant-specific literals; doing so breaks canonical hashes and isolation.\n",
                "\n", "**Extension:** inspect the resulting entity and column lineage in Master Catalog.\n",
            ],
        },
    ]
    notebook = {
        "cells": cells,
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python", "version": "3.12"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    return (json.dumps(notebook, ensure_ascii=False, indent=1) + "\n").encode("utf-8")


def _business_aggregates(data: dict[str, list[dict[str, object]]]) -> dict[str, str]:
    result = {}
    for dataset, column in (("prepaid_recharges", "amount"), ("postpaid_invoices", "amount"), ("product_catalog", "monthly_fee")):
        result[f"{dataset}.{column}.sum"] = f"{sum(Decimal(str(row[column])) for row in data[dataset]):.2f}"
    return result


def _write_if_changed(path: Path, content: bytes) -> None:
    if path.is_file() and path.read_bytes() == content:
        return
    path.write_bytes(content)


def generate() -> None:
    data = _source_data()
    assert {name: len(rows) for name, rows in data.items()} == EXPECTED_SOURCE_COUNTS
    _validate_source_contract(data)
    SOURCE_ROOT.mkdir(parents=True, exist_ok=True)
    NOTEBOOK_ROOT.mkdir(parents=True, exist_ok=True)

    datasets = []
    for name, rows in data.items():
        content = _csv_bytes(DATASET_COLUMNS[name], rows)
        path = SOURCE_ROOT / f"{name}.csv"
        _write_if_changed(path, content)
        datasets.append({"file": path.name, "name": name, "row_count": len(rows), "sha256": _sha256(content)})

    task_code = _task_code()
    notebooks = []
    for task_key, depends_on, title in TASKS:
        content = _notebook(title, task_code[task_key])
        path = NOTEBOOK_ROOT / f"{task_key}.ipynb"
        _write_if_changed(path, content)
        notebooks.append({"task_key": task_key, "file": path.name, "depends_on": depends_on, "sha256": _sha256(content)})

    expected_entity_edges = [
        "landing.crm_customers->bronze.crm_customers",
        "bronze.crm_customers->silver.customer_master",
        "bronze.prepaid_lines->silver.prepaid_service",
        "bronze.postpaid_lines->silver.postpaid_service",
        "bronze.home_services->silver.home_service",
        "silver.prepaid_service->silver.service_ownership",
        "silver.postpaid_service->silver.service_ownership",
        "silver.home_service->silver.service_ownership",
        "silver.customer_master->gold.customer_360",
        "silver.service_ownership->gold.customer_360",
        "silver.service_ownership->gold.customer_service_portfolio",
        "gold.customer_360->gold.geographic_service_summary",
        "gold.customer_service_portfolio->gold.geographic_service_summary",
    ]
    expected_column_edges = [
        "bronze.crm_customers.document_number->silver.customer_master.document_number->gold.customer_360.document_number",
        "bronze.prepaid_lines.msisdn->silver.prepaid_service.service_number->silver.service_ownership.service_number->gold.customer_service_portfolio.service_number",
        "bronze.postpaid_lines.msisdn->silver.postpaid_service.service_number->silver.service_ownership.service_number->gold.customer_service_portfolio.service_number",
        "bronze.crm_addresses.region->silver.customer_addresses.region->gold.customer_360.region->gold.geographic_service_summary.region",
        "bronze.prepaid_recharges.amount->silver.prepaid_service.monthly_value->silver.service_ownership.monthly_value->gold.customer_360.monthly_value_total->gold.geographic_service_summary.monthly_value_total",
        "bronze.product_catalog.product_name->silver.product_catalog.product_name->gold.customer_service_portfolio.product_name",
    ]
    metadata = {
        "schema_version": 1,
        "lab_id": LAB_ID,
        "display_name": "Telco Customer 360 Lineage",
        "pack_version": "1.1.2",
        "status": "available",
        "datasets": datasets,
        "notebooks": notebooks,
        "tables": TABLES,
        "formats": {"landing": "CSV", "bronze": "DELTA", "silver": "DELTA", "gold": "DELTA"},
        "table_storage": {"landing": "MANAGED", "bronze": "MANAGED", "silver": "MANAGED", "gold": "MANAGED"},
        "expected_results": {
            "source_row_counts": EXPECTED_SOURCE_COUNTS,
            "business_aggregates": _business_aggregates(data),
            "quality": {"minimum_quarantined_rows": 30, "exact_quarantined_rows": 30},
            "lineage": {
                "target_tables": TABLES["gold"],
                "expected_table_rows": EXPECTED_TABLE_COUNTS,
                "expected_entity_edges": expected_entity_edges,
                "expected_column_edges": expected_column_edges,
                "qualified_node_template": "aidp_lab.oci_{layer}.{participant_key}_telco_lineage_{table}",
                "required_schema_paths": [
                    "aidp_lab.oci_landing.", "aidp_lab.oci_bronze.",
                    "aidp_lab.oci_silver.", "aidp_lab.oci_gold.",
                ],
                "forbidden_schema_paths": ["aidp_lab.telco_lineage."],
                "direction": "BOTH", "max_depth": 8,
            },
        },
    }
    unsigned = json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("utf-8")
    metadata["pack_sha256"] = _sha256(unsigned)
    _write_if_changed(
        LAB_ROOT / "lab.json",
        (json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Verify that regeneration produces no package diff")
    args = parser.parse_args()
    before = {path.relative_to(LAB_ROOT): path.read_bytes() for path in LAB_ROOT.rglob("*") if path.is_file()} if LAB_ROOT.exists() else {}
    generate()
    if args.check:
        after = {path.relative_to(LAB_ROOT): path.read_bytes() for path in LAB_ROOT.rglob("*") if path.is_file()}
        if before != after:
            raise SystemExit("telco_lineage package is not deterministic or is stale")


if __name__ == "__main__":
    main()
