"""Shared project paths and native TEST session binding for current command runners."""
import re


def market_project_path(root, value):
    path = (root / value).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError('The current artifact must stay in its owning project')
    return path


def session(lifecycle, label, *, apply):
    if not label or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', label):
        raise ValueError('A bounded unique native session label is required')
    lifecycle._agent()
    tests = lifecycle._deployments('TEST')
    if not any(row['lifecycleState'] == 'ACTIVE' for row in tests):
        raise ValueError('An active TEST deployment is required before creating its session')
    if not apply:
        return {'action': 'new-session', 'applied': False}
    lifecycle._reconcile()
    lifecycle._guard()
    payload = {'agentFlowKey': lifecycle.agent_key, 'agentFlowComputeKey': lifecycle.compute,
               'displayName': lifecycle.name + ' ' + label, 'deploymentType': 'TEST'}
    body, ledger, record = lifecycle._write('session-test', lifecycle.ws + '/agentFlowSessions', payload=payload, legacy=True)
    binding = lifecycle._session_info(body['key'], tests)
    if binding.get('lifecycleState') not in (None, 'ACTIVE'):
        raise ValueError('The session was submitted; reconcile its receipt before another creation')
    lifecycle._reconciled(ledger, record, resource_key=body['key'])
    lifecycle.state['test_session_key'] = body['key']
    lifecycle._save_state()
    return {'action': 'new-session', 'applied': True, 'binding_verified': True}
