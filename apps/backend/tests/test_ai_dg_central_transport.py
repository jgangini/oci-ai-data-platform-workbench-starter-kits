from types import SimpleNamespace
from unittest.mock import Mock
import sys

import pytest

from app.ai_data_governance import caller_transport
from test_ai_dg_caller_transport import transport


def central_dependencies(monkeypatch):
    original, context, session, signer = transport()
    secret_get = Mock()
    monkeypatch.setitem(sys.modules, "aidputils", SimpleNamespace(secrets=SimpleNamespace(get=secret_get)))
    monkeypatch.setitem(sys.modules, "aidputils.agents.auth.util",
                        SimpleNamespace(auth_utils=SimpleNamespace(get_auth_context=context.get)))
    import requests
    monkeypatch.setattr(requests, "Session", lambda: session)
    factory = Mock(return_value=signer)
    monkeypatch.setattr(caller_transport, "runtime_signer", factory)
    config = {"region": "us-chicago-1", "platform_id": "ocid1.aidataplatform.oc1.us-chicago-1.test",
              "workspace_key": original.workspace,
              "source_scopes": [{"catalog_key": "catalog", "schema_key": "catalog.schema"}],
              "credential_name": "installed-reference", "identity_sha256": "a" * 64}
    return config, context, session, signer, factory, secret_get


def test_central_metadata_uses_existing_identity_and_keeps_session_isolation(monkeypatch):
    config, context, session, signer, factory, secret_get = central_dependencies(monkeypatch)
    central = caller_transport.native_central_transport(config)
    factory.assert_called_once_with(config, secret_get)
    central("GET", "/tables/catalog.schema.table")
    assert session.request.call_args.kwargs["auth"] is signer
    assert central.access_mode == "CENTRAL_METADATA" and central.code_reads_enabled
    central("GET", f"/workspaces/{central.workspace}/jobs/11111111-1111-4111-8111-111111111111")
    context.set(SimpleNamespace(dh_user_principal="other", subject_id="other", session_id="other"))
    previous = session.request.call_count
    with pytest.raises(PermissionError):
        central("GET", "/tables/catalog.schema.table")
    assert session.request.call_count == previous


def test_missing_technical_credential_never_falls_back_to_caller(monkeypatch):
    config, _, session, _, factory, _ = central_dependencies(monkeypatch)
    factory.side_effect = RuntimeError("fixture credential unavailable")
    with pytest.raises(RuntimeError, match="unavailable"):
        caller_transport.native_central_transport(config)
    session.request.assert_not_called()
