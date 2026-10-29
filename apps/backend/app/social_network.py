"""Independent, durable AI Social Network module; shared native provisioning primitives."""
from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import datetime, timezone

from .aidp import (AidpClient, AidpProvisionConflict, AidpProvisionError, AidpProvisionPending,
                   CATALOG_NAME, MODULE_CONTROL_ROOT)
from .lab_packs import load_lab_pack, module_runtime_source
from .labs.ai_social_network.notebooks.social_ingest import GOLD_TABLES, TABLE_DESCRIPTIONS, location, validate_config

MODULE_ID = 'ai_social_network'
DISPLAY_NAME = 'AI Social Network'


def module_payload(manifest=None):
    state = manifest or {}
    operation = state.get('operation') or {}
    version = load_lab_pack(MODULE_ID, require_available=False).pack_version
    installed = state.get('status', 'not_installed') != 'not_installed'
    return {'module_id': MODULE_ID, 'display_name': DISPLAY_NAME,
        'status': state.get('status', 'not_installed'), 'installed': installed,
        'bundled_version': version, 'installed_version': state.get('pack_version') if installed else None,
        'update_available': installed and state.get('pack_version') != version,
        'phase': state.get('phase', 'not_installed'), 'enabled': bool(state.get('enabled')),
        'operation_id': operation.get('operation_id'), 'operation_type': operation.get('type'),
        'source_bucket': (state.get('config') or {}).get('source_bucket', ''),
        'source_prefix': (state.get('config') or {}).get('source_prefix', 'aidp-social/')}


