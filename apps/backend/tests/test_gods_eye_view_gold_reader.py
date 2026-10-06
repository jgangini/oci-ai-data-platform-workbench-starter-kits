import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.gods_eye_view.agent import incident_query, sensor_query
from app.gods_eye_view.gold_reader import query, statement
from test_gods_eye_view_agent import incident_database


@pytest.mark.parametrize("value", ["Bogotá", "O'Hara\\'; DROP TABLE t; --", "雪🌧️", "NULL", "{{other}}", ":version", ""])
def test_gold_statement_encodes_text_without_sql_interpolation(value):
    sql = statement("SELECT payload FROM gods_eye_view_incidents WHERE publication_version=:version", {"version": value}, "oci_medallion")
    assert sql == ("SELECT payload FROM `oci_medallion`.`oci_gold`.`territorial_incidents` WHERE publication_version="
        "decode(unhex('" + value.encode().hex() + "'),'UTF-8')")


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), {}, [], "x" * 201])
def test_gold_statement_rejects_invalid_values(value):
    with pytest.raises(ValueError, match="parameter"):
        statement("SELECT :value", {"value": value}, "oci_medallion")


@pytest.mark.parametrize("catalog", ["", "a.b", "a`", "a;DROP", None])
def test_gold_statement_rejects_catalog_injection(catalog):
    with pytest.raises(ValueError, match="catalog"):
        statement("SELECT :value", {"value": 1}, catalog)


def test_gold_statement_preserves_null_numbers_and_requires_exact_binds():
    assert statement("SELECT :a,:b,:c,:a", {"a": None, "b": -74.2, "c": 5}, "gold") == "SELECT NULL,-74.2,5,NULL"
    for sql, binds in (("SELECT :a", {}), ("SELECT 1", {"extra": 1}), ("DELETE FROM t", {}), ("SELECT 1; DROP TABLE t", {})):
        with pytest.raises(ValueError):
            statement(sql, binds, "gold")


def native(monkeypatch, result):
    tool = SimpleNamespace(invoke=MagicMock(return_value=result))
    configuration = MagicMock(side_effect=lambda **values: SimpleNamespace(model_dump=lambda: values))
    factory = MagicMock(return_value=tool)
    monkeypatch.setitem(sys.modules, "aidputils.agents.toolkit.configs", SimpleNamespace(AIDPToolConf=configuration))
    monkeypatch.setitem(sys.modules, "aidputils.agents.toolkit.tool_helper", SimpleNamespace(create_langgraph_tool=factory))
    return tool, factory


def test_gold_reader_uses_configured_spark_contract_and_preserves_full_payload(monkeypatch):
    row = {"id": "i", "correlation_context": {"unknown_future_field": [1, {"x": True}]}}
    tool, factory = native(monkeypatch, {"result": {"structuredContent": {"rows": [{"payload": json.dumps(row)}]}}})
    config = {"gold_query_compute_id": "existing-gold-compute", "catalog": "oci_medallion"}
    assert query(config, *incident_query("v1")) == [row]
    actual = factory.call_args.args[0]
    assert actual["tool_class"] == "SQLTool" and actual["params"] == []
    # The installed executor reads clusterKey; SDK queryType/sparkComputeKey are ignored here.
    assert actual["conf"] == {"catalogType": "STANDARD", "clusterKey": "existing-gold-compute",
        "catalogKey": "oci_medallion", "schemaKey": "oci_gold", "isRowLimitEnabled": True, "maxRows": 101,
        "query": actual["conf"]["query"]}
    assert "`oci_medallion`.`oci_gold`.`territorial_incidents`" in actual["conf"]["query"]
    assert "ADMIN" not in actual["conf"]["query"] and ":version" not in actual["conf"]["query"]
    tool.invoke.assert_called_once_with({})


@pytest.mark.parametrize("result", [
    {"error": "compute stopped"}, {"isError": True}, {"result": {"structuredContent": {"rows": [], "error": "failed"}}},
    {"result": {"metadata": {"error": "failed"}, "result": []}}, {}, [], {"result": {"rows": [1]}},
    {"metadata": {"error": "failed"}, "result": {"rows": []}},
    {"result": {"rows": [{"payload": "[]"}]}}, {"result": {"rows": [{"other": "{}"}]}},
    {"result": {"rows": [{"payload": "{}"}] * 101}},
])
def test_gold_reader_does_not_turn_failure_or_unknown_shape_into_absence(monkeypatch, result):
    tool, _ = native(monkeypatch, result)
    with pytest.raises(RuntimeError):
        query({"gold_query_compute_id": "query-compute", "catalog": "oci_medallion"}, *sensor_query("v1"))
    assert tool.invoke.call_count == 1


