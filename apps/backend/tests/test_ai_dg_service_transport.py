import hashlib
import json
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from app.ai_data_governance.service_transport import MetadataTransport, runtime_signer, runtime_object_storage


def config():
    return {"region": "us-chicago-1", "platform_id": "ocid1.aidataplatform.oc1.us-chicago-1.test",
            "workspace_key": "d40e4c62-71d9-4837-8609-5479d950f6d5", "credential_name": "runtime-reference",
            "identity_sha256": hashlib.sha256(json.dumps(["tenancy", "user", "fingerprint"],
                                                         separators=(",", ":")).encode()).hexdigest()}


def test_object_storage_uses_complete_ephemeral_config_and_disables_blind_sdk_retries(monkeypatch):
    factory, retry = Mock(), Mock()
    monkeypatch.setitem(sys.modules, "oci", SimpleNamespace(object_storage=SimpleNamespace(ObjectStorageClient=factory),
        retry=SimpleNamespace(NoneRetryStrategy=retry)))
    signer = SimpleNamespace(api_key="tenancy/user/fingerprint")
    getter = Mock(return_value="private-fixture")
    runtime_object_storage(config(), signer, getter)
    payload = factory.call_args.args[0]
    assert set(payload) == {"tenancy", "user", "fingerprint", "region", "key_content"}
    assert payload["key_content"] == "private-fixture"
    assert factory.call_args.kwargs["signer"] is signer
    assert factory.call_args.kwargs["retry_strategy"] is retry.return_value


def test_object_storage_rejects_foreign_identity_before_reading_private_material(monkeypatch):
    monkeypatch.setitem(sys.modules, "oci", SimpleNamespace())
    getter = Mock()
    with pytest.raises(ValueError, match="identity differs"):
        runtime_object_storage(config(), SimpleNamespace(api_key="foreign/user/fingerprint"), getter)
    getter.assert_not_called()


def test_identity_is_bound_before_reading_private_key(monkeypatch):
    secrets = Mock()
    secrets.get.side_effect = lambda name, key: {"tenancy": "tenancy", "user": "user",
        "fingerprint": "different", "region": "us-chicago-1"}[key]
    monkeypatch.setitem(sys.modules, "oci", SimpleNamespace(signer=Mock()))
    with pytest.raises(RuntimeError, match="bound Governance"):
        runtime_signer(config(), secrets.get)
    assert all(c.kwargs["key"] != "private_key" for c in secrets.get.call_args_list)
    with pytest.raises(ValueError):
        runtime_signer({**config(), "identity_sha256": None}, secrets.get)


def test_bound_signer_reuses_existing_credential_reference(monkeypatch):
    secrets = Mock()
    secrets.get.side_effect = lambda name, key: {"tenancy": "tenancy", "user": "user",
        "fingerprint": "fingerprint", "region": "us-chicago-1", "private_key": "private-fixture"}[key]
    signer = Mock()
    monkeypatch.setitem(sys.modules, "aidputils", None)
    monkeypatch.setitem(sys.modules, "oci", SimpleNamespace(signer=signer))
    runtime_signer(config(), secrets.get)
    assert signer.Signer.call_args.kwargs["private_key_content"] == "private-fixture"
    assert {c.kwargs["name"] for c in secrets.get.call_args_list} == {"runtime-reference"}


@pytest.mark.parametrize("method,path", [
    ("PUT", "/tables/catalog.schema.table"), ("GET", "https://untrusted.example"),
    ("GET", "/workspaces/other/jobRuns/test"), ("GET", "/tables/a?redirect=elsewhere"),
    ("GET", "/credentials"), ("GET", "/tables/../secrets"),
    ("GET", "/tables/..%2fcredentials"),
])
def test_inventory_cannot_write_follow_foreign_paths_or_read_credentials(method, path):
    session = Mock()
    transport = MetadataTransport(config(), Mock(), session)
    with pytest.raises(ValueError):
        transport(method, path)
    session.request.assert_not_called()


def test_metadata_response_is_bounded_and_redirects_are_not_followed():
    session = MagicMock()
    response = session.request.return_value.__enter__.return_value
    response.status_code = 200
    response.headers = {"etag": "observed"}
    response.iter_content.return_value = [b'{"items":[]}']
    transport = MetadataTransport(config(), Mock(), session)
    assert transport("GET", "/tables") == ({"items": []}, {"etag": "observed"})
    assert session.request.call_args.kwargs["allow_redirects"] is False
    response.iter_content.return_value = [b"x" * (16 * 1024 * 1024 + 1)]
    with pytest.raises(RuntimeError, match="request failed"):
        transport("GET", "/tables")
