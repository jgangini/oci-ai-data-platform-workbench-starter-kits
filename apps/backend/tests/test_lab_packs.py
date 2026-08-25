import ast
import csv
import io
import json
from decimal import Decimal

import pytest

from app.lab_packs import lab_catalog, load_lab_pack, module_catalog, public_lab_catalog
from scripts.generate_telco_lineage_lab import _source_data, _validate_source_contract


LEGACY_LABS = ("banking", "telecommunications", "retail", "healthcare")
ACTIVE_LABS = ("banking", "telecommunications", "telco_lineage", "retail", "healthcare")


def test_catalog_separates_five_participant_labs_from_global_governance_module() -> None:
    packs = lab_catalog()
    public = public_lab_catalog()
    assert tuple(pack.lab_id for pack in packs) == (*ACTIVE_LABS, "ai_data_governance_vsc_extension")
    assert tuple(item["lab_id"] for item in public) == ACTIVE_LABS
    assert all(pack.available for pack in packs[:5])
    assert {pack.lab_id: pack.pack_version for pack in packs[:5]} == dict.fromkeys(
        ACTIVE_LABS, "2.0.0"
    )
    assert all(item["description"].strip() for item in public)
    assert "transactions" in public[0]["description"]
    assert packs[-1].status == "available"
    assert packs[-1].pack_version == "3.0.0"
    assert packs[-1].kind == "governance_extension"
    assert packs[-1].scope == "global"
    assert packs[-1].installation_modes == ("production",)
    assert not packs[-1].datasets and not packs[-1].notebooks
    assert module_catalog() == (packs[-1],)
    module = load_lab_pack("ai_data_governance_vsc_extension")
    assert module.agent["editable_by"] == "AI_DATA_PLATFORM_ADMIN"
    assert module.agent["tools"] == ["catalog_inventory", "catalog_lineage"]


@pytest.mark.parametrize("lab_id", LEGACY_LABS)
def test_pack_hashes_rows_notebooks_parameters_and_lineage_contract(lab_id: str) -> None:
    pack = load_lab_pack(lab_id)
    assert len(pack.datasets) == 4
    assert len(pack.notebooks) == 5
    assert [asset.task_key.split("_", 1)[0] for asset in pack.notebooks] == [
        "01", "02", "03", "04", "05"
    ]
    assert pack.notebooks[-1].task_key == f"05_lineage_{lab_id}"
    assert "lineage_demo" not in pack.tables["gold"]
    assert pack.formats == dict.fromkeys(
        ("landing", "bronze", "silver", "gold"), "DELTA"
    )
    lineage = pack.expected_results["lineage"]
    assert lineage["target_tables"] == list(pack.tables["gold"])
    assert lineage["levels"] == ["ENTITY", "COLUMN"]
    assert lineage["direction"] == "BOTH"
    assert lineage["max_depth"] == 8
    assert lineage["should_include_edges"] is True
    assert lineage["expected_entity_edges"] and lineage["expected_column_edges"]
    for asset in pack.datasets:
        rows = list(csv.reader(io.StringIO(asset.read_bytes().decode("utf-8"))))
        assert "participant_key" not in rows[0]
        assert len(rows) - 1 == asset.row_count
    assert pack.expected_results["source_row_counts"] == {
        asset.name.removesuffix(".csv"): asset.row_count for asset in pack.datasets
    }
    assets = {asset.name.removesuffix(".csv"): asset for asset in pack.datasets}
    for expression, expected in pack.expected_results["business_aggregates"].items():
        dataset, column, operation = expression.split(".")
        assert operation == "sum"
        rows = csv.DictReader(
            io.StringIO(assets[dataset].read_bytes().decode("utf-8"))
        )
        assert sum(Decimal(row[column]) for row in rows) == Decimal(expected)
    assert pack.expected_results["quality"]["minimum_quarantined_rows"] >= 1
    rendered = ""
    for asset in pack.notebooks:
        notebook = json.loads(asset.read_bytes())
        for cell in notebook["cells"]:
            if cell["cell_type"] == "code":
                source = "".join(cell["source"])
                ast.parse(source)
                rendered += source
    for parameter in (
        "participant_key", "lab_id", "workspace_root", "bucket_name",
        "objectstorage_namespace", "catalog_name",
    ):
        assert f'required_parameter("{parameter}")' in rendered
    assert 'oidlUtils.parameters.getParameter(name, "")' in rendered
    assert "import oidlUtils" not in rendered
    assert all(b"\r\n" not in asset.read_bytes() for asset in pack.notebooks)
    assert "spark.aidp.lineage.enabled=false" not in rendered
    assert "lineage_demo" not in rendered
    assert "CREATE EXTERNAL TABLE" not in rendered
    assert 'saveAsTable(table("landing", dataset))' in rendered
    assert 'saveAsTable(table("bronze", dataset))' in rendered
    assert 'saveAsTable(table("silver", dataset))' in rendered
    assert 'saveAsTable(table("gold",' in rendered


