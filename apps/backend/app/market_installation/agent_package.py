"""Readable workspace package; no source discovery or secrets in generated code."""
import json
from app.lab_packs import LabPackError, load_lab_pack


FILES = ('__init__.py', 'workflow.py', 'workflow_actions.py', 'model.py', 'native.py', 'streaming.py', 'presentation.py', 'provider.py',
         'repository.py', 'research.py', 'execution.py', 'catalog.py', 'proposals.py', 'contracts.py',
         'connectors.py', 'finance.py', 'schema.py', 'schema.json', 'mcp_tools.json', 'prompts.json', 'mcp_payload.py', 'memory.py')


def agent_package(config):
    required = {'credential_name', 'identity_sha256', 'region', 'model_endpoint', 'model_id', 'compartment_id',
                'guardrails_config', 'tenant_id', 'generation_id', 'namespace', 'bucket', 'catalog',
                'framework_schema', 'gold_query_compute_id', 'budget_microusd',
                'run_cap_microusd', 'step_cap_microusd', 'timezone', 'usage_cost_verified', 'research_enabled'}
    if set(config) != required or type(config['research_enabled']) is not bool:
        raise ValueError('An exact reviewed runtime binding is required')
    if any(any(term in key.lower() for term in ('secret', 'password', 'private_key', 'token')) for key in config):
        raise ValueError('Runtime binding cannot contain credential material')
    if not 0 <= config['step_cap_microusd'] <= config['run_cap_microusd'] <= config['budget_microusd'] <= 5000000:
        raise ValueError('Configured limits exceed the approved Apify envelope')
    pack = load_lab_pack('ai_market_intelligence', require_available=False)
    declared = {'notebooks/agent/ai_market_intelligence/' + name for name in FILES}
    declared.update({'notebooks/agent/market_intelligence.py', 'notebooks/agent/requirements.txt'})
    if {asset.name for asset in pack.runtime_files} != declared:
        raise LabPackError('Market package must declare every reviewed runtime file')
    assets = {asset.name.removeprefix('notebooks/agent/'): asset.read_bytes() for asset in pack.runtime_files}
    source = assets.pop('market_intelligence.py')
    assets['ai_market_intelligence/runtime.json'] = json.dumps(config, ensure_ascii=False, sort_keys=True).encode()
    # Workbench GET decodes JSON objects; canonical bytes preserve semantic/hash verification.
    for name, value in list(assets.items()):
        if name.endswith('.json'):
            assets[name] = json.dumps(json.loads(value), sort_keys=True, separators=(',', ':')).encode()
    compile(source, 'market_intelligence.py', 'exec')
    for name, content in assets.items():
        if name.endswith('.py'):
            compile(content, name, 'exec')
    return source, assets
