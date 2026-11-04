from contextvars import ContextVar
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from app.ai_data_governance.caller_transport import CallerMetadataTransport, native_caller_transport


def transport():
    context = ContextVar("native_caller", default=None)
    context.set(SimpleNamespace(dh_user_principal="opaque-native-fixture", subject_id="caller-a", session_id="session-a"))
    session = MagicMock()
    response = session.request.return_value.__enter__.return_value
    response.status_code, response.headers = 200, {}
    response.iter_content.return_value = [b'{"items":[]}']
    config = {"region": "us-chicago-1", "platform_id": "ocid1.aidataplatform.oc1.us-chicago-1.test",
        "workspace_key": "d40e4c62-71d9-4837-8609-5479d950f6d5",
        "source_scopes": [{"catalog_key": "catalog", "schema_key": "catalog.schema"}]}
    signer = Mock()
    return CallerMetadataTransport(config, signer, session, get_context=context.get), context, session, signer


def test_scoped_page_uses_caller_signer_and_preserves_network_bounds():
    request, _, session, signer = transport()
    assert request("GET", "/tables", params={"catalogKey": "catalog", "schemaKey": "catalog.schema", "limit": "20"}) == ({"items": []}, {})
    options = session.request.call_args.kwargs
    assert options["auth"] is signer and options["allow_redirects"] is False
    assert options["timeout"] == (10, 60)
    assert options["headers"] == {"Accept": "application/json"}


@pytest.mark.parametrize("change", [None,
    SimpleNamespace(dh_user_principal="opaque-native-fixture", subject_id="caller-a", session_id=None),
    SimpleNamespace(dh_user_principal="opaque-native-fixture", subject_id="caller-a", session_id=" "),
    SimpleNamespace(dh_user_principal="opaque-native-fixture", subject_id="caller-a", session_id={}),
    SimpleNamespace(dh_user_principal="", subject_id="caller-a", session_id="session-a"),
    SimpleNamespace(dh_user_principal="other-principal", subject_id="caller-a", session_id="session-a"),
    SimpleNamespace(dh_user_principal="opaque-native-fixture", subject_id="caller-b", session_id="session-a"),
    SimpleNamespace(dh_user_principal="opaque-native-fixture", subject_id="caller-a", session_id="session-b")])
def test_missing_changed_or_resumed_identity_is_rejected_before_http(change):
    request, context, session, _ = transport()
    context.set(change)
    with pytest.raises(PermissionError):
        request("GET", "/tables/catalog.schema.table")
    session.request.assert_not_called()


@pytest.mark.parametrize("method,path,params", [
    ("GET", "/roles", None), ("GET", "/credentials", None),
    ("GET", "/workspaces/d40e4c62-71d9-4837-8609-5479d950f6d5/jobRuns/11111111-1111-1111-1111-111111111111", None),
    ("PUT", "/tables/catalog.schema.table", None),
    ("GET", "/tables/other.schema.table", None),
    ("GET", "/tables/catalog.schema.table%2Fcredentials", None),
    ("GET", "/tables/catalog.schema.table?redirect=elsewhere", None),
    ("GET", "/tables/catalog.schema.table", {"arbitrary": "query"}),
    ("GET", "/tables", None),
    ("GET", "/tables", {"catalogKey": "catalog", "schemaKey": "other.schema", "limit": "10"}),
    ("GET", "/tables", {"catalogKey": "catalog", "schemaKey": "catalog.schema", "limit": "101"}),
    ("GET", "/tables", {"catalogKey": "catalog", "schemaKey": "catalog.schema", "limit": "10", "user": "forged"}),
])
def test_consultation_cannot_escape_scopes_read_credentials_or_mutate(method, path, params):
    request, _, session, _ = transport()
    with pytest.raises(ValueError):
        request(method, path, params=params)
    session.request.assert_not_called()


