from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.modules.ai_data_governance import repository
from app.modules.ai_data_governance.schema import load_model


STORAGE = "oci://approved_artifacts@testnamespace/oci_artifacts"


def row(table_name="AI_DG_RUN"):
    values = {"STRING": "value", "INT": 1, "BIGINT": 1, "BOOLEAN": True,
              "TIMESTAMP": datetime(2026, 10, 31, tzinfo=timezone.utc)}
    table = next(t for t in load_model()["tables"] if t["name"] == table_name)
    return {c["name"]: values[c["type"]] if c["required"] else None for c in table["columns"]}


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(extra="ignored"), lambda r: r.pop("run_id"),
    lambda r: r.update(run_id=None), lambda r: r.update(attempt_no=True),
    lambda r: r.update(attempt_no=2**31), lambda r: r.update(attempt_no=1.0),
    lambda r: r.update(started_at=datetime(2026, 10, 31)),
    lambda r: r.update(started_at="2026-10-31T00:00:00Z"),
])
def test_bad_batch_rejected_before_spark(mutate):
    spark = Mock()
    target = repository.DeltaRepository(spark, STORAGE)
    record = row()
    mutate(record)
    with pytest.raises(ValueError):
        target.insert_immutable("AI_DG_RUN", [record])
    assert not spark.method_calls


def test_duplicate_source_and_unknown_table_are_not_written():
    spark = Mock()
    target = repository.DeltaRepository(spark, STORAGE)
    for table, rows in [("AI_DG_RUN", [row(), row()]), ("AI_MI_RUN", [row()])]:
        with pytest.raises(ValueError):
            target.insert_immutable(table, rows)
    assert not spark.method_calls


def test_timestamps_are_normalized_without_changing_caller():
    record = row()
    record["started_at"] = datetime(2026, 10, 31, 1, tzinfo=timezone(timedelta(hours=1)))
    original = deepcopy(record)
    checked = repository.checked_rows("AI_DG_RUN", [record])
    assert checked[0]["started_at"] == datetime(2026, 10, 31, tzinfo=timezone.utc)
    assert record == original


def test_batch_bounds_and_empty_batch():
    with pytest.raises(ValueError):
        repository.checked_rows("AI_DG_RUN", [row(), row()], limit=1)
    for bound in (0, True, 1001):
        with pytest.raises(ValueError):
            repository.DeltaRepository(Mock(), STORAGE, max_batch_rows=bound)
    spark = Mock()
    assert repository.DeltaRepository(spark, STORAGE).insert_immutable("AI_DG_RUN", []) == 0
    assert not spark.method_calls


def test_matching_is_immutable_and_repeated_content_is_accepted(monkeypatch):
    monkeypatch.setattr(repository, "_comparison_frame", lambda table, frame: frame)
    table = next(t for t in load_model()["tables"] if t["name"] == "AI_DG_DOCUMENT")
    record = row("AI_DG_DOCUMENT")
    # Epoch projection never materializes timestamps through Python's local zone.
    previous = repository._comparable(record)
    matching = Mock()
    matching.limit.return_value.collect.return_value = [SimpleNamespace(asDict=lambda **_: previous)]
    repository.DeltaRepository._check_matching(table, matching, [record])
    with pytest.raises(ValueError, match="different content"):
        repository.DeltaRepository._check_matching(table, matching, [{**record, "description_text": "changed"}])
    matching.limit.return_value.collect.return_value *= 2
    with pytest.raises(ValueError, match="duplicate"):
        repository.DeltaRepository._check_matching(table, matching, [record])


def prepared(monkeypatch):
    spark = Mock()
    spark.conf.get.return_value = "UTC"
    verify = Mock()
    monkeypatch.setattr(repository, "_verify_table", verify)
    monkeypatch.setattr(repository, "_spark_schema", lambda table: table["name"])
    monkeypatch.setattr(repository, "_comparison_frame", lambda table, frame: frame)
    committed = []
    spark.table.return_value.join.return_value.limit.return_value.collect.side_effect = lambda: [
        SimpleNamespace(asDict=lambda value=repository._comparable(record), **_: value) for record in committed]

    def commit(_sql):
        committed.extend(spark.createDataFrame.call_args.args[0])
        return Mock()

    spark.sql.side_effect = commit
    return spark, repository.DeltaRepository(spark, STORAGE), verify


