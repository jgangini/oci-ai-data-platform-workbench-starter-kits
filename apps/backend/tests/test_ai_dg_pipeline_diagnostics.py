import json
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from app.ai_data_governance.native_pipeline import pipeline_failure_report
from app.ai_data_governance.pipeline_source import pipeline_task_source
from test_ai_dg_workflow import binding
from test_ai_dg_native_documentary import RUN, TASK


def test_diagnostics_keep_java_identifiers_without_messages_or_local_data():
    java = Mock()
    java.getClass.return_value.getName.return_value = "org.apache.spark.SparkException"
    java.getErrorClass.return_value = "DELTA_CONCURRENT_APPEND"
    error = RuntimeError("private query and credential value")
    error.java_exception = java
    report = pipeline_failure_report("sync_inventory", error)
    assert report == {"phase": "sync_inventory", "status": "FAILED", "error_type": "RuntimeError",
        "origin": [], "java_error_type": "org.apache.spark.SparkException", "spark_error_class": "DELTA_CONCURRENT_APPEND"}
    assert "private query" not in json.dumps(report)
    java.toString.assert_not_called()
    java.getMessage.assert_not_called()


@pytest.mark.parametrize("bad", ["private query with spaces", "A" * 201, None])
def test_invalid_diagnostic_identifiers_are_omitted(bad):
    error = RuntimeError("private text")
    error.java_exception = Mock()
    error.java_exception.getClass.return_value.getName.return_value = bad
    error.java_exception.getErrorClass.return_value = bad
    report = pipeline_failure_report("publish_metadata", error)
    assert set(report) == {"phase", "status", "error_type", "origin"}


def test_failed_java_diagnostic_does_not_replace_original_error_type():
    error = ValueError("private text")
    error.java_exception = Mock()
    error.java_exception.getClass.side_effect = TimeoutError("private transport")
    assert pipeline_failure_report("replicate_model", error)["error_type"] == "ValueError"


@pytest.mark.parametrize("phase", ["sync_inventory", "publish_metadata", "replicate_model"])
def test_assembled_entry_emits_safe_origin_on_failure(phase, monkeypatch, capsys):
    source = pipeline_task_source(binding(), phase).decode()
    body, footer = source.rsplit('if __name__ == "__main__":', 1)
    module = ModuleType("diagnostic_runtime")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    exec(compile(body, phase + ".py", "exec"), module.__dict__)
    module.__dict__.update(__name__="__main__", aidputils=SimpleNamespace(secrets=SimpleNamespace(get=Mock())),
        run_native_pipeline=Mock(side_effect=ValueError("secret input SQL https://private.example/token")))
    monkeypatch.setattr(sys, "argv", [phase + ".py", "--job-run-id", RUN, "--task-run-id", TASK,
        "--task-key", phase, "--execution-count", "1", "--repair-count", "0"])
    with pytest.raises(RuntimeError, match="Governance task failed"):
        exec(compile('if __name__ == "__main__":' + footer, phase + ".py", "exec"), module.__dict__)
    raw = capsys.readouterr().out
    report = json.loads(raw)
    assert report["phase"] == phase and report["error_type"] == "ValueError"
    assert report["origin"] and report["origin"][-1]["function"] == "<module>"
    assert all(set(frame) == {"function", "line"} for frame in report["origin"])
    assert not any(value in raw for value in ("secret input", "private.example", "token", "Mock"))
