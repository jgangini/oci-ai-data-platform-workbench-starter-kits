from copy import deepcopy

import pytest

from app.modules.ai_data_governance.catalog_metadata import publish_generated_description


KEY = "catalog.schema.table"
TEXT = "Contenido sustentado por el workflow. [IA]"


class NativeTable:
    def __init__(self, description=None):
        self.detail = {"key": KEY, "description": description, "tableFields": [
            {"fieldName": "code", "fieldType": "STRING", "fieldDescription": description},
            {"fieldName": "protected", "fieldType": "INT", "fieldDescription": "Texto humano"},
        ]}
        self.calls = []
        self.put_error = False
        self.readback_error = False
        self.after_write = None

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, deepcopy(kwargs)))
        if method == "GET":
            if self.readback_error and any(call[0] == "PUT" for call in self.calls):
                raise RuntimeError("private-token-must-not-escape")
            return deepcopy(self.detail), {}
        assert method == "PUT"
        payload = kwargs["payload"]
        if "description" in payload:
            self.detail["description"] = payload["description"]
        else:
            update, = payload["updateTableFields"]
            next(f for f in self.detail["tableFields"] if f["fieldName"] == update["fieldName"])["fieldDescription"] = update["fieldDescription"]
        if self.after_write:
            self.after_write(self.detail)
        if self.put_error:
            raise TimeoutError("private-token-must-not-escape")
        return {}, {}


@pytest.mark.parametrize("existing", ["Texto humano", "Texto anterior [IA]", "[IA]",
                                      " Texto [IA]   ", "Texto [ai]", "Texto [AI]", "0"])
@pytest.mark.parametrize("column", [None, "code"])
def test_every_nonempty_description_is_preserved(existing, column):
    native = NativeTable(existing)
    before = deepcopy(native.detail)
    outcome = publish_generated_description(native.request, KEY, TEXT, column_name=column)
    assert outcome["status"] == "preserved" and not outcome["write_attempted"]
    assert [call[0] for call in native.calls] == ["GET"]
    assert native.detail == before


@pytest.mark.parametrize("empty", [None, "", " \t\n"])
def test_only_blank_can_be_written_without_etag_then_retry_preserves(empty):
    native = NativeTable(empty)
    result = publish_generated_description(native.request, KEY, TEXT)
    assert result == {"status": "verified", "write_attempted": True,
                      "write_acknowledged": True, "observed_description": TEXT}
    assert [call[0] for call in native.calls] == ["GET", "PUT", "GET"]
    assert native.calls[1][2] == {"payload": {"description": TEXT}}
    result = publish_generated_description(native.request, KEY, TEXT)
    assert result["status"] == "preserved"
    assert sum(call[0] == "PUT" for call in native.calls) == 1


def test_column_update_never_replaces_schema_or_other_description():
    native = NativeTable()
    before = deepcopy(native.detail)
    result = publish_generated_description(native.request, KEY, TEXT, column_name="code")
    assert result["status"] == "verified"
    assert native.calls[1][2] == {"payload": {"updateTableFields": [
        {"fieldName": "code", "fieldDescription": TEXT}]}}
    assert native.detail["description"] == before["description"]
    assert native.detail["tableFields"][1] == before["tableFields"][1]
    assert native.detail["tableFields"][0]["fieldType"] == "STRING"


@pytest.mark.parametrize("observed", [None, "new-column-key"])
def test_recreated_column_cannot_receive_documentation_from_an_old_native_key(observed):
    native = NativeTable()
    if observed is not None:
        native.detail["tableFields"][0]["key"] = observed
    with pytest.raises(ValueError, match="identity changed"):
        publish_generated_description(native.request, KEY, TEXT, column_name="code", expected_column_key="old-column-key")
    assert [call[0] for call in native.calls] == ["GET"]


