"""Explicit project retirement before the existing empty installation phases."""
from __future__ import annotations

import hashlib
import json
from urllib.parse import quote
from uuid import UUID

from app.aidp import AidpProvisionConflict, AidpProvisionPending, CATALOG_NAME
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.schema import definition, schema_sha256
from .compute import COMPUTE_NAME
from .credentials import _project_credential, existing_project_reference

MODULE = 'ai_market_intelligence'
ROOT = '/Workspace/.control/modules/' + MODULE
PHASES = ('reinstall_inventory', 'reinstall_stop', 'reinstall_resources', 'reinstall_tables',
          'reinstall_storage', 'reinstall_files', 'reinstall_credential')
TERMINAL = {'SUCCESS', 'SUCCEEDED', 'FAILED', 'ERROR', 'CANCELED', 'CANCELLED', 'TIMED_OUT', 'INTERNAL_ERROR', 'UPSTREAM_FAILED', 'UPSTREAM_CANCELED'}


def _etag(headers):
    value = next((value for key, value in headers.items() if key.lower() == 'etag'), None)
    if not value:
        raise AidpProvisionConflict('Native resource has no concurrency token; no destructive action was submitted.')
    return value


def _identity(detail):
    fields = ('key', 'id', 'displayName', 'type', 'createdBy', 'timeCreated', 'entryFilePath',
              'description', 'credentialDescription', 'credentialType', 'dependenciesFilePath', 'computeKey',
              'tasks', 'jobClusters', 'schedule', 'continuous', 'driverConfig', 'replicaConfig',
              'catalogKey', 'schemaKey', 'location', 'tableType', 'tableProperties', 'tableFields',
              'path', 'pathInfo', 'name', 'version', 'managedTableDefinition', 'externalTableDefinition',
              'partitionKeys', 'parameters', 'runAs', 'maxConcurrentRuns', 'gitConfig')
    value = {key: detail[key] for key in fields if key in detail}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _snapshot(client, path, kind, *, identity_recheck=False):
    detail, headers = client._request('GET', path, include_headers=True)
    key = detail.get('key') or detail.get('id')
    if not key:
        raise AidpProvisionConflict('Native resource identity is unavailable; no destructive action was submitted.')
    etag = next((value for name, value in headers.items() if name.lower() == 'etag'), None)
    if not etag:
        if not identity_recheck or kind not in {'agent', 'workflow', 'compute', 'table'}:
            _etag(headers)
        if kind != 'table':
            try:
                UUID(key)
            except (ValueError, TypeError, AttributeError) as exc:
                raise AidpProvisionConflict('Native resource has no stable UUID; it was preserved.') from exc
        elif not detail.get('location'):
            raise AidpProvisionConflict('Native table storage identity is incomplete; it was preserved.')
        if not detail.get('createdBy') or not detail.get('timeCreated') or path.rsplit('/', 1)[-1] != key:
            raise AidpProvisionConflict('Native resource ownership is incomplete; it was preserved.')
    return {'path': path, 'kind': kind, 'key': key, 'identity': _identity(detail),
            'etag': etag, 'identity_recheck': not bool(etag), 'done': False}, detail


