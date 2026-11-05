"""Project-owned AI Compute provisioning; never deletes shared resources."""
from uuid import UUID
from app.services.oci.aidp import AidpClient
from app.services.oci.contracts import AidpProvisionPending
from .resources import resource


COMPUTE_NAME = 'aidp_ai_market_intelligence_compute'


def compute_payload(operation_id, *, name=COMPUTE_NAME):
    if str(UUID(operation_id)) != operation_id:
        raise ValueError('A canonical compute operation UUID is required')
    if name != COMPUTE_NAME:
        raise ValueError('Unknown Market Intelligence compute name')
    declared = resource('compute')
    if declared['name'] != name or declared['type'] != 'AI_COMPUTE':
        raise ValueError('Current compute descriptor identity changed')
    return {'type': declared['type'], 'displayName': declared['name'],
            'description': 'AI Market Intelligence development compute; operation=' + operation_id,
            'driverConfig': declared['driverConfig'], 'replicaConfig': declared['replicaConfig']}


def prepare_compute(client, expected_workspace, operation_id, *, expected_key='', name=COMPUTE_NAME):
    if client._workspace()['key'] != expected_workspace:
        raise RuntimeError('Compute workspace identity changed')
    payload = compute_payload(operation_id, name=name)
    path = f'/workspaces/{expected_workspace}/clusters'
    if expected_key:
        compute = client._request('GET', path + '/' + expected_key, allow_not_found=True)
        if not compute:
            raise RuntimeError('Recorded market compute is missing; reconcile before recreating')
    else:
        matches = [row for row in client._list(path, params={'type': 'AI_COMPUTE', 'displayName': name})
                   if row.get('displayName') == name]
        if len(matches) > 1:
            raise RuntimeError('Duplicate market compute names')
        compute = matches[0] if matches else None
        if not compute:
            compute = client._request('POST', path, payload=payload, phase='workspace',
                retry_scope='ai-market-compute:' + operation_id)
        if not compute or not compute.get('key'):
            raise AidpProvisionPending('Compute creation is pending; keep the same operation identity', 'compute')
        compute = client._request('GET', path + '/' + compute['key'])
    if (compute.get('displayName') != name or compute.get('type') != 'AI_COMPUTE'
            or compute.get('description') != payload['description']):
        raise RuntimeError('Refusing to adopt a compute outside this project operation')
    state = str(compute.get('lifecycleState') or compute.get('state') or '').upper()
    try:
        AidpClient._require_operational_state(compute, {'ACTIVE', 'STOPPED'}, 'Market AI compute', 'compute')
    except AidpProvisionPending:
        # The caller saves the validated key before waiting for readiness.
        pass
    shape = compute.get('driverConfig', {}).get('driverShapeConfig', {})
    if shape.get('ocpus') != 1 or shape.get('memoryInGBs') != 16:
        raise RuntimeError('Market compute shape differs from the prepared payload')
    return {'compute_key': compute['key'], 'state': state, 'name': name}