def test_telco_lineage_pack_has_variable_dag_delta_lineage_and_tutorials() -> None:
    pack = load_lab_pack("telco_lineage")
    assert len(pack.datasets) == 10
    assert sum(asset.row_count or 0 for asset in pack.datasets) == 7405
    assert len(pack.notebooks) == 14
    assert sum(len(names) for names in pack.tables.values()) == 31
    assert pack.formats == {
        "landing": "DELTA", "bronze": "DELTA", "silver": "DELTA", "gold": "DELTA",
    }
    assert pack.tables["silver"] == (
        "customer_master", "customer_addresses", "product_catalog",
        "prepaid_service", "postpaid_service", "home_service",
        "service_ownership", "quality_issues",
    )
    assert pack.tables["gold"] == (
        "customer_360", "customer_service_portfolio", "geographic_service_summary",
    )
    assert pack.expected_results["quality"]["exact_quarantined_rows"] == 30
    assert pack.expected_results["lineage"]["expected_table_rows"] == {
        "customer_master": 493, "customer_addresses": 617, "product_catalog": 15,
        "prepaid_service": 645, "postpaid_service": 398, "home_service": 218,
        "service_ownership": 1261, "quality_issues": 30, "customer_360": 493,
        "customer_service_portfolio": 1261, "geographic_service_summary": 12,
    }
    assert pack.notebooks[9].depends_on == (
        "06_silver_customer", "07_silver_prepaid", "08_silver_postpaid", "09_silver_home",
    )
    assert pack.notebooks[12].depends_on == (
        "11_gold_customer_360", "12_gold_service_portfolio",
    )

    rendered = ""
    for asset in pack.notebooks:
        notebook = json.loads(asset.read_bytes())
        assert all(
            cell.get("execution_count") is None and cell.get("outputs") == []
            for cell in notebook["cells"] if cell["cell_type"] == "code"
        )
        markdown = "".join(
            "".join(cell["source"]) for cell in notebook["cells"] if cell["cell_type"] == "markdown"
        )
        assert "Learning goals" in markdown and "Exercise" in markdown and "Pitfall" in markdown
        for cell in notebook["cells"]:
            if cell["cell_type"] == "code":
                source = "".join(cell["source"])
                ast.parse(source)
                rendered += source
    for parameter in (
        "participant_key", "lab_id", "workspace_root", "bucket_name",
        "objectstorage_namespace", "catalog_name",
    ):
        assert rendered.count(f'required_parameter("{parameter}")') == 14
    assert "CREATE EXTERNAL TABLE IF NOT EXISTS" not in rendered
    assert "USING ICEBERG" not in rendered
    assert 'source.write.format("delta")' in rendered
    assert 'frame.write.format("delta")' in rendered
    assert ".saveAsTable(target)" in rendered
    assert '.option("path", target_location)' not in rendered
    assert 'formatted.get("type") == "managed"' in rendered
    assert 'spark.conf.get("spark.aidp.lineage.enabled", "true").lower() == "true"' in rendered
    assert "nadia.cloud.ai" not in rendered.casefold()

    metadata = json.loads((pack.datasets[0].path.parents[1] / "lab.json").read_text(encoding="utf-8"))
    assert metadata["table_storage"] == dict.fromkeys(
        ("landing", "bronze", "silver", "gold"), "MANAGED"
    )
    lineage = metadata["expected_results"]["lineage"]
    assert lineage["required_schema_paths"] == [
        "{participant_key}_aidp.oci_landing.", "{participant_key}_aidp.oci_bronze.",
        "{participant_key}_aidp.oci_silver.", "{participant_key}_aidp.oci_gold.",
    ]
    assert lineage["forbidden_schema_paths"] == ["aidp_lab.telco_lineage."]
    assert lineage["qualified_node_template"] == (
        "{participant_key}_aidp.oci_{layer}.{participant_key}_telco_lineage_{table}"
    )


def test_telco_lineage_source_contract_has_exactly_thirty_independent_issues() -> None:
    assert _validate_source_contract(_source_data()) == {
        "crm_customers": 7, "crm_addresses": 3, "product_catalog": 0,
        "prepaid_lines": 5, "prepaid_recharges": 3,
        "postpaid_accounts": 2, "postpaid_lines": 2, "postpaid_invoices": 3,
        "home_services": 2, "home_installations": 3,
    }


def test_canonical_assets_are_identical_for_every_participant() -> None:
    pack = load_lab_pack("banking")
    first = [asset.read_bytes() for asset in (*pack.datasets, *pack.notebooks)]
    second = [asset.read_bytes() for asset in (*load_lab_pack("banking").datasets, *load_lab_pack("banking").notebooks)]
    assert first == second
    rendered = b"\n".join(first)
    assert b"u_0000000000000000" not in rendered
    assert b"canonical-bucket" not in rendered
    assert b"canonical-namespace" not in rendered