@pytest.mark.parametrize('update', [False, True])
@pytest.mark.parametrize('expires', [False, True])
def test_dispatch_guard_runs_after_internal_reads_and_before_each_merge(monkeypatch, update, expires):
    spark, target, _ = prepared(monkeypatch)
    before = row('AI_DG_CASE')
    after = {**before, 'status': 'INVESTIGATING'}
    clock = [before['created_at']]
    deadline = clock[0] + timedelta(seconds=30)
    guards = []
    def admission(table_name):
        guards.append(table_name)
        if clock[0] >= deadline:
            raise PermissionError('Case authority expired before SQL dispatch')
    target.before_write = admission
    def delayed_read():
        clock[0] = deadline if expires else deadline-timedelta(seconds=1)
        records = [after] if spark.sql.called else ([before] if update else [])
        return [SimpleNamespace(asDict=lambda value=repository._comparable(record), **_: value)
            for record in records]
    spark.table.return_value.join.return_value.limit.return_value.collect.side_effect = delayed_read
    def write():
        return (target.update_current('AI_DG_CASE', [after], expected=[before]) if update
            else target.insert_immutable('AI_DG_CASE', [after]))
    if expires:
        with pytest.raises(PermissionError, match='expired'):
            write()
        spark.sql.assert_not_called()
    else:
        assert write() == 1
        assert spark.sql.call_count == 1
    assert guards == ['AI_DG_CASE']
    spark.catalog.dropTempView.assert_called_once()


def test_values_never_enter_sql_and_view_is_cleaned(monkeypatch):
    spark, target, verify = prepared(monkeypatch)
    record = row()
    record["manifest_json"] = "' ); DROP TABLE confidential; --\nΔ"
    assert target.insert_immutable("AI_DG_RUN", [record]) == 1
    assert spark.createDataFrame.call_args.args[0] == [record]
    sql = spark.sql.call_args.args[0]
    assert "MERGE INTO `oci_medallion`.`oci_artifacts`.`AI_DG_RUN`" in sql
    assert "WHEN MATCHED" not in sql
    assert record["manifest_json"] not in sql
    assert "WHEN NOT MATCHED THEN INSERT" in sql
    view = spark.createDataFrame.return_value.createOrReplaceTempView.call_args.args[0]
    spark.catalog.dropTempView.assert_called_once_with(view)
    verify.assert_called_once()


def test_failed_merge_cleans_view_and_propagates(monkeypatch):
    spark, target, _ = prepared(monkeypatch)
    spark.sql.side_effect = RuntimeError("write unavailable")
    with pytest.raises(RuntimeError, match="write unavailable"):
        target.insert_immutable("AI_DG_RUN", [row()])
    spark.catalog.dropTempView.assert_called_once()


def test_missing_readback_never_reports_success(monkeypatch):
    spark, target, _ = prepared(monkeypatch)
    spark.table.return_value.join.return_value.limit.return_value.collect.side_effect = lambda: []
    with pytest.raises(ValueError, match="could not be confirmed"):
        target.insert_immutable("AI_DG_RUN", [row()])
    assert spark.sql.call_count == 1


def test_storage_mismatch_or_non_utc_stops_before_data_frame(monkeypatch):
    spark, target, verify = prepared(monkeypatch)
    verify.side_effect = ValueError("storage differs")
    with pytest.raises(ValueError, match="storage differs"):
        target.insert_immutable("AI_DG_RUN", [row()])
    spark.createDataFrame.assert_not_called()
    spark.conf.get.return_value = "America/Lima"
    with pytest.raises(ValueError, match="UTC"):
        target.insert_immutable("AI_DG_RUN", [row()])
    spark.createDataFrame.assert_not_called()


def test_declared_candidate_key_conflict_is_rejected_in_batch():
    first = row("AI_DG_ASSET_VERSION")
    second = {**first, "version_id": "different"}
    with pytest.raises(ValueError, match="Duplicate"):
        repository.checked_rows("AI_DG_ASSET_VERSION", [first, second])


