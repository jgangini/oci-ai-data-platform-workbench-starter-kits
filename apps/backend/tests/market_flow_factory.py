"""Local CLI bridge for the portable graph; never runs native setup or provisioning."""
import copy
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tomllib
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.model import DecisionModel
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.repository import Conflict, Journal, Scope
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.workflow import MarketWorkflow


class FrozenSnapshot:
    def __init__(self, value):
        self.value, self.revision = copy.deepcopy(value), 1
        self.scope = Scope(**value['scope'])

    def read(self):
        return copy.deepcopy(self.value), self.revision

    def write(self, value, etag):
        if etag != self.revision:
            raise Conflict('Local snapshot changed')
        self.value, self.revision = copy.deepcopy(value), self.revision + 1


class NoExternalEffects:
    def __getattr__(self, name):
        raise RuntimeError('Frozen node evaluation cannot execute connectors or native writes')


class ReplayFirstDraft:
    """Seed one recorded draft; review and any repair still use the counted model."""
    def __init__(self, actual, replay):
        self.actual, self.replay, self.consumed = actual, replay, False

    async def ask(self, name, schema, payload):
        if name == 'draft_report' and not self.consumed:
            encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
            if hashlib.sha256(encoded).hexdigest() != self.replay['input_sha256']:
                raise ValueError('Recorded draft does not match the current factual context')
            self.consumed = True
            return copy.deepcopy(self.replay['response'])
        return await self.actual.ask(name, schema, payload)


def checked_json(path, expected_sha256):
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise ValueError('Frozen evaluation input changed')
    return json.loads(data)


from market_model_bridge import StructuredBridge, update_model_observation


LEGACY_BRIDGE_SHA256='29be300f0b4c8d6162b6db975a7d2af820453977a831b3c198774f69a5bc44fb'


def build_graph(runtime):
    if hashlib.sha256(Path(__file__).with_name('market_model_bridge.py').read_bytes()).hexdigest()!=LEGACY_BRIDGE_SHA256:
        raise ValueError('The shared legacy model bridge changed; review and rebind the factory')
    if runtime['external_effects_allowed'] is not False:
        raise ValueError('This factory only supports local frozen evaluation')
    root = Path.cwd()
    config = tomllib.loads((root / 'aidp-agent.toml').read_text(encoding='utf-8'))
    evaluation = config['market_test']
    if hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != evaluation['factory_sha256']:
        raise ValueError('Review and rebind changed local evaluation code')
    fixture_path = (root / evaluation['journal_file']).resolve()
    binding_path = (root / evaluation['binding_file']).resolve()
    if not all(path.is_relative_to(root) for path in (fixture_path, binding_path)):
        raise ValueError('Frozen evaluation files must stay in the owning project')
    value = checked_json(fixture_path, evaluation['journal_sha256'])
    binding = checked_json(binding_path, evaluation['binding_sha256'])
    for key in ('model_id', 'region', 'compartment_id'):
        if config['model'][key] != binding[key]:
            raise ValueError('Local model binding differs from the configured runtime')
    decision = DecisionModel(StructuredBridge(runtime['model'], binding,
        observations_dir=root / '.aidp-local' / 'structured-responses'))
    if evaluation.get('draft_replay_file'):
        replay_path = (root / evaluation['draft_replay_file']).resolve()
        if not replay_path.is_relative_to(root):
            raise ValueError('Recorded draft must stay in the owning project')
        decision = ReplayFirstDraft(decision, checked_json(replay_path, evaluation['draft_replay_sha256']))
    flow = MarketWorkflow(Journal(FrozenSnapshot(value), value['budget_microusd']),
        decision, NoExternalEffects(),
        run_cap_microusd=100000, step_cap_microusd=20000, public_discovery=True,
        usage_cost_verified=True, clock=lambda: datetime.fromisoformat(evaluation['clock']))
    import market_contract_fixtures
    if hashlib.sha256(Path(market_contract_fixtures.__file__).read_bytes()).hexdigest() != evaluation['contract_factory_sha256']:
        raise ValueError('Review and rebind changed contract-fixture code')
    return market_contract_fixtures.attach_contract_cases(flow.build(runtime['checkpointer']), runtime, root, binding,
        snapshot_factory=FrozenSnapshot, bridge_factory=StructuredBridge)
