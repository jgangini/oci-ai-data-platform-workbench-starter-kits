from copy import deepcopy
import ast
import sys
from types import ModuleType

import pytest

from app.ai_data_governance.documentary_source import documentary_agent_source, documentary_entry_source
from app.ai_data_governance.inventory import canonical
from app.ai_data_governance.prompts import documentary_prompts


def binding():
    return {"region": "us-chicago-1", "platform_id": "ocid1.aidataplatform.oc1.us-chicago-1.test",
        "workspace_key": "d40e4c62-71d9-4837-8609-5479d950f6d5", "credential_name": "ExistingRuntime",
        "identity_sha256": "a" * 64, "storage_root": "oci://artifacts@namespace/oci_artifacts",
        "job_key": "b40e4c62-71d9-4837-8609-5479d950f6d5",
        "source_scopes": [{"catalog_key": "catalog", "schema_key": "catalog.schema"}],
        "model_id": "ocid1.generativeaimodel.oc1.us-chicago-1.existing", "compartment_id": "ocid1.compartment.oc1..existing"}


def test_standalone_source_loads_without_native_or_model_calls_and_keeps_canonical_prompts(monkeypatch):
    value = binding()
    original = deepcopy(value)
    source = documentary_agent_source(value)
    module = ModuleType("ai_dg_documentation_test")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    namespace = module.__dict__
    exec(compile(source, "documentation_agent.py", "exec"), namespace)
    assert value == original
    assert namespace["PROMPTS"] == documentary_prompts()
    assert namespace["canonical"]({"value": "safe"}) == canonical({"value": "safe"})
    agent = namespace["DataGovernanceDocumentationAgent"]()
    assert agent.llm is agent.memory is agent.signer is None
    assert agent.binding["credential_name"] == "ExistingRuntime"
    assert b"from ." not in source
    assert b"base64" not in source and b"sys.path" not in source
    assert len(agent.binding["component_revision"]) == 64
    entry_classes = [node for node in ast.parse(source).body if isinstance(node, ast.ClassDef)
        and {"setup", "invoke"}.issubset({method.name for method in node.body if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef))})]
    assert [entry.name for entry in entry_classes] == ["DataGovernanceDocumentationAgent"]


def test_standalone_source_supports_exec_without_module_registration():
    namespace = {"__name__": "ai_dg_unregistered_documentary"}
    assert namespace["__name__"] not in sys.modules
    exec(compile(documentary_agent_source(binding()), "documentation_agent.py", "exec"), namespace)
    for name in ("DocumentationEnvelope", "DocumentationResult", "DocumentReviews"):
        assert namespace[name].model_json_schema()["type"] == "object"
    agent = namespace["DataGovernanceDocumentationAgent"]()
    assert agent.signer is agent.memory is agent.llm is None


def test_assembled_documentary_runtime_executes_cross_run_work_reservations():
    from test_ai_dg_model_recovery import VersionedObjects, owner

    namespace = {"__name__": "ai_dg_assembled_reservation"}
    exec(compile(documentary_agent_source(binding()), "documentary_runtime.py", "exec"), namespace)
    store = namespace["GovernanceJournal"](VersionedObjects(), platform_key=binding()["platform_id"],
        workspace_key=binding()["workspace_key"], storage_root=binding()["storage_root"])
    key = namespace["identity"]("model-work", "document", "scope", "work")
    assert store.reserve_model_work(key, owner("a" * 64))
    assert not store.reserve_model_work(key, owner("b" * 64))


