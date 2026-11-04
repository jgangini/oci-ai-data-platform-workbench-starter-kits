from copy import deepcopy
from datetime import datetime, timezone

import pytest

from app.ai_data_governance.inventory import canonical, table_records
from app.ai_data_governance.repository import checked_rows


AT = datetime(2026, 10, 31, tzinfo=timezone.utc)


def observed(**changes):
    return {"table_key": "catalog.schema.users", "native_version": None, "name": "users",
            "description": None, "columns": (
                {"name": "ABC13", "native_key": None, "ordinal": 0, "data_type": "STRING", "description": None},
                {"name": "id", "native_key": "native-column-2", "ordinal": 1, "data_type": "INT", "description": "Existing [IA]"},
            ), **changes}


def records(table=None, **kwargs):
    return table_records("platform", "scope", "run", table or observed(), AT, **kwargs)


def test_observations_conform_to_the_single_model_without_inventing_meaning():
    result = records()
    for target, values in [("ASSET", result.assets), ("ASSET_VERSION", result.versions),
                           ("SCOPE_ASSET", result.projections), ("EVIDENCE", result.evidence),
                           ("WORK", result.work)]:
        assert checked_rows("AI_DG_" + target, values) == values
    assert len(result.assets) == len(result.versions) == len(result.evidence) == 3
    assert len(result.work) == 2
    assert all(w["state"] == "WAIT_CONTEXT" and w["phase"] == "DOCUMENT" for w in result.work)
    assert result.assets[0]["identity_status"] == "RESOLVED"
    assert result.assets[1]["identity_status"] == "FALLBACK"
    assert all(a["lifecycle_state"] == "ACTIVE" for a in result.assets)
    assert all(p["consistency_state"] == "VERIFIED" for p in result.projections)
    assert all(e["evidence_kind"] == "CATALOG" for e in result.evidence)
    assert result.versions[2]["description_origin"] == "EXTERNAL_UNVERIFIED"
    assert result.versions[1]["description"] is None
    assert result.versions[1]["ordinal_position"] == 0
    assert all(v["owner_ref"] is None and v["native_version"] is None for v in result.versions)


def test_observation_ids_are_stable_across_run_and_timestamp():
    first = records()
    second = table_records("platform", "scope", "other-run", observed(), AT.replace(hour=1))
    for group, key in [("assets", "asset_id"), ("versions", "version_id"),
                       ("evidence", "evidence_id"), ("work", "work_id")]:
        assert [r[key] for r in getattr(first, group)] == [r[key] for r in getattr(second, group)]
    # Writer must retain original provenance rather than insert these as replacements.
    assert first.versions[0]["observed_run_id"] != second.versions[0]["observed_run_id"]


def test_version_changes_do_not_rename_the_asset_or_forge_a_native_etag():
    first = records()
    table = observed()
    table["columns"][0]["description"] = "Observed externally"
    second = records(table)
    assert [v["asset_id"] for v in first.versions] == [v["asset_id"] for v in second.versions]
    assert first.versions[1]["version_id"] != second.versions[1]["version_id"]
    assert second.versions[1]["native_version"] is None
    assert len(second.work) == 1


def test_metadata_change_retains_one_work_identity_per_asset_and_field():
    first = records()
    table = observed(native_version="new-observed-version")
    second = records(table)
    assert [w["work_id"] for w in first.work] == [w["work_id"] for w in second.work]
    assert first.work[0]["expected_version_id"] != second.work[0]["expected_version_id"]
    assert first.versions[0]["description_origin"] == "UNKNOWN"


def test_missing_column_coverage_is_partial_and_not_an_empty_schema():
    partial = records(observed(columns=None))
    empty = records(observed(columns=()))
    assert len(partial.assets) == len(empty.assets) == 1
    assert partial.projections[0]["coverage_state"] == "PARTIAL"
    assert empty.projections[0]["coverage_state"] == "COMPLETE"
    assert partial.versions[0]["version_id"] != empty.versions[0]["version_id"]


def test_existing_human_and_ia_texts_do_not_create_documentation_work():
    table = observed(description="Existing human description")
    table["columns"][0]["description"] = "  Existing [IA]  "
    assert records(table).work == []


def test_scope_separates_evidence_work_and_projection_not_shared_asset_identity():
    first = records()
    second = table_records("platform", "scope-2", "run", observed(), AT)
    assert first.assets == second.assets and first.versions == second.versions
    assert first.evidence[0]["evidence_id"] != second.evidence[0]["evidence_id"]
    assert first.work[0]["work_id"] != second.work[0]["work_id"]


def test_exact_migrated_column_identity_is_preserved_without_similarity_matching():
    key = canonical(["COLUMN", canonical(["catalog.schema.users", "ABC13"])])
    result = records(legacy_ids={key: "existing-column-id"})
    assert result.assets[1]["asset_id"] == "existing-column-id"
    other = observed()
    other["columns"][0]["name"] = "ABC14"
    assert records(other, legacy_ids={key: "existing-column-id"}).assets[1]["asset_id"] != "existing-column-id"


@pytest.mark.parametrize("change", [
    lambda t: t.update(columns=[{}]),
    lambda t: t.update(columns=[t["columns"][0], t["columns"][0]]),
    lambda t: t["columns"][0].update(values=["business rows"]),
    lambda t: t["columns"][0].update(native_key=12),
    lambda t: t["columns"][0].update(description=12),
])
def test_invalid_observations_fail_before_any_persistence(change):
    table = deepcopy(observed())
    change(table)
    with pytest.raises(ValueError):
        records(table)
