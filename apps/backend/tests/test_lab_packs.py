import ast
import csv
import io
import json
from decimal import Decimal

import pytest

from app.lab_packs import LabPackError, lab_catalog, load_lab_pack


ACTIVE_LABS = ("banking", "telecommunications", "retail", "healthcare")


def test_catalog_has_four_versioned_labs_and_disabled_agent() -> None:
    packs = lab_catalog()
    assert tuple(pack.lab_id for pack in packs) == (*ACTIVE_LABS, "agent")
    assert all(pack.pack_version == "1.0.0" and pack.available for pack in packs[:4])
    assert packs[-1].status == "planned"
    assert not packs[-1].datasets and not packs[-1].notebooks
    with pytest.raises(LabPackError, match="not available"):
        load_lab_pack("agent")


@pytest.mark.parametrize("lab_id", ACTIVE_LABS)
def test_pack_hashes_rows_notebooks_parameters_and_lineage_contract(lab_id: str) -> None:
    pack = load_lab_pack(lab_id)
    assert len(pack.datasets) == 4
    assert len(pack.notebooks) == 5
    assert [asset.task_key.split("_", 1)[0] for asset in pack.notebooks] == [
        "01", "02", "03", "04", "05"
    ]
    assert pack.notebooks[-1].task_key == f"05_lineage_{lab_id}"
    assert pack.tables["gold"][-1] == "lineage_demo"
    assert pack.expected_results["lineage"] == {
        "target_table": "lineage_demo", "expected_rows": 1
    }
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
        "objectstorage_namespace",
    ):
        assert f'required_parameter("{parameter}")' in rendered
    assert "oidlUtils.parameters.getParameter(name)" in rendered
    assert "import oidlUtils" not in rendered
    assert all(b"\r\n" not in asset.read_bytes() for asset in pack.notebooks)
    assert "spark.aidp.lineage.enabled=false" not in rendered
    assert "lineage_demo" in rendered


def test_canonical_assets_are_identical_for_every_participant() -> None:
    pack = load_lab_pack("banking")
    first = [asset.read_bytes() for asset in (*pack.datasets, *pack.notebooks)]
    second = [asset.read_bytes() for asset in (*load_lab_pack("banking").datasets, *load_lab_pack("banking").notebooks)]
    assert first == second
    rendered = b"\n".join(first)
    assert b"u_0000000000000000" not in rendered
    assert b"canonical-bucket" not in rendered
    assert b"canonical-namespace" not in rendered