class SocialAidpClient(AidpClient):
    _agent_compute_name = 'aidp_ai_social_network_compute'
    _agent_name = MODULE_ID
    _agent_entry_filename = 'social_agent.py'
    _sync_notebook_filename = 'social_ingest.ipynb'
    _module_job_name = 'wf_ai_social_network_ingest'
    _agent_description = 'Read-only social Gold analytics with native AI Lakehouse conversation memory'

    @property
    def _module_root(self):
        return MODULE_CONTROL_ROOT + '/' + MODULE_ID

    def _module_manifest(self, workspace_key):
        manifest = self._workspace_json(workspace_key, self._module_manifest_path, 'Invalid social module manifest')
        if manifest is not None:
            try:
                operation = manifest['operation']
                valid = (manifest['schema_version'] == 1 and manifest['module_id'] == MODULE_ID
                    and str(uuid.UUID(operation['operation_id'])) == operation['operation_id']
                    and operation['type'] in ('install', 'redeploy')
                    and manifest['status'] in ('installing', 'redeploying', 'active', 'error')
                    and isinstance(manifest['resources'], dict))
                validate_config(manifest['config'])
            except (KeyError, TypeError, ValueError):
                valid = False
            if not valid:
                raise AidpProvisionError('The social module manifest is invalid; reconciliation stopped')
            self._social_config = manifest['config']
        return manifest

    def _new_module_manifest(self, operation_id, operation_type):
        config = getattr(self, '_social_config', None)
        if config is None:
            raise AidpProvisionConflict('Select the source bucket and prefix before deployment')
        return {'schema_version': 1, 'module_id': MODULE_ID, 'pack_version': load_lab_pack(MODULE_ID).pack_version,
            'status': 'installing' if operation_type == 'install' else 'redeploying', 'phase': 'verify',
            'enabled': False, 'config': config, 'resources': {},
            'operation': {'operation_id': operation_id, 'type': operation_type, 'phase': 'started'}}

    def _resume_governance_operation(self, workspace_key, manifest, operation_id, operation_type):
        failed_sync = manifest.get('status') == 'error' and manifest.get('phase') == 'sync'
        super()._resume_governance_operation(workspace_key, manifest, operation_id, operation_type)
        if failed_sync:
            manifest.update(phase='control', attempt=int(manifest.get('attempt', 0)) + 1)
            self._write_module_manifest(workspace_key, manifest)

    async def status(self):
        def observe():
            workspace = str(self._workspace()['key'])
            manifest = self._module_manifest(workspace)
            if manifest:
                manifest = {**manifest, 'enabled': self._governance_enabled(workspace, manifest)}
            return module_payload(manifest)
        return await asyncio.to_thread(observe)

    def source_config(self, bucket, prefix):
        values = {'namespace': self.settings.objectstorage_namespace,
            'source_bucket': bucket or self.settings.bucket_name, 'source_prefix': prefix,
            'gold_bucket': self.settings.gods_eye_control_bucket, 'artifacts_bucket': self.settings.artifacts_bucket_name,
            'catalog': CATALOG_NAME, 'gold_schema': 'oci_gold', 'control_schema': 'oci_artifacts',
            **self._governance_oci_config()}
        try:
            return validate_config(values)
        except ValueError as exc:
            raise AidpProvisionConflict(str(exc)) from None

    def _governance_job_payload(self, workspace_key, compute_key, **_kwargs):
        source = module_runtime_source(MODULE_ID, 'notebooks/social_ingest.py').decode('utf-8')
        source = source.replace('CONFIG = {}', 'CONFIG = ' + repr(self._social_config), 1)
        # One readable native Python notebook; source configuration is the only rendered difference.
        notebook = {'nbformat': 4, 'nbformat_minor': 5, 'metadata': {
            'kernelspec': {'display_name': 'Python 3', 'language': 'python', 'name': 'python3'},
            'language_info': {'name': 'python'}}, 'cells': [{'cell_type': 'code', 'execution_count': None,
                'metadata': {}, 'outputs': [], 'source': source.splitlines(keepends=True)}]}
        path = self._module_root + '/' + self._sync_notebook_filename
        changed = self._upload_notebook(workspace_key, path, notebook, repair_drift=True)
        return {'name': self._module_job_name, 'path': self._module_root,
            'description': 'Incremental social JSON to committed Delta Gold, every five minutes UTC',
            'maxConcurrentRuns': 1, 'continuous': None, 'queue': {'isEnabled': False},
            'schedule': {'quartzCronExpression': '0 0/5 * * * ?', 'timezoneId': 'UTC',
                         'pauseStatus': 'PAUSED' if _kwargs['paused'] else 'UNPAUSED'},
            'jobClusters': [{'clusterKey': compute_key}], 'tasks': [{'type': 'NOTEBOOK_TASK',
                'taskKey': 'ingest_social_gold', 'dependsOn': [], 'runIf': 'ALL_SUCCESS',
                'maxRetries': 0, 'isRetryOnTimeout': False, 'notebookPath': path,
                'cluster': {'clusterKey': compute_key}, 'parameters': []}]}, changed

    def _pause_writer(self, workspace_key, compute_key):
        job_key = self._job_key(workspace_key, self._module_job_name)
        if not job_key:
            return
        details = self._request('GET', f'/workspaces/{workspace_key}/jobs/{job_key}', phase='control')
        schedule = details.get('schedule') or {}
        if schedule.get('pauseStatus') != 'PAUSED':
            allowed = ('name', 'path', 'description', 'maxConcurrentRuns', 'continuous', 'queue', 'jobClusters', 'tasks')
            payload = {key: details[key] for key in allowed if key in details}
            payload['name'] = self._module_job_name
            payload['schedule'] = {**schedule, 'pauseStatus': 'PAUSED'}
            self._publish_job(workspace_key, job_key, payload, compute_key)
        runs = self._list(f'/workspaces/{workspace_key}/jobRuns', params={'jobKey': job_key}, phase='control')
        terminal = {'SUCCESS', 'SUCCEEDED', 'FAILED', 'ERROR', 'CANCELED', 'CANCELLED', 'INTERNAL_ERROR', 'TIMED_OUT', 'UPSTREAM_FAILED', 'UPSTREAM_CANCELED'}
        if any(self._governance_run_state(run) not in terminal for run in runs):
            raise AidpProvisionPending('Waiting for the managed social writer before updating its source', 'control')

    def _verify_social(self, workspace_key, manifest, _operation_type):
        for key in ('source_bucket', 'gold_bucket', 'artifacts_bucket'):
            try:
                self.object_storage.head_bucket(self.settings.objectstorage_namespace, self._social_config[key])
            except self._oci.exceptions.ServiceError:
                raise AidpProvisionError('A selected social source, Gold or artifacts bucket is unavailable') from None
        self._ensure_catalog(CATALOG_NAME)
        manifest['phase'] = 'control'
        self._write_module_manifest(workspace_key, manifest)

    def _control_social(self, workspace_key, manifest, _operation_type):
        compute = self._shared_compute(workspace_key)
        compute_key = str(compute['key'])
        self._pause_writer(workspace_key, compute_key)
        credential_key, _ = self._ensure_governance_credential()
        job_key, _ = self._ensure_governance_job(workspace_key, compute_key, desired_enabled=True, paused=True)
        resources = manifest['resources']
        resources.update(credential_key=credential_key, workflow_key=job_key, gold_query_compute_id=compute_key,
            sync_requested_at=datetime.now(timezone.utc).isoformat())
        self._write_module_manifest(workspace_key, manifest)
        self._request('POST', f'/workspaces/{workspace_key}/jobRuns',
            payload={'jobKey': job_key, 'parameters': [], 'queue': {'isEnabled': False}}, phase='sync',
            retry_scope='social-bootstrap:' + manifest['operation']['operation_id'] + ':' + str(manifest.get('attempt', 0)))
        manifest['phase'] = 'sync'
        self._write_module_manifest(workspace_key, manifest)
        raise AidpProvisionPending('The first social Gold ingestion is starting', 'sync')

    def _sync_social(self, workspace_key, manifest, _operation_type):
        resources = manifest['resources']
        try:
            done = self._successful_governance_sync(workspace_key, resources['workflow_key'], requested_at=resources['sync_requested_at'])
        except AidpProvisionError:
            raise AidpProvisionError('The social ingestion failed; inspect its native task output before resuming') from None
        if not done:
            raise AidpProvisionPending('The first social Gold ingestion is still running', 'sync')
        manifest['phase'] = 'metadata'
        self._write_module_manifest(workspace_key, manifest)

    def _catalog_descriptions(self):
        config = self._social_config
        catalog_key = str(self._catalog(config['catalog'])['key'])
        schemas = self._list('/schemas', params={'catalogKey': catalog_key}, phase='metadata')
        waiting = False
        for schema_name in (config['gold_schema'], config['control_schema']):
            matches = [item for item in schemas if self._resource_name(item) == schema_name]
            if len(matches) > 1:
                raise AidpProvisionError('Duplicate social catalog schemas; metadata update stopped')
            if not matches:
                raise AidpProvisionPending('The social catalog schema is not visible yet', 'metadata')
            schema_key = str(matches[0]['key'])
            tables = self._schema_tables(catalog_key, schema_key)
            for name, description in TABLE_DESCRIPTIONS.items():
                gold = name in GOLD_TABLES
                if schema_name != config['gold_schema' if gold else 'control_schema']:
                    continue
                matches = [item for item in tables if self._resource_name(item) == name]
                if len(matches) > 1:
                    raise AidpProvisionError('Duplicate social catalog tables; metadata update stopped')
                if not matches:
                    raise AidpProvisionPending('The social catalog table is not visible yet', 'metadata')
                visible = self._describe_catalog_table(catalog_key, schema_key, name, str(matches[0]['key']), description)
                waiting = waiting or not visible
        if waiting:
            raise AidpProvisionPending('The social catalog descriptions are being published', 'metadata')

    def _describe_catalog_table(self, catalog_key, schema_key, name, key, description):
        path = '/tables/' + key
        detail, headers = self._request('GET', path, include_headers=True, phase='metadata')
        expected_location = location(self._social_config, name)
        external = detail.get('externalTableDefinition') or {}
        identity = (detail.get('key'), detail.get('displayName'), detail.get('catalogKey'), detail.get('schemaKey'))
        if identity != (key, name, catalog_key, schema_key):
            raise AidpProvisionError('The social catalog table identity changed; metadata update stopped')
        if (detail.get('tableType') != 'EXTERNAL' or detail.get('location', '').rstrip('/') != expected_location
                or external.get('objectStorageLocationPath', '').rstrip('/') != expected_location
                or str(external.get('externalTableDataFormat')).upper() != 'DELTA'):
            raise AidpProvisionError('The social catalog table storage changed; metadata update stopped')
        if str(detail.get('description') or '').strip():
            return True
        etag = headers.get('etag') or headers.get('ETag')
        self._request('PUT', path, payload={'description': description},
                      headers={'If-Match': etag} if etag else None, phase='metadata')
        observed = self._request('GET', path, phase='metadata')
        return observed.get('description') == description

    def _metadata_social(self, workspace_key, manifest, _operation_type):
        self._catalog_descriptions()
        manifest['phase'] = 'agent'
        self._write_module_manifest(workspace_key, manifest)

    def _governance_agent_artifacts(self):
        workspace = str(self._workspace()['key'])
        manifest = self._module_manifest(workspace)
        if not manifest or not self.settings.agent_model_id:
            raise AidpProvisionError('The social model or managed deployment configuration is incomplete')
        config = {key: self._social_config[key] for key in ('catalog', 'gold_schema', 'credential_name', 'identity_sha256')}
        config.update(region=self.settings.aidp_region, compartment_id=self.settings.compartment_id,
            model_id=self.settings.agent_model_id, gold_query_compute_id=manifest['resources']['gold_query_compute_id'])
        source = module_runtime_source(MODULE_ID, 'notebooks/agent/social_agent.py').decode('utf-8')
        source = source.replace('CONFIG = {}', 'CONFIG = ' + repr(config), 1).encode('utf-8')
        digest = hashlib.sha256(source).hexdigest()
        descriptor = json.dumps({'schema_version': 1, 'module_id': MODULE_ID, 'entry_file': self._agent_entry_filename,
            'entry_sha256': digest, 'tools': ['summary', 'ranking', 'trend', 'search', 'history', 'details', 'comments'], 'memory': 'AI_LAKEHOUSE'}, sort_keys=True, separators=(',', ':')).encode()
        return source, descriptor, digest

    def _activate_social(self, workspace_key, manifest, _operation_type):
        compute = manifest['resources']['gold_query_compute_id']
        _, changed = self._ensure_governance_job(workspace_key, compute, desired_enabled=True, paused=False)
        if changed:
            raise AidpProvisionPending('The five-minute social schedule is being activated', 'activation')
        manifest.update(status='active', phase='active', enabled=True, pack_version=load_lab_pack(MODULE_ID).pack_version)
        manifest['operation']['phase'] = 'complete'
        self._write_module_manifest(workspace_key, manifest)

    def _reconcile_social(self, user_ocid, operation_id, operation_type):
        workspace = str(self._workspace()['key'])
        manifest, complete = self._prepare_governance_reconciliation(workspace, operation_id, operation_type)
        if complete:
            return module_payload(manifest)
        handlers = {'verify': self._verify_social, 'control': self._control_social, 'sync': self._sync_social, 'metadata': self._metadata_social,
            'agent': self._reconcile_governance_agent, 'permissions': self._reconcile_governance_permissions,
            'activation': self._activate_social}
        while handler := handlers.get(manifest['phase']):
            handler(workspace, manifest, operation_type)
        return module_payload(manifest)

    async def operate(self, user_ocid, operation_id, operation_type, bucket, prefix, *, role_membership_verified=False):
        if operation_type not in ('install', 'redeploy'):
            raise AidpProvisionConflict('Unsupported social module operation')
        if not role_membership_verified and not await self.is_platform_admin(user_ocid):
            raise AidpProvisionConflict('The selected user is not an AI_DATA_PLATFORM_ADMIN')
        async with self._locks.setdefault(MODULE_ID, asyncio.Lock()):
            def configure():
                workspace = str(self._workspace()['key'])
                existing = self._module_manifest(workspace)
                self._ensure_governance_credential()
                proposed = self.source_config(bucket, prefix)
                if existing and any(proposed[key] != existing['config'][key] for key in ('source_bucket', 'source_prefix', 'gold_bucket', 'artifacts_bucket', 'identity_sha256')):
                    raise AidpProvisionConflict('The installed social source and storage identity are immutable; use a planned data migration to change them')
                self._social_config = existing['config'] if existing else proposed
            await asyncio.to_thread(configure)
            return await self._run_module_operation(self._reconcile_social, user_ocid, operation_id, operation_type)

    async def agent_endpoint(self):
        return await self.governance_agent_endpoint()