def test_table_read_and_native_lineage_are_admitted_without_operator_credential():
    request, _, session, _ = transport()
    request("GET", "/tables/catalog.schema.table")
    request("POST", "/actions/fetchLineage", params={"limit": "100"}, payload={
        "anchorNode": "aidp://catalogs@ocid1.aidataplatform.oc1.us-chicago-1.test/o/catalog.schema.table",
        "direction": "BOTH", "maxDepth": 2, "level": "ENTITY", "shouldIncludeEdges": True})
    assert session.request.call_count == 2


def test_native_factory_uses_remote_signer_and_never_initializes_it_without_context(monkeypatch):
    request, context, _, _ = transport()
    factory = Mock()
    monkeypatch.setitem(sys.modules, "aidputils.agents.auth.signer.custom_remote_signer",
        SimpleNamespace(CustomRemoteSigner=factory))
    monkeypatch.setitem(sys.modules, "aidputils.agents.auth.util",
        SimpleNamespace(auth_utils=SimpleNamespace(get_auth_context=context.get)))
    config = {"region": "us-chicago-1", "platform_id": request.platform, "workspace_key": request.workspace,
        "source_scopes": [{"catalog_key": "catalog", "schema_key": "catalog.schema"}]}
    context.set(None)
    with pytest.raises(PermissionError):
        native_caller_transport(config)
    factory.assert_not_called()
    context.set(SimpleNamespace(dh_user_principal="native-fixture", subject_id="caller", session_id="session"))
    result = native_caller_transport(config)
    assert result.signer is factory.return_value
    factory.assert_called_once_with()
    assert result.session.auth is None
    assert not any(name.lower() == "authorization" for name in result.session.headers)
    result.session.close()


def test_explicit_source_read_uses_fixed_workspace_and_current_caller_signer():
    from urllib.parse import quote
    request, _, session, signer = transport()
    request.code_reads_enabled = True
    root = f'/workspaces/{request.workspace}'
    request('GET', root + '/jobs/11111111-1111-4111-8111-111111111111')
    response = session.request.return_value.__enter__.return_value
    response.iter_content.return_value = [b'spark.table("catalog.schema.table")']
    assert request('GET', root + '/objects/' + quote('/Workspace/job.py', safe=''))[0].startswith('spark.table')
    assert session.request.call_args.kwargs['auth'] is signer
    session.request.reset_mock()
    for path in (root + '/jobs', root + '/jobs/not-a-key', root + '/objects/%2FWorkspace%2F..%2Fprivate.py',
                 '/workspaces/other/jobs/11111111-1111-4111-8111-111111111111'):
        with pytest.raises(ValueError):
            request('GET', path)
    session.request.assert_not_called()


def test_current_job_discovery_is_opt_in_and_passes_the_exact_fixed_page_bound():
    request, _, session, signer = transport()
    path = f'/workspaces/{request.workspace}/jobs'
    with pytest.raises(ValueError):
        request('GET', path, params={'limit': '32'})
    session.request.assert_not_called()
    request.code_reads_enabled = True
    request('GET', path, params={'limit': '32'})
    assert session.request.call_args.kwargs['params'] == {'limit': '32'}
    assert session.request.call_args.kwargs['auth'] is signer
    session.request.reset_mock()
    for method, target, params in [('POST', path, {'limit':'32'}),
            ('GET', path, {'limit':'33'}), ('GET', path, {'limit':'32','page':'next'}),
            ('GET', '/workspaces/foreign/jobs', {'limit':'32'})]:
        with pytest.raises(ValueError):
            request(method, target, params=params)
    session.request.assert_not_called()


def test_source_reads_are_disabled_by_default_and_recheck_identity_after_http():
    from urllib.parse import quote
    request, context, session, _ = transport()
    path = f'/workspaces/{request.workspace}/objects/' + quote('/Workspace/job.py', safe='')
    with pytest.raises(ValueError):
        request('GET', path)
    session.request.assert_not_called()
    request.code_reads_enabled = True
    response = session.request.return_value.__enter__.return_value
    def body(**kwargs):
        context.set(SimpleNamespace(dh_user_principal='changed', subject_id='caller-a', session_id='session-a'))
        yield b'private source'
    response.iter_content.side_effect = body
    with pytest.raises(PermissionError):
        request('GET', path)
