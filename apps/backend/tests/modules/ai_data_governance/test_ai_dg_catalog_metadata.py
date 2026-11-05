import pytest

from app.modules.ai_data_governance.catalog_metadata import initialize_table_descriptions
from app.modules.ai_data_governance.schema import descriptions


class Transport:
    def __init__(self):
        self.description = {}
        self.calls = []
        self.etag = '"version-1"'

    def __call__(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        key = path.removeprefix("/tables/")
        if method == "PUT":
            assert "headers" not in kwargs
            assert set(kwargs["payload"]) == {"description"}
            self.description[key] = kwargs["payload"]["description"]
        return {"key": key, "description": self.description.get(key)}, {"ETag": self.etag}


def initialize(transport):
    return initialize_table_descriptions(transport)


def test_only_module_control_descriptions_are_initialized_and_rechecked():
    transport = Transport()
    result = initialize(transport)
    assert set(result["verified"]) == set(descriptions())
    assert result["preserved"] == []
    assert sum(method == "PUT" for method, _, _ in transport.calls) == 18
    assert all(path.startswith("/tables/oci_medallion.oci_artifacts.ai_dg_") for _, path, _ in transport.calls)
    assert all("tableFields" not in kwargs.get("payload", {}) for _, _, kwargs in transport.calls)


def test_completed_initialization_is_read_only_on_retry():
    transport = Transport()
    initialize(transport)
    transport.calls.clear()
    assert len(initialize(transport)["verified"]) == 18
    assert all(method == "GET" for method, _, _ in transport.calls)


def test_existing_human_description_is_preserved():
    transport = Transport()
    key = "oci_medallion.oci_artifacts.ai_dg_scope"
    transport.description[key] = "Texto existente"
    assert initialize(transport)["preserved"] == ["AI_DG_SCOPE"]
    assert not any(method == "PUT" and path.endswith("ai_dg_scope") for method, path, _ in transport.calls)
    assert transport.description[key] == "Texto existente"


def test_missing_etag_does_not_change_blank_only_policy():
    transport = Transport()
    transport.etag = None
    assert len(initialize(transport)["verified"]) == 18
    assert sum(method == "PUT" for method, _, _ in transport.calls) == 18


def test_wrong_response_identity_cannot_trigger_a_write():
    with pytest.raises(ValueError, match="identity"):
        initialize(lambda *a, **k: ({"key": "another-table"}, {"etag": "v1"}))


def test_write_acknowledgment_without_verified_description_is_not_success():
    class IgnoredWrite(Transport):
        def __call__(self, method, path, **kwargs):
            if method == "PUT":
                return {}, {}
            return super().__call__(method, path, **kwargs)
    with pytest.raises(ValueError, match="not verified"):
        initialize(IgnoredWrite())


def test_existing_ia_description_is_preserved_without_etag():
    transport = Transport()
    transport.etag = None
    key = "oci_medallion.oci_artifacts.ai_dg_scope"
    transport.description[key] = "Descripción anterior [IA]"
    assert initialize(transport)["preserved"] == ["AI_DG_SCOPE"]
    assert not any(method == "PUT" and path.endswith("ai_dg_scope") for method, path, _ in transport.calls)
