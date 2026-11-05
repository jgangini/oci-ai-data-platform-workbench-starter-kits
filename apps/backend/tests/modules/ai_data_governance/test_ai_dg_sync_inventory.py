import json
from types import SimpleNamespace

import pytest

from app.modules.ai_data_governance.sync_inventory import synchronize_scope as synchronize_admitted_scope
from tests.modules.ai_data_governance.test_ai_dg_inventory_writer import MemoryRepository


def synchronize_scope(*args, **kwargs):
    return synchronize_admitted_scope(*args, check_active=kwargs.pop("check_active", lambda: None), **kwargs)


class Reader:
    def __init__(self, tables, *, fail_page=False):
        self.tables = tables
        self.fail_page = fail_page
        self.detail_calls = []

    def table_pages(self, scope):
        yield SimpleNamespace(items=tuple({"key": key} for key in self.tables))
        if self.fail_page:
            raise TimeoutError("later page unavailable")

    def read_table(self, scope, key):
        self.detail_calls.append(key)
        value = self.tables[key]
        if isinstance(value, Exception):
            raise value
        return value


def table(name="users", columns=()):
    return {"table_key": "catalog.schema." + name, "name": name, "native_version": None,
            "description": None, "columns": columns}


def target():
    repository = MemoryRepository()
    repository.data["AI_DG_SCOPE"][("scope",)]["limits_json"] = '{"max_tables":100}'
    repository.data["AI_DG_RUN"][("run",)]["manifest_json"] = '{"other_checkpoint":"retain"}'
    return repository


def test_empty_catalog_first_use_finishes_without_model_or_fake_assets():
    repository = target()
    result = synchronize_scope(repository, Reader({}), "scope", "run")
    assert result["status"] == "SUCCEEDED" and result["assets_confirmed"] == 0
    assert result["model_calls"] == result["catalog_writes"] == 0
    assert not repository.data["AI_DG_ASSET"] and not repository.data["AI_DG_DOCUMENT"]
    run = repository.data["AI_DG_RUN"][("run",)]
    assert run["status"] == "SUCCEEDED" and run["finished_at"] is not None
    assert json.loads(run["manifest_json"])["other_checkpoint"] == "retain"


def test_table_batches_are_bounded_and_confirmed_before_run_completion():
    repository = target()
    repository.max_batch_rows = 2
    reader = Reader({"catalog.schema." + name: table(name) for name in ("one", "two", "three")})
    result = synchronize_scope(repository, reader, "scope", "run")
    assert result["assets_confirmed"] == result["tables_observed"] == 3
    assert all(len(rows) <= 2 for _, _, rows in repository.calls)
    assert all(row["state"] == "WAIT_CONTEXT" for row in repository.data["AI_DG_WORK"].values())


def test_failed_page_keeps_confirmed_observations_and_reports_partial_not_deletions():
    repository = target()
    reader = Reader({"catalog.schema.users": table()}, fail_page=True)
    result = synchronize_scope(repository, reader, "scope", "run")
    assert result["status"] == "PARTIAL" and result["assets_confirmed"] == 1
    assert not result["enumeration_complete"]
    assert result["enumeration_error"] == "CATALOG_ENUMERATION_FAILED"
    assert next(iter(repository.data["AI_DG_ASSET"].values()))["lifecycle_state"] == "ACTIVE"


def test_missing_columns_and_failed_details_are_partial_not_empty_success():
    repository = target()
    reader = Reader({"catalog.schema.users": table(columns=None), "catalog.schema.failed": TimeoutError()})
    result = synchronize_scope(repository, reader, "scope", "run")
    assert result["status"] == "PARTIAL" and result["tables_failed"] == result["tables_partial"] == 1
    assert result["enumeration_complete"]


def test_uncertain_delta_commit_stops_phase_instead_of_masking_as_catalog_failure():
    repository = target()
    repository.fail_table = "AI_DG_ASSET_VERSION"
    reader = Reader({"catalog.schema.users": table()})
    with pytest.raises(TimeoutError):
        synchronize_scope(repository, reader, "scope", "run")
    assert repository.data["AI_DG_RUN"][("run",)]["status"] == "RUNNING"
    assert not repository.data["AI_DG_SCOPE_ASSET"]
    result = synchronize_scope(repository, reader, "scope", "run")
    assert result["status"] == "SUCCEEDED" and len(repository.data["AI_DG_ASSET_VERSION"]) == 1


def test_completed_same_attempt_reuses_receipt_without_rereading_catalog():
    repository = target()
    reader = Reader({"catalog.schema.users": table()})
    first = synchronize_scope(repository, reader, "scope", "run")
    reader.detail_calls.clear()
    assert synchronize_scope(repository, reader, "scope", "run") == first
    assert reader.detail_calls == []


def test_cancelled_run_or_disabled_scope_does_not_start_inventory():
    for change in (lambda r: r.data["AI_DG_RUN"][("run",)].update(status="CANCELLED"),
                   lambda r: r.data["AI_DG_SCOPE"][("scope",)].update(enabled=False)):
        repository = target()
        change(repository)
        reader = Reader({})
        with pytest.raises(ValueError):
            synchronize_scope(repository, reader, "scope", "run")
        assert not repository.calls


def test_cancel_during_catalog_read_stops_before_first_commit():
    repository = target()
    reader = Reader({"catalog.schema.users": table()})
    def active():
        if reader.detail_calls:
            raise RuntimeError("Native task cancelled")
    with pytest.raises(RuntimeError, match="cancelled"):
        synchronize_scope(repository, reader, "scope", "run", check_active=active)
    assert not repository.calls
    assert repository.data["AI_DG_RUN"][("run",)]["status"] == "RUNNING"


def test_wide_table_keeps_all_columns_and_parent_without_exceeding_batch_bound():
    repository = target()
    columns = tuple({"name": "c" + str(n), "native_key": None, "ordinal": n,
                     "data_type": "STRING", "description": None} for n in range(500))
    repository.max_batch_rows = 500
    reader = Reader({"catalog.schema.users": table(columns=columns)})
    result = synchronize_scope(repository, reader, "scope", "run")
    assert result["status"] == "SUCCEEDED" and result["assets_confirmed"] == 501
    assert len(repository.data["AI_DG_ASSET"]) == len(repository.data["AI_DG_SCOPE_ASSET"]) == 501
    assert all(len(rows) <= 500 for _, _, rows in repository.calls)
