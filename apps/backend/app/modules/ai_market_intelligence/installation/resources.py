"""Read explicit, packaged AIDP resource declarations for the current module."""
import json
from pathlib import Path

MODULE_ROOT = Path(__file__).resolve().parents[1]

def resource(kind):
    if kind not in {'agents', 'compute'}:
        raise ValueError('Unsupported Market Intelligence resource kind')
    path = MODULE_ROOT / 'aidp/workspaces/default/agent_flow' / kind / 'ai_market_intelligence.json'
    return json.loads(path.read_text(encoding='utf-8'))
