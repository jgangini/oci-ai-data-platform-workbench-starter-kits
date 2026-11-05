import sys
from types import ModuleType

import pytest

from app.modules.ai_data_governance.runtime_source import inventory_task_source


def config():
    return {"region": "us-chicago-1", "platform_id": "ocid1.aidataplatform.oc1.us-chicago-1.test",
        "workspace_key": "d40e4c62-71d9-4837-8609-5479d950f6d5",
        "job_key": "6bd1af22-8dc8-4f1e-91ad-b9738a2ada5a", "credential_name": "runtime-reference",
        "identity_sha256": "a" * 64, "storage_root": "oci://approved_artifacts@testnamespace/oci_artifacts",
        "source_scopes": [{"catalog_key": "catalog", "schema_key": "catalog.schema"}]}


def test_readable_source_is_deterministic_and_imports_without_native_calls(monkeypatch):
    source = inventory_task_source(config())
    assert source == inventory_task_source(config())
    assert b"base64" not in source and b"exec(" not in source and b"from ." not in source
    module = ModuleType("ai_dg_generated_inventory_check")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    exec(compile(source, "sync_inventory.py", "exec"), module.__dict__)
    assert module._text({"description": "Existing text"}, ("description",)) == "Existing text"
    with pytest.raises(ValueError, match="redacted"):
        module._text({"description": "*********(redacted)"}, ("description",))
    assert module.CONFIG == config() and len(module.load_model()["tables"]) == 18
    assert callable(module.synchronize_scope) and callable(module.run_native_inventory)
    assert not hasattr(module, "legacy_column_ids") and callable(module.MetadataTransport)


@pytest.mark.parametrize("change", [
    lambda c: c.update(private_key="must-not-be-packaged"),
    lambda c: c.update(job_key="{{job.run_id}}"),
    lambda c: c.update(identity_sha256=""),
    lambda c: c.update(source_scopes=[{"catalog_key": "oci_medallion", "schema_key": "foreign.oci_artifacts"}]),
    lambda c: c.update(source_scopes=[*c["source_scopes"], *c["source_scopes"]]),
])
def test_source_requires_explicit_safe_bindings(change):
    values = config()
    change(values)
    with pytest.raises(ValueError):
        inventory_task_source(values)
