"""The deployed agent must work alone, without a project package or source loader."""
import ast
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "hooks"))
import territorial_bootstrap as bootstrap
from gods_eye_agent_source import agent_source


def configuration():
    return {"region": "us-chicago-1", "compartment_id": "test-compartment", "model_id": "test-model",
        "oci_credential_name": "AidpRuntime", "oci_identity_sha256": "a" * 64,
        "catalog": "oci_medallion", "gold_query_compute_id": "query-compute"}


def test_agent_is_portable_one_file_with_fixed_gold_queries(tmp_path, monkeypatch):
    source = agent_source(configuration(), bootstrap.runtime_archive())
    path = tmp_path / "ai_gods_eye_view.py"
    path.write_text(source, encoding="utf-8")
    process = subprocess.run([sys.executable, "-I", "-c",
        "import runpy,sys; a=runpy.run_path(sys.argv[1]); "
        "assert a['RUNTIME_CONFIG']['oci_credential_name']=='AidpRuntime'; "
        "assert a['parse_bbox']('-74.2,4.5,-74.1,4.7')==(-74.2,4.5,-74.1,4.7); "
        "assert a['GodsEyeViewAgent']().agent is None", str(path)],
        cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert process.returncode == 0, process.stderr
    tree = ast.parse(source)
    assert not any(isinstance(node, ast.ImportFrom) and (node.level or (node.module or "").startswith("territorial"))
                   for node in ast.walk(tree))
    assert "sys.path" not in source and "manifest.json" not in source and "PrismaReaderRuntime" not in source
    assert "oracledb" not in source
    namespace = {}
    exec(compile(source, str(path), "exec"), namespace)
    submitted = []

    class ToolConfig:
        def __init__(self, **values):
            self.values = values

        def model_dump(self):
            return self.values

    def tool(values):
        submitted.append(values)
        return SimpleNamespace(invoke=lambda args: {"rows": [{"payload": json.dumps({"id": "sensor-1", "value": 2.3})}]})

    monkeypatch.setitem(sys.modules, "aidputils.agents.toolkit.configs", SimpleNamespace(AIDPToolConf=ToolConfig))
    monkeypatch.setitem(sys.modules, "aidputils.agents.toolkit.tool_helper", SimpleNamespace(create_langgraph_tool=tool))
    assert namespace["gold_query"](configuration(), "SELECT payload FROM territorial_sensors WHERE publication_version=:version",
        {"version": "published' OR 1=1"}) == [{"id": "sensor-1", "value": 2.3}]
    request = submitted[0]["conf"]
    assert request["catalogType"] == "STANDARD" and request["clusterKey"] == "query-compute"
    assert "queryType" not in request and "sparkComputeKey" not in request
    assert "`oci_medallion`.`oci_gold`.`territorial_sensors`" in request["query"]
    assert "OR 1=1" not in request["query"]
    with pytest.raises(ValueError, match="fixed SELECT"):
        namespace["gold_query"](configuration(), "DELETE FROM territorial_sensors", {})
    assert len(submitted) == 1