def test_candidate_key_collision_in_destination_stops_before_merge(monkeypatch):
    spark, target, _ = prepared(monkeypatch)
    record = row("AI_DG_ASSET_VERSION")
    existing = repository._comparable({**record, "version_id": "other-native-record"})
    # No PK match, but the declared (asset_id,metadata_hash) candidate key collides.
    spark.table.return_value.join.return_value.limit.return_value.collect.side_effect = [
        [], [SimpleNamespace(asDict=lambda **_: existing)]]
    with pytest.raises(ValueError, match="different content"):
        target.insert_immutable("AI_DG_ASSET_VERSION", [record])
    spark.sql.assert_not_called()


def test_epoch_comparison_is_independent_of_python_local_timezone(monkeypatch):
    monkeypatch.setattr(repository, "_comparison_frame", lambda table, frame: frame)
    table = next(t for t in load_model()["tables"] if t["name"] == "AI_DG_DOCUMENT")
    record = row("AI_DG_DOCUMENT")
    record["created_at"] = datetime(1970, 1, 1, 0, 30, 0, 123456,
                                    tzinfo=timezone(timedelta(hours=-5)))
    collected = {**record, "created_at": 19800123456}
    matching = Mock()
    matching.limit.return_value.collect.return_value = [SimpleNamespace(asDict=lambda **_: collected)]
    repository.DeltaRepository._check_matching(table, matching, [record], require_complete=True)


def test_timestamps_are_projected_in_spark_before_collect(monkeypatch):
    import sys

    functions = SimpleNamespace(expr=Mock(), col=Mock())
    monkeypatch.setitem(sys.modules, "pyspark", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "pyspark.sql", SimpleNamespace(functions=functions))
    table = next(t for t in load_model()["tables"] if t["name"] == "AI_DG_RUN")
    frame = Mock()
    repository._comparison_frame(table, frame)
    assert [c.args[0] for c in functions.expr.call_args_list] == [
        "unix_micros(`started_at`)", "unix_micros(`finished_at`)"]
    frame.select.assert_called_once()


def test_lookup_rejects_incomplete_duplicate_and_unknown_keys_before_spark():
    spark = Mock()
    target = repository.DeltaRepository(spark, STORAGE)
    for table, keys in [("AI_DG_RUN", [{}]), ("AI_DG_RUN", [{"run_id": None}]),
                        ("AI_MI_RUN", [{"run_id": "x"}]),
                        ("AI_DG_RUN", [{"run_id": "x"}, {"run_id": "x"}])]:
        with pytest.raises(ValueError):
            target.read_by_keys(table, keys)
    assert not spark.method_calls


def test_lookup_uses_typed_keys_and_restores_utc_timestamps(monkeypatch):
    spark, target, _ = prepared(monkeypatch)
    record = row()
    spark.table.return_value.join.return_value.limit.return_value.collect.side_effect = lambda: [
        SimpleNamespace(asDict=lambda **_: repository._comparable(record))]
    assert target.read_by_keys("AI_DG_RUN", [{"run_id": record["run_id"]}]) == [record]
    assert spark.createDataFrame.call_args.args[0] == [{"run_id": record["run_id"]}]
    spark.sql.assert_not_called()


def test_lookup_duplicate_destination_is_not_a_success(monkeypatch):
    spark, target, _ = prepared(monkeypatch)
    record = repository._comparable(row())
    spark.table.return_value.join.return_value.limit.return_value.collect.side_effect = lambda: [
        SimpleNamespace(asDict=lambda **_: record)] * 2
    with pytest.raises(ValueError, match="Duplicate"):
        target.read_by_keys("AI_DG_RUN", [{"run_id": "value"}])