def inventory(client, state):
    """Freeze the complete allowlist before any cancellation or deletion."""
    scope = '/workspaces/' + state['workspace']
    old = state['previous_resources']
    if client.object_storage.get_bucket(client.settings.objectstorage_namespace, client.settings.artifacts_bucket_name).data.versioning != 'Disabled':
        raise AidpProvisionConflict('Versioned artifacts require explicit version cleanup before reinstalling; nothing was removed.')
    items = []
    legacy_root = False
    agents = client._agents(state['workspace'], MODULE)
    if len(agents) > 1:
        raise AidpProvisionConflict('Duplicate project agents require reconciliation before reinstalling.')
    for row in agents:
        item, detail = _snapshot(client, scope + '/agents/' + quote(row['key'], safe=''), 'agent', identity_recheck=state.get('mode') == 'reinstall')
        entries = {ROOT + '/agent/market_intelligence.py', '/Workspace/ai_market_intelligence/agent/market_intelligence.py'}
        if (detail.get('type') != 'CODE' or detail.get('entryFilePath') not in entries
                or old.get('agent_key', detail['key']) != detail['key']):
            raise AidpProvisionConflict('An agent outside the approved project was found; nothing was removed.')
        items.append(item)
        legacy_root = detail.get('entryFilePath') == '/Workspace/ai_market_intelligence/agent/market_intelligence.py'
        for deployment in client._list(item['path'] + '/deployments'):
            snapshot, _ = _snapshot(client, item['path'] + '/deployments/' + quote(deployment['key'], safe=''), 'deployment')
            items.append(snapshot)
    for row in client._list(scope + '/jobs'):
        path = row.get('path', '')
        if not (path == ROOT or path.startswith(ROOT + '/')):
            continue
        item, detail = _snapshot(client, scope + '/jobs/' + quote(row['key'], safe=''), 'workflow', identity_recheck=state.get('mode') == 'reinstall')
        tasks = detail.get('tasks') or []
        if not tasks or any(task.get('source') != 'WORKSPACE' or not str(task.get('filePath') or task.get('notebookPath') or '').startswith(ROOT + '/') for task in tasks):
            raise AidpProvisionConflict('A project workflow has an unrelated task binding; nothing was removed.')
        items.append(item)
    catalog = client._catalog(CATALOG_NAME)
    expected = {table['name'] for table in definition()['tables']}
    for schema in client._list('/schemas', params={'catalogKey': catalog['key']}):
        if client._resource_name(schema) not in {'oci_artifacts', 'oci_gold'}:
            continue
        for row in client._schema_tables(catalog['key'], schema['key']):
            name = client._resource_name(row)
            if not name.startswith('ai_mi_'):
                continue
            item, detail = _snapshot(client, '/tables/' + quote(row['key'], safe=''), 'table', identity_recheck=state.get('mode') == 'reinstall')
            properties = {prop['propertyName']: prop.get('propertyValue') for prop in detail.get('tableProperties', [])}
            if (name not in expected or client._resource_name(detail) != name
                    or detail.get('catalogKey') != catalog['key'] or detail.get('schemaKey') != schema['key']
                    or properties.get('ai_mi.module_id') != MODULE or properties.get('ai_mi.schema_sha256') != schema_sha256()
                    or detail.get('tableType') not in {'MANAGED', 'EXTERNAL'}):
                raise AidpProvisionConflict('An unverified table was found in the project scope; nothing was removed.')
            items.append(item)
    computes = [row for row in client._list(scope + '/clusters', params={'type': 'AI_COMPUTE'}) if client._resource_name(row) == COMPUTE_NAME]
    if len(computes) > 1:
        raise AidpProvisionConflict('Duplicate dedicated AI Compute resources require reconciliation.')
    for row in computes:
        item, detail = _snapshot(client, scope + '/clusters/' + quote(row['key'], safe=''), 'compute', identity_recheck=state.get('mode') == 'reinstall')
        prefix = 'AI Market Intelligence development compute; operation='
        description = detail.get('description', '')
        if detail.get('type') != 'AI_COMPUTE' or not description.startswith(prefix):
            raise AidpProvisionConflict('The named compute is not owned by this project.')
        UUID(description.removeprefix(prefix))
        if old.get('agent_compute_key', detail['key']) != detail['key']:
            raise AidpProvisionConflict('The recorded compute identity changed.')
        owned_agents = {item['key'] for item in items if item['kind'] == 'agent'}
        if any(agent.get('computeKey') == detail['key'] and agent['key'] not in owned_agents for agent in client._list(scope + '/agents')):
            raise AidpProvisionConflict('Another agent uses this compute; it will not be removed.')
        items.append(item)
    credential = _project_credential(client)
    if credential:
        reference = existing_project_reference(credential, old.get('apify_credential_key', ''))
        item, detail = _snapshot(client, '/credentials/' + quote(reference['credential_key'], safe=''), 'credential')
        existing_project_reference(detail, reference['credential_key'])
        items.append(item)
    state['retirement'] = {'items': items, 'cancel_requested': [],
                           'storage': {'bucket': client.settings.artifacts_bucket_name, 'namespace': client.settings.objectstorage_namespace},
                           'legacy_root': legacy_root}
    client._save(state)


def _remove(client, state, item):
    detail, headers = client._request('GET', item['path'], allow_not_found=True, include_headers=True)
    if detail is None:
        if not item.get('delete_requested'):
            raise AidpProvisionConflict('An inventoried resource disappeared before removal; review the project inventory.')
        item['done'] = True
        client._save(state)
        return
    if _identity(detail) != item['identity']:
        raise AidpProvisionConflict('A resource changed after the reinstall inventory; it was preserved.')
    if item['kind'] == 'table' and item.get('etag') and _etag(headers) != item['etag']:
        raise AidpProvisionConflict('The inventoried table revision changed; it was preserved.')
    if item['kind'] == 'compute':
        if any(agent.get('computeKey') == item['key'] for agent in client._list('/workspaces/' + state['workspace'] + '/agents')):
            raise AidpProvisionConflict('An agent still uses this compute; it was preserved.')
    if detail.get('lifecycleState') == 'DELETING' or detail.get('state') == 'DELETING':
        raise AidpProvisionPending('Waiting for project resource removal.', state['phase'])
    item['delete_requested'] = True
    client._save(state)
    if item.get('identity_recheck'):
        # Some native resources omit ETag. This final read is not atomic CAS.
        detail, headers = client._request('GET', item['path'], allow_not_found=True, include_headers=True)
        if detail is None or _identity(detail) != item['identity']:
            raise AidpProvisionConflict('A resource changed before removal; it was preserved for review.')
        if item['kind'] == 'table':
            _no_writers(client, state)
        if item['kind'] == 'compute' and any(agent.get('computeKey') == item['key'] for agent in client._list('/workspaces/' + state['workspace'] + '/agents')):
            raise AidpProvisionConflict('An agent now uses this compute; it was preserved.')
        etag = next((value for name, value in headers.items() if name.lower() == 'etag'), None)
        delete_headers = {'if-match': etag} if etag else {}
    else:
        delete_headers = {'if-match': _etag(headers)}
    client._request('DELETE', item['path'], headers=delete_headers, allow_not_found=True)
    raise AidpProvisionPending('Removing project ' + item['kind'] + '; its identity and progress are saved.', state['phase'])


