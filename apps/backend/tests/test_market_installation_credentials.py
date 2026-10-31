from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from app.market_installation.credentials import APIFY_CREDENTIAL_NAME, ensure_apify_credential


OPERATION = '00000000-0000-4000-8000-000000000001'
TOKEN = SecretStr('synthetic-secret-for-local-regression')


def client(rows):
    calls = []

    def request(method, path, **kwargs):
        calls.append((method, path, kwargs['payload']['displayName']))
        payload = kwargs['payload']
        rows.append({'displayName': payload['displayName'], 'description': payload['credentialDescription'],
                     'credentialType': payload['type'], 'key': 'project-key', 'lifeCycleState': 'ACTIVE'})

    return SimpleNamespace(_list=lambda *args, **kwargs: rows, _request=request), calls


def test_new_connection_is_separate_from_shared_oci_credential_and_reuses_operation():
    original = {'displayName': 'AidpRuntime', 'key': 'oci-key', 'type': 'SECRET_TOKEN'}
    api, calls = client([original.copy()])
    result = ensure_apify_credential(api, TOKEN, OPERATION)
    assert result['credential_name'] == APIFY_CREDENTIAL_NAME
    assert ensure_apify_credential(api, TOKEN, OPERATION) == result
    assert calls == [('POST', '/credentials', APIFY_CREDENTIAL_NAME)]
    assert api._list()[0] == original


def test_unrelated_named_secret_is_never_adopted_or_overwritten():
    api, calls = client([{'displayName': APIFY_CREDENTIAL_NAME, 'key': 'unrelated', 'type': 'SECRET_TOKEN',
                          'credentialDescription': 'owned by someone else'}])
    with pytest.raises(RuntimeError, match='unrelated credential'):
        ensure_apify_credential(api, TOKEN, OPERATION)
    assert calls == []


def test_missing_recorded_secret_is_not_silently_recreated():
    api, calls = client([])
    with pytest.raises(RuntimeError, match='missing'):
        ensure_apify_credential(api, TOKEN, OPERATION, expected_key='lost-key')
    assert calls == []


def test_provider_error_never_exposes_the_secret_and_retry_must_reconcile():
    api, _ = client([])

    def fail(*args, **kwargs):
        raise RuntimeError('server message ' + TOKEN.get_secret_value())

    api._request = fail
    with pytest.raises(RuntimeError, match='same operation') as result:
        ensure_apify_credential(api, TOKEN, OPERATION)
    assert TOKEN.get_secret_value() not in str(result.value)