def test_inventory_update_preserves_identity_and_requires_existing_snapshot(monkeypatch):
    spark, target, _ = prepared(monkeypatch)
    before = row("AI_DG_ASSET")
    after = {**before, "current_version_id": "new-version"}
    spark.table.return_value.join.return_value.limit.return_value.collect.side_effect = [
        [SimpleNamespace(asDict=lambda **_: repository._comparable(before))],
        [SimpleNamespace(asDict=lambda **_: repository._comparable(after))]]
    assert target.update_current("AI_DG_ASSET", [after], expected=[before]) == 1
    sql = spark.sql.call_args.args[0]
    assert "WHEN MATCHED THEN UPDATE" in sql and "WHEN NOT MATCHED" not in sql
    assert "t.`first_seen_at` =" not in sql and "t.`native_key` = s." not in sql
    assert "new-version" not in sql
    spark.catalog.dropTempView.assert_called_once()


def test_inventory_update_does_not_edit_immutable_records_or_identity(monkeypatch):
    spark, target, _ = prepared(monkeypatch)
    for table, before, after in [
        ("AI_DG_DOCUMENT", row("AI_DG_DOCUMENT"), row("AI_DG_DOCUMENT")),
        ("AI_DG_ASSET", row("AI_DG_ASSET"), {**row("AI_DG_ASSET"), "native_key": "different"}),
    ]:
        with pytest.raises(ValueError):
            target.update_current(table, [after], expected=[before])
    assert not spark.method_calls


def test_agent_projection_allows_only_deployment_version_update(monkeypatch):
    spark, target, _ = prepared(monkeypatch)
    before = row("AI_DG_AGENT")
    after = {**before, "deployment_version": "next-revision"}
    spark.table.return_value.join.return_value.limit.return_value.collect.side_effect = [
        [SimpleNamespace(asDict=lambda **_: repository._comparable(before))],
        [SimpleNamespace(asDict=lambda **_: repository._comparable(after))]]
    assert target.update_current("AI_DG_AGENT", [after], expected=[before]) == 1
    sql = spark.sql.call_args.args[0]
    assert "t.`deployment_version` = s.`deployment_version`" in sql
    assert "t.`native_agent_key` = s." not in sql
    for name, value in (("native_agent_key", "other"), ("enabled", not before["enabled"]),
                        ("agent_role", "other"), ("code_path", "other")):
        with pytest.raises(ValueError, match="immutable"):
            target.update_current("AI_DG_AGENT", [{**before, name: value}], expected=[before])


def test_changed_snapshot_blocks_inventory_update_and_missing_readback_fails(monkeypatch):
    spark, target, _ = prepared(monkeypatch)
    before = row("AI_DG_ASSET")
    after = {**before, "current_version_id": "new"}
    changed = {**before, "current_version_id": "external"}
    spark.table.return_value.join.return_value.limit.return_value.collect.side_effect = [
        [SimpleNamespace(asDict=lambda **_: repository._comparable(changed))]]
    with pytest.raises(ValueError, match="different content"):
        target.update_current("AI_DG_ASSET", [after], expected=[before])
    spark.sql.assert_not_called()
    spark.table.return_value.join.return_value.limit.return_value.collect.side_effect = [
        [SimpleNamespace(asDict=lambda **_: repository._comparable(before))], []]
    with pytest.raises(ValueError, match="could not be confirmed"):
        target.update_current("AI_DG_ASSET", [after], expected=[before])
    assert spark.sql.call_count == 1
    spark.catalog.dropTempView.assert_called_once()


def test_retry_recognizes_a_commit_whose_readback_was_lost(monkeypatch):
    spark, target, _ = prepared(monkeypatch)
    before = row("AI_DG_ASSET")
    after = {**before, "current_version_id": "committed-version"}
    persisted = repository._comparable(before)

    def readback():
        return [SimpleNamespace(asDict=lambda **_: dict(persisted))]

    def commit_then_disconnect(_sql):
        persisted.update(repository._comparable(after))
        raise TimeoutError("Commit response lost")

    spark.table.return_value.join.return_value.limit.return_value.collect.side_effect = readback
    spark.sql.side_effect = commit_then_disconnect
    with pytest.raises(TimeoutError):
        target.update_current("AI_DG_ASSET", [after], expected=[before])
    assert target.update_current("AI_DG_ASSET", [after], expected=[before]) == 1
    assert spark.sql.call_count == 1  # Reconciled; no second MERGE.