def test_assembled_runtime_reuses_generation_without_missing_helpers_or_a_second_call():
    import asyncio
    import json
    from unittest.mock import AsyncMock
    from test_ai_dg_documentary_hooks import admission
    from test_ai_dg_generation_reuse import generated
    from test_ai_dg_model_recovery import make_journal

    async def check():
        namespace = {"__name__": "ai_dg_assembled_generation_reuse"}
        exec(compile(documentary_agent_source(binding()), "documentary_runtime.py", "exec"), namespace)
        store, provider = make_journal(), AsyncMock(side_effect=generated)
        for index, batch in enumerate(("a" * 64, "b" * 64)):
            row = {**admission(), "run_id": "producer-" + str(index)}
            contexts = {row["work_id"]: "same verified context"}
            store.create("batches", batch, {"schema_version": 1,
                "batch": {"batch_ref": batch, "admissions": [row], "contexts": contexts},
                "execution": {"job_run_id": str(index)}})
            hooks = namespace["JournalDocumentaryHooks"](store, call_model=provider, authorize=AsyncMock())
            await hooks.load_batch(batch, {})
            key = namespace["documentation_generation_key"](namespace["_admission"](row))
            assert await hooks.claim_generation("document", row, key, {})
            response = await hooks.generate({"batch_ref": batch, "admissions": [row], "contexts": contexts},
                namespace["DocumentationResult"].model_json_schema(), {})
            assert json.loads(response)["batch_ref"] == batch
        assert provider.await_count == 1
    asyncio.run(check())


def test_native_entry_has_only_its_agent_class_and_delegates_setup_and_invocation(monkeypatch):
    import asyncio
    from unittest.mock import Mock, AsyncMock
    runtime = Mock()
    runtime.invoke = AsyncMock(return_value={"messages": ["native-result"]})
    module = ModuleType("documentary_runtime")
    module.DataGovernanceDocumentationAgent = Mock(return_value=runtime)
    helper = ModuleType("aidputils.agents.toolkit.agent_helper")
    helper.pre_invoke_setup = Mock(return_value={"configurable": {"thread_id": "native-session"}})
    monkeypatch.setitem(sys.modules, "documentary_runtime", module)
    monkeypatch.setitem(sys.modules, "aidputils.agents.toolkit.agent_helper", helper)
    source = documentary_entry_source()
    classes = [n.name for n in ast.parse(source).body if isinstance(n, ast.ClassDef)]
    assert classes == ["DataGovernanceDocumentationAgent"]
    namespace = {"__name__": "ai_dg_native_entry_test"}
    exec(compile(source, "documentation_agent.py", "exec"), namespace)
    entry = namespace[classes[0]]()
    entry.setup()
    result = asyncio.run(entry.invoke("native-run:0", thread_id="native-session"))
    runtime.setup.assert_called_once_with()
    runtime.invoke.assert_awaited_once_with("native-run:0", thread_id="native-session")
    helper.pre_invoke_setup.assert_called_once_with(thread_id="native-session")
    assert result == {"messages": ["native-result"]}


def test_entry_imports_the_generated_sibling_implementation_from_its_folder(tmp_path, monkeypatch):
    import importlib.util
    from unittest.mock import Mock

    helper = ModuleType("aidputils.agents.toolkit.agent_helper")
    helper.pre_invoke_setup = Mock()
    monkeypatch.setitem(sys.modules, "aidputils.agents.toolkit.agent_helper", helper)
    monkeypatch.delitem(sys.modules, "documentary_runtime", raising=False)
    monkeypatch.syspath_prepend(str(tmp_path))
    (tmp_path / "documentary_runtime.py").write_bytes(documentary_agent_source(binding()))
    path = tmp_path / "documentation_agent.py"
    path.write_bytes(documentary_entry_source())
    spec = importlib.util.spec_from_file_location("ai_dg_folder_entry_test", path)
    entry_module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(entry_module)
        entry = entry_module.DataGovernanceDocumentationAgent()
        assert entry.runtime.__class__.__module__ == "documentary_runtime"
        assert entry.runtime.binding["credential_name"] == "ExistingRuntime"
        assert entry.runtime.signer is entry.runtime.memory is entry.runtime.llm is None
    finally:
        sys.modules.pop("documentary_runtime", None)


@pytest.mark.parametrize("changes", [{"private_key": "never"}, {"model_id": "https://untrusted/model"},
    {"compartment_id": "other"}, {"identity_sha256": ""}])
def test_unbound_models_credentials_and_extra_configuration_are_rejected(changes):
    with pytest.raises(ValueError):
        documentary_agent_source({**binding(), **changes})
