"""Readable workspace package; no source discovery or secrets in generated code."""
from app.lab_packs import LabPackError, load_lab_pack
from .agent_source import assemble_agent, V2_MODULES
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.v2_config import validate_v2_config


FILES = ('__init__.py', 'runtime_core.py', 'media.py', 'actor_profiles.py', 'social.py', 'social_search.py', 'workflow.py', 'workflow_actions.py', 'model.py', 'native.py', 'streaming.py', 'presentation.py', 'provider.py',
         'repository.py', 'research.py', 'execution.py', 'catalog.py', 'proposals.py', 'contracts.py',
         'connectors.py', 'serp.py', 'finance.py', 'schema.py', 'schema.json', 'mcp_tools.json', 'prompts.json', 'mcp_payload.py', 'memory.py')


V2_FILES=tuple(module+'.py' for module in V2_MODULES)+('v2_prompts.json','mcp_tools.json')


def validate_agent_config(config, version):
    required = {'credential_name', 'identity_sha256', 'region', 'model_endpoint', 'model_id', 'compartment_id',
                'guardrails_config', 'tenant_id', 'generation_id', 'namespace', 'bucket', 'catalog',
                'framework_schema', 'gold_query_compute_id', 'budget_microusd',
                'run_cap_microusd', 'step_cap_microusd', 'timezone', 'usage_cost_verified', 'research_enabled'}
    if version not in {'legacy','v2'}:
        raise ValueError('Unknown Market Intelligence runtime version')
    if version=='v2':
        validate_v2_config(config)
    elif set(config)!=required or type(config['research_enabled']) is not bool:
        raise ValueError('An exact reviewed runtime binding is required')
    if any(any(term in key.lower() for term in ('secret', 'password', 'private_key', 'token')) for key in config):
        raise ValueError('Runtime binding cannot contain credential material')
    if version=='legacy' and not 0 <= config['step_cap_microusd'] <= config['run_cap_microusd'] <= config['budget_microusd'] <= 5000000:
        raise ValueError('Configured limits exceed the approved Apify envelope')


def agent_package(config, *, version='legacy'):
    validate_agent_config(config,version)
    pack = load_lab_pack('ai_market_intelligence', require_available=False)
    declared = {'notebooks/agent/ai_market_intelligence/' + name for name in set(FILES)|set(V2_FILES)}
    declared.update({'notebooks/agent/market_intelligence.py', 'notebooks/agent/requirements.txt',
                     'notebooks/agent/requirements-v2.txt'})
    if {asset.name for asset in pack.runtime_files} != declared:
        raise LabPackError('Market package must declare every reviewed runtime file')
    assets = {asset.name.removeprefix('notebooks/agent/'): asset.read_bytes() for asset in pack.runtime_files}
    files = {name.removeprefix('ai_market_intelligence/'): content for name, content in assets.items()
             if name.startswith('ai_market_intelligence/')}
    source = assemble_agent(files, assets['market_intelligence.py'], config, version=version)
    return source, {'requirements.txt': assets['requirements-v2.txt' if version=='v2' else 'requirements.txt']}
