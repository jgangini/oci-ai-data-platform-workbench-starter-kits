"""Bounded definer-package contract for deleting simulated sensor readings."""
from types import SimpleNamespace

import pytest

from app.territorial import database, sensors


def test_sensor_reset_capability_is_independent_from_social_reset_and_does_not_commit():
    calls = []
    def callfunc(name, result_type, values):
        calls.append((name, result_type, values))
        return 1
    connection = SimpleNamespace(cursor=lambda: SimpleNamespace(callfunc=callfunc))
    assert database.sensor_reset_version(connection) == 1
    assert calls == [("ADMIN.PRISMA_CONTROL.SENSOR_RESET_VERSION", int, [])]
    assert "FUNCTION SENSOR_RESET_VERSION RETURN NUMBER;" in database.PACKAGE_SPEC
    assert "FUNCTION SENSOR_RESET_VERSION RETURN NUMBER IS BEGIN RETURN 2; END;" in database.PACKAGE_BODY
    assert "FUNCTION RESET_VERSION RETURN NUMBER IS BEGIN RETURN 2; END;" in database.PACKAGE_BODY


@pytest.mark.parametrize("sensor_type", (*sensors.SENSOR_TYPES, "all"))
def test_sensor_publication_replacement_binds_exact_scope_and_commits_after_success(sensor_type):
    calls = []
    connection = SimpleNamespace(cursor=lambda: SimpleNamespace(callproc=lambda name, values: calls.append((name, values))),
                                 commit=lambda: calls.append("commit"))
    database.replace_sensor_publication(connection, "operation-1", sensor_type, "old-version", "clean-version")
    assert calls == [("ADMIN.PRISMA_CONTROL.REPLACE_SENSOR_PUBLICATION",
                      ["operation-1", sensor_type, "old-version", "clean-version"]), "commit"]


@pytest.mark.parametrize("sensor_type", [None, "", "Rainfall", "rainfall' OR 1=1", True, ["rainfall"]])
def test_invalid_sensor_reset_type_cannot_reach_database(sensor_type):
    with pytest.raises(ValueError, match="Invalid sensor reset type"):
        database.replace_sensor_publication(object(), "operation-1", sensor_type, "old", "new")


def test_database_rejection_does_not_commit_replacement():
    calls = []
    def rejected(_name, _values):
        raise RuntimeError("Sensor reset is not active")
    connection = SimpleNamespace(cursor=lambda: SimpleNamespace(callproc=rejected), commit=lambda: calls.append("commit"))
    with pytest.raises(RuntimeError, match="Sensor reset is not active"):
        database.replace_sensor_publication(connection, "stale-operation", "rainfall", "old", "new")
    assert calls == []


def test_sensor_sql_rejects_unscoped_commands_and_unproven_readings_before_deleting():
    body = database.PACKAGE_BODY
    social_guard = body.split("PROCEDURE require_reset", 1)[1].split("FUNCTION PURGE_SYNTHETIC_POSTS", 1)[0]
    assert "JSON_VALUE(payload,'$.sensor_type') IS NULL" in social_guard
    procedure = body.split("PROCEDURE REPLACE_SENSOR_PUBLICATION", 1)[1].split("PROCEDURE valid_name", 1)[0]
    for predicate in ("name='checkpoint_reset'", "JSON_VALUE(payload,'$.operation_id')=p_operation_id",
                      "JSON_VALUE(payload,'$.sensor_type')=p_sensor_type", "JSON_VALUE(payload,'$.status')='pending'",
                      "JSON_VALUE(payload,'$.ready')='true'", "p_sensor_type IS NULL"):
        assert predicate in procedure
    assert all(f"'{kind}'" in procedure for kind in sensors.SENSOR_TYPES)
    assert '(@.mode == "Synthetic" || @.mode == "simulation") && @.is_simulated == true' in procedure
    assert procedure.index("Sensor reset is not active") < procedure.index("Clean sensor replacement publication is missing")
    assert procedure.index("Clean sensor replacement publication is missing") < procedure.index("DELETE FROM")
    assert "WHERE version=p_new AND version!=p_old" in procedure
    assert "AND NOT JSON_EXISTS(payload,'$.sensors[*]?(($kind == \"all\" || @.sensor_type == $kind) &&" in procedure
    deletion = procedure.split("DELETE FROM", 1)[1]
    assert "ADMIN.PRISMA_PUBLICATIONS WHERE version=p_old" in deletion
    assert '($kind == "all" || @.sensor_type == $kind)' in deletion
    assert all(f'@.sensor_type == "{kind}"' in deletion for kind in sensors.SENSOR_TYPES)
    assert '(@.mode == "Synthetic" || @.mode == "simulation") && @.is_simulated == true' in deletion
    assert 'PASSING p_sensor_type AS "kind" ERROR ON ERROR' in deletion
    assert "PRISMA_SOCIAL_POSTS" not in procedure and "$.evidence" not in deletion and "$.incidents" not in deletion
    assert "TRUNCATE" not in procedure and "DROP" not in procedure
