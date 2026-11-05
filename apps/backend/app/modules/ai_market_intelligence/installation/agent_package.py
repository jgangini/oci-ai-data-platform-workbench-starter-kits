"""Package the single reviewed portable agent; keep runtime secrets outside source."""
from app.packages.catalog import PackageError, load_package
from .agent_source import assemble_agent, MODULES
from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.config import validate_market_config

SOURCE_ROOT = 'aidp/workspaces/default/files/agents/ai_market_intelligence/'
FILES = tuple(module + '.py' for module in MODULES) + ('prompts.json', 'mcp_tools.json', 'factory.py', 'model_bridge.py')


def authoring_files():
    return {name: raw for name, raw in _reviewed_assets().items()
            if name.startswith('ai_market_intelligence/')}


def _reviewed_assets():
    pack = load_package('ai_market_intelligence', require_available=False)
    declared = {SOURCE_ROOT + 'ai_market_intelligence/' + name for name in FILES}
    declared.update({SOURCE_ROOT + 'market_intelligence.py', SOURCE_ROOT + 'requirements.txt'})
    if len(pack.runtime_files) != len(declared) or {asset.name for asset in pack.runtime_files} != declared:
        raise PackageError('Market package must declare every reviewed runtime file')
    return {asset.name.removeprefix(SOURCE_ROOT): asset.read_bytes() for asset in pack.runtime_files}


def validate_agent_config(config):
    validate_market_config(config)
    if any(any(term in key.lower() for term in ('secret', 'password', 'private_key', 'token')) for key in config):
        raise ValueError('Runtime binding cannot contain credential material')

def agent_package(config):
    validate_agent_config(config)
    assets = _reviewed_assets()
    files = {name.removeprefix('ai_market_intelligence/'): content for name, content in assets.items() if name.startswith('ai_market_intelligence/')}
    return assemble_agent(files, assets['market_intelligence.py'], config), {'requirements.txt': assets['requirements.txt']}