def test_matching_contemporary_column_key_permits_the_same_partial_update():
    native = NativeTable()
    native.detail["tableFields"][0]["fieldKey"] = "native-column-key"
    assert publish_generated_description(native.request, KEY, TEXT, column_name="code",
        expected_column_key="native-column-key")["status"] == "verified"
    assert native.calls[1][2] == {"payload": {"updateTableFields": [
        {"fieldName": "code", "fieldDescription": TEXT}]}}


def test_uncertain_committed_write_is_reconciled_without_another_put():
    native = NativeTable()
    native.put_error = True
    result = publish_generated_description(native.request, KEY, TEXT)
    assert result["status"] == "verified" and not result["write_acknowledged"]
    assert sum(call[0] == "PUT" for call in native.calls) == 1


def test_missing_readback_stays_unknown_without_exposing_transport_error():
    native = NativeTable()
    native.put_error = native.readback_error = True
    result = publish_generated_description(native.request, KEY, TEXT)
    assert result["status"] == "unknown" and result["observed_description"] is None
    assert "private-token" not in str(result)
    assert sum(call[0] == "PUT" for call in native.calls) == 1


@pytest.mark.parametrize("acknowledged", [False, True])
def test_still_blank_readback_is_unknown_not_a_confirmed_conflict(acknowledged):
    native = NativeTable()
    base = native.request

    def delayed(method, path, **kwargs):
        if method == "PUT":
            native.calls.append((method, path, kwargs))
            if not acknowledged:
                raise TimeoutError("write may still be running")
            return {}, {}
        return base(method, path, **kwargs)

    result = publish_generated_description(delayed, KEY, TEXT)
    assert result["status"] == "unknown"
    assert result["write_acknowledged"] is acknowledged
    assert sum(call[0] == "PUT" for call in native.calls) == 1


def test_changed_readback_is_conflict_and_is_not_overwritten():
    native = NativeTable()
    native.after_write = lambda detail: detail.update(description="Edición externa posterior")
    result = publish_generated_description(native.request, KEY, TEXT)
    assert result["status"] == "conflict"
    assert native.detail["description"] == "Edición externa posterior"
    assert sum(call[0] == "PUT" for call in native.calls) == 1


@pytest.mark.parametrize("change,column", [
    (lambda d: d.update(key="outside"), None),
    (lambda d: d.pop("description"), None),
    (lambda d: d.update(description=12), None),
    (lambda d: d.pop("tableFields"), "code"),
    (lambda d: d["tableFields"].append(dict(d["tableFields"][0])), "code"),
    (lambda d: d["tableFields"][0].pop("fieldDescription"), "code"),
])
def test_unknown_identity_or_coverage_is_not_a_blank_field(change, column):
    native = NativeTable()
    change(native.detail)
    with pytest.raises(ValueError):
        publish_generated_description(native.request, KEY, TEXT, column_name=column)
    assert all(call[0] == "GET" for call in native.calls)


@pytest.mark.parametrize("text", ["", " [IA]", "Texto [ai]", "Texto [AI]", "Texto sin marca", "Texto [IA] [IA]"])
def test_generated_text_must_have_one_exact_final_ia_marker(text):
    native = NativeTable()
    with pytest.raises(ValueError, match="marker"):
        publish_generated_description(native.request, KEY, text)
    assert native.calls == []


@pytest.mark.parametrize("column", [None, "code"])
def test_redacted_description_is_unavailable_and_never_overwritten(column):
    native = NativeTable("*********(redacted)")
    with pytest.raises(ValueError, match="redacted"):
        publish_generated_description(native.request, KEY, TEXT, column_name=column)
    assert [c[0] for c in native.calls] == ["GET"]


def test_redacted_readback_is_unknown_not_a_confirmed_external_edit():
    native = NativeTable()
    native.after_write = lambda detail: detail.update(description="*********(redacted)")
    result = publish_generated_description(native.request, KEY, TEXT)
    assert result["status"] == "unknown" and result["observed_description"] is None
    assert sum(c[0] == "PUT" for c in native.calls) == 1