def test_gold_reader_empty_rows_are_valid_and_missing_compute_never_invokes(monkeypatch):
    tool, factory = native(monkeypatch, json.dumps({"result": {"rows": []}}))
    config = {"gold_query_compute_id": "query-compute", "catalog": "oci_medallion"}
    assert query(config, *sensor_query("v1")) == []
    factory.reset_mock()
    with pytest.raises(RuntimeError, match="compute"):
        query({"catalog": "oci_medallion"}, *sensor_query("v1"))
    factory.assert_not_called()
    assert tool.invoke.call_count == 1


@pytest.mark.skipif(os.getenv("TERRITORIAL_SPARK_INTEGRATION") != "1", reason="Requires the existing local Spark test image")
def test_agent_queries_execute_on_real_spark_with_versioned_gold_and_literal_parameters(monkeypatch, incident_database):
    import ast
    import inspect
    from pyspark.sql import SparkSession
    from app.gods_eye_view import agent

    spark = SparkSession.builder.master("local[2]").appName("territorial-gold-query-test").config("spark.ui.enabled", "false").getOrCreate()
    try:
        for kind in ("incidents", "evidence"):
            spark.createDataFrame(incident_database.execute(f"SELECT * FROM territorial_{kind}").fetchall(),
                "publication_version string, id string, payload string").createOrReplaceTempView(f"territorial_{kind}")
        relations = [{"event_id": "different-publications", "post_key": "different-publications-0", "relation": "duplicate",
            "claim_relation": value, "duplicate_of": "original", "new_field": {"preserved": True}}
            for value in ("supports", "contradicts")]
        spark.createDataFrame([("v1", row["event_id"], row["post_key"], json.dumps(row)) for row in relations],
            "publication_version string, event_id string, post_key string, payload string").createOrReplaceTempView("gods_eye_view_event_posts")
        sensor = {"id": "reading-1", "sensor_id": "station-1", "sensor_type": "rainfall", "locality": "Bosa", "lat": 4.6,
            "lon": -74.15, "observed_at": "2026-10-05T09:00:00-05:00", "value": 12.5, "unit": "mm/h", "mode": "Synthetic"}
        spark.createDataFrame([("v1", sensor["id"], json.dumps(sensor)), ("v2", sensor["id"], json.dumps({**sensor, "value": 99}))],
            "publication_version string, id string, payload string").createOrReplaceTempView("gods_eye_view_sensors")
        for filters, expected in (({"country": "Colombia", "severity": "high"}, {"co-high", "different-publications"}),
                                 ({"country": "Colombia", "city": "Lima"}, set()),
                                 ({"country": "Colombia' OR 1=1 --"}, set())):
            sql, binds = incident_query("v1", **filters)
            assert {json.loads(row[0])["id"] for row in spark.sql(sql, args=binds).collect()} == expected
        # Execute the production tool body with real Spark; its surrounding LLM/OCI setup is unrelated.
        module = ast.parse(inspect.getsource(agent))
        agent_class = next(node for node in module.body if isinstance(node, ast.ClassDef) and node.name == "GodsEyeViewAgent")
        setup = next(node for node in agent_class.body if isinstance(node, ast.FunctionDef) and node.name == "setup")
        evidence_node = next(node for node in setup.body if isinstance(node, ast.FunctionDef) and node.name == "consultar_evidencia")
        evidence_node.decorator_list = []
        namespace = {"query": lambda sql, binds: [json.loads(row[0]) for row in spark.sql(sql, args=binds).collect()]}
        exec(compile(ast.Module(body=[evidence_node], type_ignores=[]), "<production-evidence-tool>", "exec"), namespace)
        evidence = namespace["consultar_evidencia"]
        found = evidence("v1", incident_id="different-publications")
        assert [row["id"] for row in found] == ["different-publications-1", "different-publications-0"]
        assert {json.dumps(row, sort_keys=True) for row in found[1]["incident_relations"]} == {json.dumps(row, sort_keys=True) for row in relations}
        assert evidence("v2", incident_id="co-high") == []
        sql, binds = sensor_query("v1", sensor_id="station-1", sensor_type="rainfall", locality="Bosa", bbox="-74.2,4.5,-74.1,4.7",
            date_from="2026-10-05T14:00:00Z", date_to="2026-10-05T14:00:00Z")
        assert [json.loads(row[0]) for row in spark.sql(sql, args=binds).collect()] == [sensor]
        value = "Bogotá \\'; SELECT 1 -- 🌧️"
        assert spark.sql(statement("SELECT :value", {"value": value}, "oci_medallion")).first()[0] == value
    finally:
        spark.stop()
