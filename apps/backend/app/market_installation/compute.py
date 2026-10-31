"""Project-owned AI Compute provisioning; never deletes shared resources."""
from uuid import UUID


COMPUTE_NAME = 'aidp_ai_market_intelligence_compute'


def compute_payload(operation_id):
    if str(UUID(operation_id)) != operation_id:
        raise ValueError('A canonical compute operation UUID is required')
    return {'type': 'AI_COMPUTE', 'displayName': COMPUTE_NAME,
            'description': 'AI Market Intelligence development compute; operation=' + operation_id,
            'driverConfig': {'driverShapeConfig': {'ocpus': 1, 'memoryInGBs': 16}},
            'replicaConfig': {'minReplica': 1, 'maxReplica': 1}}


def prepare_compute(client, expected_workspace, operation_id, *, expected_key=''):
    if client._workspace()['key'] != expected_workspace:
        raise RuntimeError('Compute workspace identity changed')
    payload = compute_payload(operation_id)
    path = f'/workspaces/{expected_workspace}/clusters'
    if expected_key:
        compute = client._request('GET', path + '/' + expected_key, allow_not_found=True)
        if not compute:
            raise RuntimeError('Recorded market compute is missing; reconcile before recreating')
    else:
        matches = [row for row in client._list(path, params={'type': 'AI_COMPUTE', 'displayName': COMPUTE_NAME})
                   if row.get('displayName') == COMPUTE_NAME]
        if len(matches) > 1:
            raise RuntimeError('Duplicate market compute names')
        compute = matches[0] if matches else None
        if not compute:
            compute = client._request('POST', path, payload=payload, phase='workspace',
                retry_scope='ai-market-compute:' + operation_id)
        if not compute or not compute.get('key'):
            raise RuntimeError('Compute creation is pending; keep the same operation identity')
        compute = client._request('GET', path + '/' + compute['key'])
    if (compute.get('displayName') != COMPUTE_NAME or compute.get('type') != 'AI_COMPUTE'
            or compute.get('description') != payload['description']):
        raise RuntimeError('Refusing to adopt a compute outside this project operation')
    state = compute.get('state') or compute.get('lifecycleState')
    if state not in {'CREATING', 'STARTING', 'ACTIVE', 'STOPPED'}:
        raise RuntimeError('Market compute is not ready; native state=' + str(state) + '; no replacement created')
    shape = compute.get('driverConfig', {}).get('driverShapeConfig', {})
    if shape.get('ocpus') != 1 or shape.get('memoryInGBs') != 16:
        raise RuntimeError('Market compute shape differs from the prepared payload')
    return {'compute_key': compute['key'], 'state': state, 'name': COMPUTE_NAME}