def step(client, state):
    phase = state['phase']
    if phase == 'reinstall_inventory':
        inventory(client, state)
        return
    plan = state['retirement']
    if plan['storage'] != {'bucket': client.settings.artifacts_bucket_name, 'namespace': client.settings.objectstorage_namespace}:
        raise AidpProvisionConflict('The artifacts destination changed after confirmation; resources were preserved.')
    scope = '/workspaces/' + state['workspace']
    if phase == 'reinstall_stop':
        _stop_runs(client, state)
        return
    kinds = {'reinstall_resources': ('workflow', 'deployment', 'agent', 'compute'), 'reinstall_tables': ('table',),
             'reinstall_credential': ('credential',)}
    if phase in kinds:
        _stop_runs(client, state)
        if phase == 'reinstall_tables':
            _no_writers(client, state)
        for kind in kinds[phase]:
            for item in plan['items']:
                if item['kind'] == kind and not item['done']:
                    _remove(client, state, item)
        return
    if phase == 'reinstall_storage':
        _stop_runs(client, state)
        _no_writers(client, state)
        bucket, namespace = client.settings.artifacts_bucket_name, client.settings.objectstorage_namespace
        if client.object_storage.get_bucket(namespace, bucket).data.versioning != 'Disabled':
            raise AidpProvisionConflict('Artifacts versioning changed; version cleanup must be reviewed before continuing.')
        if bucket in {client.settings.bucket_name, client.settings.gods_eye_control_bucket}:
            raise AidpProvisionConflict('Artifacts must be separate from business-data buckets.')
        response = client.object_storage.list_objects(namespace, bucket, prefix=MODULE + '/', fields='name,etag', limit=100)
        if response.data.objects:
            obj = response.data.objects[0]
            if not obj.etag or not obj.name.startswith(MODULE + '/'):
                raise AidpProvisionConflict('Project artifact identity is unavailable; it was preserved.')
            client.object_storage.delete_object(namespace, bucket, obj.name, if_match=obj.etag)
            raise AidpProvisionPending('Removing project-owned artifacts; shared buckets are retained.', phase)
        return
    if phase == 'reinstall_files':
        paths = [ROOT + '/agent', ROOT + '/ops', ROOT + '/create_model.ipynb', ROOT + '/create_model.py', ROOT + '/manifest.json']
        paths.extend(ROOT + '/.setup_' + UUID(operation).hex for operation in state['retired_operations'])
        if plan['legacy_root']:
            paths.append('/Workspace/ai_market_intelligence')
        for path in paths:
            client._delete_workspace_path(state['workspace'], path, 'Removing the previous project files.')
        return
    raise AidpProvisionConflict('Unknown reinstall phase; no resources were removed.')


def _stop_runs(client, state):
    plan = state['retirement']
    scope = '/workspaces/' + state['workspace']
    for item in plan['items']:
        if item['kind'] != 'workflow':
            continue
        current = client._request('GET', item['path'], allow_not_found=True)
        if current is not None and _identity(current) != item['identity']:
            raise AidpProvisionConflict('Workflow scope changed; its runs were preserved.')
        for run in client._list(scope + '/jobRuns', params={'jobKey': item['key']}):
            if run.get('jobKey') != item['key']:
                raise AidpProvisionConflict('Workflow run ownership changed.')
            if client._governance_run_state(run) not in TERMINAL:
                if run['key'] not in plan['cancel_requested']:
                    plan['cancel_requested'].append(run['key'])
                    client._save(state)
                    client._request('POST', scope + '/jobRuns/' + quote(run['key'], safe='') + '/actions/cancel', payload={}, retry_scope='market-reinstall-cancel:' + state['operation_id'])
                raise AidpProvisionPending('Waiting for the project workflow to stop. Shared Spark remains running.', state['phase'])


def _no_writers(client, state):
    scope = '/workspaces/' + state['workspace']
    if any(row.get('path') == ROOT or str(row.get('path', '')).startswith(ROOT + '/') for row in client._list(scope + '/jobs')):
        raise AidpProvisionConflict('A project workflow still exists; tables and storage were preserved.')
    if client._agents(state['workspace'], MODULE) or any(client._resource_name(row) == COMPUTE_NAME for row in client._list(scope + '/clusters', params={'type': 'AI_COMPUTE'})):
        raise AidpProvisionConflict('The project agent or dedicated compute still exists; tables and storage were preserved.')
