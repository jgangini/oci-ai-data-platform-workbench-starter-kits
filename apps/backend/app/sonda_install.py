"""Install the operator-bound Sonda generation without activating it implicitly."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from urllib.parse import quote
from uuid import UUID

from .aidp import AidpProvisionConflict, AidpProvisionError, AidpProvisionPending
from .lab_packs import load_lab_pack
from .social_network import MODULE_ID, SocialAidpClient
from .sonda_contract import no_duplicate_keys
from .sonda_runtime import chat_source, ingestion_source, investigator_source
from .labs.ai_social_network.notebooks.sonda_delta import CONTRACT_SHA256, checked_binding, definitions, fingerprint
from .labs.ai_social_network.notebooks.sonda_io import operational_prefix
from .labs.ai_social_network.notebooks.agent.sonda_model import STUDY_PROMPT_VERSION


INVESTIGATOR = 'ai_social_network_investigator'
FIXED_FIELDS = ('namespace', 'source_bucket', 'source_prefix', 'gold_bucket', 'artifacts_bucket',
                'catalog', 'gold_schema', 'control_schema', 'identity_sha256')
BINDING_PATH = re.compile(r'^ai_social_network/registries/([0-9a-f-]{36})/generations/([0-9a-f-]{36})/control/binding\.json$')
EXCLUDED_VERSIONS = {'ai_social_publication', 'ai_social_receipts', 'ai_social_outbox',
                     'ai_social_study_results', 'ai_social_case_study_results'}


def _instant(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.astimezone(timezone.utc) if parsed.utcoffset() is not None else None
    except (AttributeError, TypeError, ValueError):
        return None


def _render(raw, placeholder, binding):
    marker = (placeholder + ' = {}').encode('utf-8')
    if raw.count(marker) != 1:
        raise AidpProvisionError('The checked Sonda runtime has no unique configuration slot')
    return raw.replace(marker, (placeholder + ' = ' + repr(binding)).encode('utf-8'), 1)


class SondaAidpClient(SocialAidpClient):
    """Preserve the public chat identity; the investigator has a private ledger."""

    _sync_notebook_filename = 'sonda_ingest.ipynb'

    def _module_manifest(self, workspace_key):
        manifest = super()._module_manifest(workspace_key)
        if manifest and manifest['config'].get('contract_sha256'):
            try:
                checked_binding(manifest['config'])
            except ValueError:
                raise AidpProvisionError('The installed Sonda generation binding is invalid') from None
        return manifest

    def _control_json(self, key, *, maximum=16384, missing=False):
        try:
            response = self.object_storage.get_object(self.settings.objectstorage_namespace,
                                                      self.settings.artifacts_bucket_name, key)
        except Exception as exc:
            if getattr(exc, 'status', None) == 404 and missing:
                return None
            raise AidpProvisionError('The protected Sonda control object is unavailable') from None
        try:
            raw = response.data.raw.read(maximum + 1)
        finally:
            response.data.close()
        if len(raw) > maximum:
            raise AidpProvisionError('The protected Sonda control object exceeds its limit')
        try:
            value = json.loads(raw.decode('utf-8'), object_pairs_hook=no_duplicate_keys)
        except (UnicodeError, ValueError):
            raise AidpProvisionConflict('The protected Sonda control object is invalid') from None
        if not isinstance(value, dict):
            raise AidpProvisionConflict('The protected Sonda control object must be an object')
        return value

    def _discover_binding(self, expected):
        matches, cursor = [], None
        for _ in range(20):
            options = {'prefix': 'ai_social_network/registries/', 'limit': 1000}
            if cursor:
                options['start'] = cursor
            try:
                page = self.object_storage.list_objects(expected['namespace'], expected['artifacts_bucket'], **options).data
            except Exception:
                raise AidpProvisionError('The protected Sonda bindings cannot be inspected') from None
            for item in page.objects:
                match = BINDING_PATH.fullmatch(item.name)
                if not match:
                    continue
                value = self._control_json(item.name)
                if all(value.get(key) == expected[key] for key in FIXED_FIELDS):
                    try:
                        registry, generation = (str(UUID(key)) for key in match.groups())
                    except ValueError:
                        raise AidpProvisionConflict('A protected Sonda binding has an invalid path identity') from None
                    if (registry, generation) != (value.get('registry_id'), value.get('generation_id')):
                        raise AidpProvisionConflict('The protected Sonda binding path and identity differ')
                    matches.append(value)
            cursor = page.next_start_with
            if not cursor:
                break
        else:
            raise AidpProvisionConflict('Sonda binding discovery exceeded its bounded inventory')
        if len(matches) != 1:
            raise AidpProvisionConflict('Exactly one matching protected Sonda generation binding is required')
        value = matches[0]
        self._check_fence(value, expected)
        return value

    @staticmethod
    def _check_fence(value, expected):
        if any(value.get(key) != expected[key] for key in FIXED_FIELDS):
            raise AidpProvisionConflict('Sonda storage, source or OCI identity does not match its protected binding')
        if value.get('contract_sha256') != CONTRACT_SHA256 or value.get('reset_verified') is not True:
            raise AidpProvisionConflict('Sonda requires the accepted contract and verified operator reset')
        if any(type(value.get(key)) is not bool for key in ('enabled', 'bootstrap', 'integrated_acceptance')):
            raise AidpProvisionConflict('Sonda operational flags must be explicit booleans')
        revision = value.get('bootstrap_config_revision')
        if type(revision) is not int or revision < 1:
            raise AidpProvisionConflict('The protected Sonda binding must identify its bootstrap configuration revision')
        for key in ('region', 'compartment_id', 'credential_name', 'model_id', 'model_revision', 'prompt_version'):
            if key in value and key in expected and value[key] != expected[key]:
                raise AidpProvisionConflict('Sonda runtime selection differs from its protected binding')

    def _fence(self, config):
        value = self._control_json(operational_prefix(config) + 'binding.json')
        self._check_fence(value, config)
        if any(value.get(key) != config[key] for key in ('registry_id', 'generation_id')):
            raise AidpProvisionConflict('The protected Sonda generation no longer matches this installation')
        return value

    def _resolve_model(self):
        model_id = self.settings.agent_model_id
        if not model_id or not self.settings.aidp_region or not self.settings.compartment_id:
            raise AidpProvisionConflict('Select a regional OCI chat model before preparing Sonda')
        config = {**self._oci_config, 'region': self.settings.aidp_region}
        try:
            client = self._oci.generative_ai.GenerativeAiClient(config, signer=self.signer, timeout=(10, 30))
        except Exception:
            raise AidpProvisionError('The selected OCI model client cannot be initialized') from None
        try:
            try:
                model = client.get_model(model_id).data
            except Exception as exc:
                # OCI's get_model is custom-model oriented. Base models still
                # require an exact ID and real version from the official catalog.
                if getattr(exc, 'status', None) not in (404, 405, 501) and getattr(exc, 'code', None) != 'NotSupported':
                    raise
                found, cursor = [], None
                for _ in range(20):
                    options = {'compartment_id': self.settings.compartment_id}
                    if cursor:
                        options['page'] = cursor
                    response = client.list_models(**options)
                    data = response.data
                    rows = data.items if hasattr(data, 'items') and not isinstance(data, dict) else data
                    found.extend(item for item in rows if getattr(item, 'id', None) == model_id)
                    cursor = response.headers.get('opc-next-page')
                    if not cursor:
                        break
                else:
                    raise AidpProvisionConflict('The OCI model catalog exceeded its bounded inventory')
                if len(found) != 1:
                    raise AidpProvisionConflict('The selected OCI model has no unique versioned catalog entry')
                model = found[0]
        except AidpProvisionConflict:
            raise
        except Exception:
            raise AidpProvisionError('The selected OCI model metadata cannot be verified') from None
        finally:
            close = getattr(getattr(client, 'base_client', None), 'close', None)
            if close:
                close()
        version = getattr(model, 'version', None)
        if (getattr(model, 'id', None) != model_id or not isinstance(version, str) or not 1 <= len(version.strip()) <= 200
                or getattr(model, 'lifecycle_state', None) != 'ACTIVE' or 'CHAT' not in (getattr(model, 'capabilities', None) or [])):
            raise AidpProvisionConflict('The selected OCI model must be active, support chat and expose its actual revision')
        return {'model_id': model_id, 'model_revision': version, 'prompt_version': STUDY_PROMPT_VERSION,
                'region': self.settings.aidp_region, 'compartment_id': self.settings.compartment_id}

    def _configure_social(self, bucket, prefix):
        super()._configure_social(bucket, prefix)
        previous = dict(self._social_config)
        expected = self.source_config(bucket, prefix)
        # Established generations never enumerate their growing operational tree.
        declared = self._fence(previous) if previous.get('contract_sha256') else self._discover_binding(expected)
        if previous.get('contract_sha256') and any(previous[key] != declared[key] for key in ('registry_id', 'generation_id')):
            raise AidpProvisionConflict('The installed Sonda generation differs; reconcile the manifest under the verified reset')
        config = {**expected, **{key: declared[key] for key in ('registry_id', 'generation_id', 'contract_sha256', 'bootstrap_config_revision')}, **self._resolve_model()}
        self._check_fence(declared, config)
        try:
            checked_binding(config)
        except ValueError:
            raise AidpProvisionConflict('The protected Sonda generation identifiers are invalid') from None
        self._social_config = config
        self._configured_sonda_binding = dict(config)

    def _new_module_manifest(self, operation_id, operation_type):
        # The inherited reconciliation rereads the old manifest before repair.
        # New content must use the server-verified selection, not that old config.
        self._social_config = dict(self._configured_sonda_binding)
        return super()._new_module_manifest(operation_id, operation_type)

    def _prepare_governance_reconciliation(self, workspace_key, operation_id, operation_type):
        selected = dict(self._configured_sonda_binding)
        manifest, complete = super()._prepare_governance_reconciliation(workspace_key, operation_id, operation_type)
        if manifest['config'] != selected:
            raise AidpProvisionConflict('The installed configuration differs; start an explicit Sonda redeploy after verified reset')
        self._social_config = manifest['config']
        return manifest, complete

    def _governance_enabled(self, workspace_key, manifest):
        if not manifest.get('config', {}).get('contract_sha256'):
            return super()._governance_enabled(workspace_key, manifest)
        try:
            fence = self._fence(manifest['config'])
            return fence['enabled'] and fence['integrated_acceptance'] and super()._governance_enabled(workspace_key, manifest)
        except (AidpProvisionConflict, AidpProvisionError):
            return False

    def _runtime_binding(self, workspace_key, compute_key=None):
        manifest = self._module_manifest(workspace_key)
        if not manifest or not manifest['config'].get('contract_sha256'):
            raise AidpProvisionError('The managed Sonda generation is not configured')
        resources = manifest['resources']
        return {**manifest['config'], 'gold_query_compute_id': compute_key or resources.get('gold_query_compute_id', ''),
                'investigator_agent_key': (resources.get('investigator') or {}).get('agent_key', ''),
                'activity_agent_id':resources.get('agent_key','')}

    def _governance_job_payload(self, workspace_key, compute_key, **kwargs):
        binding = self._runtime_binding(workspace_key, compute_key)
        raw = _render(ingestion_source(), 'RUNNER_BINDING', binding)
        source = raw.decode('utf-8') + '\nprint(json.dumps(run_sonda(RUNNER_BINDING), sort_keys=True))\n'
        notebook = {'nbformat': 4, 'nbformat_minor': 5, 'metadata': {
            'kernelspec': {'display_name': 'Python 3', 'language': 'python', 'name': 'python3'},
            'language_info': {'name': 'python'}}, 'cells': [{'cell_type': 'code', 'execution_count': None,
                'metadata': {}, 'outputs': [], 'source': source.splitlines(keepends=True)}]}
        path = self._module_root + '/' + self._sync_notebook_filename
        changed = self._upload_notebook(workspace_key, path, notebook, repair_drift=True)
        return {'name': self._module_job_name, 'path': self._module_root,
            'description': 'Bound Sonda exchange and coherent Gold reconciliation, every five minutes UTC',
            'maxConcurrentRuns': 1, 'continuous': None, 'queue': {'isEnabled': False},
            'schedule': {'quartzCronExpression': '0 0/5 * * * ?', 'timezoneId': 'UTC',
                         'pauseStatus': 'PAUSED' if kwargs['paused'] else 'UNPAUSED'},
            'jobClusters': [{'clusterKey': compute_key}], 'tasks': [{'type': 'NOTEBOOK_TASK',
                'taskKey': 'reconcile_sonda_gold', 'dependsOn': [], 'runIf': 'ALL_SUCCESS',
                'maxRetries': 0, 'isRetryOnTimeout': False, 'notebookPath': path,
                'cluster': {'clusterKey': compute_key}, 'parameters': []}]}, changed

    def _control_social(self, workspace_key, manifest, operation_type):
        fence = self._fence(self._social_config)
        if fence['enabled'] is not False or fence['bootstrap'] is not True:
            raise AidpProvisionConflict('Pause the protected Sonda generation and authorize bootstrap before deployment')
        super()._control_social(workspace_key, manifest, operation_type)

    def _sync_social(self, workspace_key, manifest, _operation_type):
        resources = manifest['resources']
        try:
            complete=self._successful_governance_sync(workspace_key, resources['workflow_key'], requested_at=resources['sync_requested_at'])
        except AidpProvisionError:
            raise AidpProvisionError('The Sonda bootstrap failed; inspect its native task output before resuming') from None
        if not complete:
            raise AidpProvisionPending('The Sonda bootstrap is still running', 'sync')
        receipt = self._control_json(operational_prefix(self._social_config) + 'runtime.json', maximum=65536, missing=True)
        requested = _instant(resources['sync_requested_at'])
        completed = _instant(receipt.get('completed_at')) if receipt else None
        if (not receipt or not requested or not completed or completed < requested or receipt.get('status') != 'prepared'
                or any(receipt.get(key) != self._social_config[key] for key in ('registry_id', 'generation_id', 'contract_sha256'))
                or type(receipt.get('table_count')) is not int or receipt['table_count'] != len(definitions(self._social_config))):
            raise AidpProvisionPending('Waiting for the current generation bootstrap receipt and all Sonda tables', 'sync')
        publication = receipt.get('publication')
        try:
            if not isinstance(publication, dict) or any(publication[key] != self._social_config[key] for key in ('registry_id', 'generation_id')):
                raise ValueError()
            versions = json.loads(publication['versions_json'], object_pairs_hook=no_duplicate_keys)
            expected = set(definitions(self._social_config)) - EXCLUDED_VERSIONS
            revision = publication['config_revision']
            target = self._social_config['bootstrap_config_revision']
            if (set(versions) != expected or any(type(v) is not int or v < 0 for v in versions.values())
                    or type(revision) is not int or type(target) is not int or target < 1 or revision < target):
                raise ValueError()
            scope = {key: self._social_config[key] for key in ('registry_id', 'generation_id')}
            if publication['publication_id'] != fingerprint([scope, revision, versions]):
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise AidpProvisionPending('Sonda needs a coherent publication of the approved Huginn configuration', 'sync') from None
        resources['bootstrap_receipt_completed_at'] = receipt['completed_at']
        resources['bootstrap_publication_id'] = publication['publication_id']
        manifest['phase'] = 'metadata'
        self._write_module_manifest(workspace_key, manifest)

    def _catalog_descriptions(self):
        config = self._social_config
        catalog_key = str(self._catalog(config['catalog'])['key'])
        schemas = self._list('/schemas', params={'catalogKey': catalog_key}, phase='metadata')
        waiting = False
        for schema_name in (config['gold_schema'], config['control_schema']):
            matches = [r for r in schemas if self._resource_name(r) == schema_name]
            if len(matches) > 1:
                raise AidpProvisionError('Duplicate Sonda catalog schemas; metadata reconciliation stopped')
            if len(matches) != 1:
                raise AidpProvisionPending('Waiting for the bound Sonda catalog schema', 'metadata')
            schema_key = str(matches[0]['key'])
            tables = self._schema_tables(catalog_key, schema_key)
            for name, definition in definitions(config).items():
                if schema_name != config['gold_schema' if definition['layer'] == 'gold' else 'control_schema']:
                    continue
                matches = [r for r in tables if self._resource_name(r) == name]
                if len(matches) > 1:
                    raise AidpProvisionError('Duplicate Sonda catalog tables; metadata reconciliation stopped')
                if not matches:
                    raise AidpProvisionPending('Waiting for all bound Sonda catalog tables', 'metadata')
                description = definition['description']
                waiting = not self._describe_catalog_table(catalog_key, schema_key, name, str(matches[0]['key']), description) or waiting
        if waiting:
            raise AidpProvisionPending('Sonda catalog descriptions are being published', 'metadata')

    def _describe_catalog_table(self, catalog_key, schema_key, name, key, description):
        path = '/tables/' + quote(key, safe='')
        detail, headers = self._request('GET', path, include_headers=True, phase='metadata')
        expected = definitions(self._social_config).get(name)
        external = detail.get('externalTableDefinition') or {}
        if (not expected or (detail.get('key'), detail.get('displayName'), detail.get('catalogKey'), detail.get('schemaKey')) != (key, name, catalog_key, schema_key)
                or detail.get('tableType') != 'EXTERNAL' or detail.get('location', '').rstrip('/') != expected['location']
                or external.get('objectStorageLocationPath', '').rstrip('/') != expected['location']
                or str(external.get('externalTableDataFormat')).upper() != 'DELTA'):
            raise AidpProvisionError('The Sonda catalog identity, format or generation location differs')
        if str(detail.get('description') or '').strip():
            return True
        etag = headers.get('etag') or headers.get('ETag')
        self._request('PUT', path, payload={'description': description}, headers={'If-Match': etag} if etag else None, phase='metadata')
        return self._request('GET', path, phase='metadata').get('description') == description

    def _agent_artifacts(self, binding, *, investigator=False):
        filename = 'sonda_worker.py' if investigator else self._agent_entry_filename
        source = _render(investigator_source() if investigator else chat_source(), 'WORKER_BINDING' if investigator else 'CHAT_BINDING', binding)
        digest = hashlib.sha256(source).hexdigest()
        descriptor = json.dumps({'schema_version': 1, 'module_id': MODULE_ID,
            'agent_name': INVESTIGATOR if investigator else MODULE_ID, 'entry_file': filename, 'entry_sha256': digest,
            'registry_id': binding['registry_id'], 'generation_id': binding['generation_id'],
            'contract_sha256': binding['contract_sha256'], 'memory': 'AI_LAKEHOUSE',
            'role': 'private_investigator' if investigator else 'read_only_chat'}, sort_keys=True, separators=(',', ':')).encode()
        return source, descriptor, digest

    def _governance_agent_artifacts(self):
        workspace = str(self._workspace()['key'])
        return self._agent_artifacts(self._runtime_binding(workspace))

    def _ensure_activity_session(self,workspace_key,agent_key):
        path=f'/workspaces/{workspace_key}/agents/{quote(agent_key,safe="")}'
        desired={'name':'sondaActivityRef','description':'Opaque invocation admission reference',
            'isRequired':False,'isSystem':False,'shouldLog':False}
        def matches(detail):
            ref=((detail.get('sessionConfig') or {}).get('variables') or {}).get('sondaActivityRef')
            return (isinstance(ref,dict) and all(ref.get(key)==value for key,value in desired.items())
                and not any(ref.get(key) is not None for key in ('value','defaultValue','initialValue')))
        detail=self._request('GET',path,phase='agent')
        if matches(detail):
            return False
        variables=dict((detail.get('sessionConfig') or {}).get('variables') or {})
        variables['sondaActivityRef']=desired
        payload={key:detail[key] for key in ('displayName','description','entryFilePath','dependenciesFilePath','computeKey')}
        payload['sessionConfig']={**(detail.get('sessionConfig') or {}),'variables':variables}
        self._request('PUT',path,payload=payload,phase='agent')
        if not matches(self._request('GET',path,phase='agent')):
            raise AidpProvisionPending('The private Activity session reference is not ready','agent')
        return True

    def _ensure_bound_agent_source(self,workspace_key,compute_key,name,root,binding,ledger,*,private):
        """Create identity before the final source binding, then verify it before deployment."""
        configured=str(ledger.get('agent_key') or UUID(int=0))
        source,descriptor,digest=self._agent_artifacts({**binding,'activity_agent_id':configured},investigator=private)
        options={'repair_drift':True,'entry_filename':'sonda_worker.py' if private else self._agent_entry_filename,
            'description':'Private bounded Sonda case investigator' if private else 'Read-only Sonda Gold chat with scoped sources and native memory'}
        agent,altered=self._ensure_agent(workspace_key,compute_key,name,root,source,descriptor,
            expected_key=str(ledger.get('agent_key') or ''),**options)
        if configured!=agent:
            source,descriptor,digest=self._agent_artifacts({**binding,'activity_agent_id':agent},investigator=private)
            verified,updated=self._ensure_agent(workspace_key,compute_key,name,root,source,descriptor,expected_key=agent,**options)
            if verified!=agent:
                raise AidpProvisionConflict('The created Agent identity changed before deployment')
            altered=altered or updated
        return agent,altered,digest

    def _ensure_global_agent(self, workspace_key, manifest):
        compute, changed = self._ensure_agent_compute(workspace_key)
        compute_key = str(compute['key'])
        binding = {**manifest['config'], 'gold_query_compute_id': manifest['resources']['gold_query_compute_id']}
        specs = ((False, MODULE_ID, self._module_root + '/agent', manifest['resources']),
                 (True, INVESTIGATOR, self._module_root + '/investigator', manifest['resources'].setdefault('investigator', {})))
        self._ensure_workspace_layout(workspace_key, (self._module_root + '/investigator',))
        for private, name, root, ledger in specs:
            if ledger.get('agent_compute_key') and ledger['agent_compute_key'] != compute_key:
                raise AidpProvisionConflict('The recorded Sonda Agent compute differs; reconcile before redeploying')
            if private and not ledger.get('agent_key') and not ledger.get('create_operation_id'):
                if self._agents(workspace_key,name):
                    raise AidpProvisionConflict('An unrecorded private Sonda investigator already exists; reconcile its identity first')
                ledger['create_operation_id']=manifest['operation']['operation_id']
                self._write_module_manifest(workspace_key,manifest)
            agent,altered,digest=self._ensure_bound_agent_source(workspace_key,compute_key,name,root,binding,ledger,private=private)
            first_private=(private and ledger.get('create_operation_id')==manifest['operation']['operation_id']
                and not ledger.get('deployed_source_hash')
                and ledger.get('agent_source_hash') in (None,digest))
            ledger.update(agent_key=agent, agent_compute_key=compute_key, agent_source_hash=digest)
            self._write_module_manifest(workspace_key, manifest)
            altered=self._ensure_activity_session(workspace_key,agent) or altered
            component = {'status': 'installing' if first_private else manifest['status'],
                'operation': manifest['operation'], 'resources': ledger}
            try:
                deployment, deployed = self._ensure_global_agent_deployment(workspace_key, compute_key, agent, component, digest, agent_name=name)
                ledger['deployment_key'] = str(deployment.get('key') or deployment.get('id') or '')
                if not deployed and str(deployment.get('lifecycleState') or deployment.get('state')).upper()=='ACTIVE':
                    ledger['deployed_source_hash']=digest
                    if first_private:
                        for key in ('deployment_redeploy_requested','deployment_redeploy_baseline','deployment_redeploy_baseline_key'):
                            ledger.pop(key,None)
            finally:
                # A native Pending may have recorded a redeploy request/baseline.
                # Commit each ledger before another agent can interrupt the pass.
                self._write_module_manifest(workspace_key, manifest)
            changed = changed or altered or deployed
        self._write_module_manifest(workspace_key, manifest)
        return changed

    def _ensure_global_agent_permissions(self, workspace_key, manifest):
        changed = super()._ensure_global_agent_permissions(workspace_key, manifest)
        ledger = manifest['resources'].get('investigator') or {}
        if not ledger.get('agent_key'):
            raise AidpProvisionError('The private Sonda investigator is not recorded')
        users, groups = self._role_principal_ocids('AI_DATA_PLATFORM_ADMIN')
        admins = {('AI_DATA_PLATFORM_ADMIN', 'ROLE'), *((key, 'USER') for key in users), *((key, 'GROUP') for key in groups)}
        path = f"/workspaces/{workspace_key}/agents/{quote(ledger['agent_key'], safe='')}"
        permissions = self._list(path + '/permissions', phase='permissions')
        if any(r.get('isInherited') is True and self._permission_grantee(r) not in admins
               and self._permission_values(r) & {'USE', 'READ', 'ADMIN', 'MANAGE', 'ADMINISTRATOR'} for r in permissions):
            raise AidpProvisionError('A non-administrator inherits access to the private Sonda investigator')
        changed = self._reconcile_role_permissions_exact(path, 'assignAgentPermissionDetails', 'revokeAgentPermissionDetails',
            (('AI_DATA_PLATFORM_ADMIN', 'ADMIN', None),), include_columns=True, reject_inherited_editors=True,
            allowed_inherited_editors=admins, platform_admin_grantees=admins) or changed
        for suffix, folder in (('', True), ('/sonda_worker.py', False), ('/requirements.txt', False), ('/agent-manifest.json', False)):
            key = self._workspace_object_key(workspace_key, self._module_root + '/investigator' + suffix)
            changed = self._reconcile_role_permissions_exact(f'/workspaces/{workspace_key}/objects/{quote(key, safe="")}',
                'assignWorkspaceObjectPermissionDetails', 'revokeWorkspaceObjectPermissionDetails', (), inheritable=folder,
                reject_inherited_editors=True, allowed_inherited_editors=admins, platform_admin_grantees=admins) or changed
        return changed

    def _activate_social(self, workspace_key, manifest, _operation_type):
        fence = self._fence(manifest['config'])
        compute = manifest['resources']['gold_query_compute_id']
        # The final render includes the investigator key recorded in agent phase.
        accepted = fence['enabled'] is True and fence['integrated_acceptance'] is True
        _, changed = self._ensure_governance_job(workspace_key, compute, desired_enabled=accepted, paused=not accepted)
        if not accepted:
            manifest.update(enabled=False, phase='activation')
            self._write_module_manifest(workspace_key, manifest)
            raise AidpProvisionPending('Sonda is prepared and paused pending integrated operator acceptance', 'acceptance')
        if changed:
            raise AidpProvisionPending('The accepted five-minute Sonda schedule is being published', 'activation')
        manifest.update(status='active', phase='active', enabled=True, pack_version=load_lab_pack(MODULE_ID).pack_version)
        manifest['operation']['phase'] = 'complete'
        self._write_module_manifest(workspace_key, manifest)
