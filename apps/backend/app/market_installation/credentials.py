"""Install a project-specific Apify secret without modifying shared OCI identity."""
from uuid import UUID

from pydantic import SecretStr
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence import APIFY_CREDENTIAL_NAME


APIFY_SECRET_KEY = 'token'


def _project_credential(client):
    rows = [row for row in client._list('/credentials', phase='control')
            if row.get('displayName') == APIFY_CREDENTIAL_NAME]
    if len(rows) > 1:
        raise RuntimeError('Duplicate project credentials; reconcile before continuing')
    return rows[0] if rows else None


def _credential_reference(existing, expected_key, description):
    """Validate only the returned metadata; never include secret input in errors."""
    if not existing:
        raise RuntimeError('Project credential is not visible yet; keep the same operation')
    key = str(existing.get('key') or existing.get('id') or '')
    if not key or expected_key and key != expected_key:
        raise RuntimeError('Project credential identity changed')
    if not expected_key and (existing.get('credentialDescription') or existing.get('description')) != description:
        raise RuntimeError('Refusing to adopt an unrelated credential with the same name')
    if (existing.get('type') or existing.get('credentialType')) != 'SECRET_TOKEN':
        raise RuntimeError('Unexpected project credential type')
    if (existing.get('lifecycleState') or existing.get('lifeCycleState') or existing.get('state')) != 'ACTIVE':
        raise RuntimeError('Project credential is not active yet; keep the same operation')
    return {'credential_key': key, 'credential_name': APIFY_CREDENTIAL_NAME, 'secret_key': APIFY_SECRET_KEY}


def existing_project_reference(existing, expected_key=''):
    description = (existing or {}).get('credentialDescription') or (existing or {}).get('description') or ''
    if not description.startswith('AI Market Intelligence'):
        raise RuntimeError('The existing Apify credential has unrelated ownership; it will not be changed.')
    return _credential_reference(existing, expected_key or existing.get('key') or existing.get('id'), description)


def created_by_operation(existing, state):
    description = (existing or {}).get('credentialDescription') or (existing or {}).get('description')
    return bool(state.get('resources', {}).get('apify_create_requested') and description ==
                'AI Market Intelligence Apify connection; operation=' + state['operation_id'])


def ensure_apify_credential(client, token: SecretStr, operation_id: str, *, expected_key=''):
    """Caller persists operation_id before this write; retries reuse its receipt."""
    if str(UUID(operation_id)) != operation_id or not isinstance(token, SecretStr):
        raise ValueError('A stable operation and redacted secret value are required')
    value = token.get_secret_value()
    if not 20 <= len(value) <= 512 or any(ord(c) <= 32 for c in value):
        raise ValueError('Invalid Apify secret format')
    description = 'AI Market Intelligence Apify connection; operation=' + operation_id

    existing = _project_credential(client)
    if not existing:
        if expected_key:
            raise RuntimeError('The recorded project credential is missing')
        payload = {'displayName': APIFY_CREDENTIAL_NAME, 'credentialDescription': description,
                   'type': 'SECRET_TOKEN', 'credentialDetails': {'credentialType': 'SECRET_TOKEN',
                   'secretTokenPair': [{'secretKey': APIFY_SECRET_KEY, 'secretValue': value}]}}
        try:
            client._request('POST', '/credentials', payload=payload, phase='control')
        except Exception:
            # An uncertain create is reconciled by the same operation, never rotated.
            raise RuntimeError('Project credential creation is incomplete; reconcile the same operation') from None
        existing = _project_credential(client)
    return _credential_reference(existing, expected_key, description)
