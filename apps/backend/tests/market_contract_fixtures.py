"""Node-contract adapter for hash-bound, explicitly synthetic local fixtures."""
import copy
from datetime import datetime
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from langchain_core.runnables import RunnableLambda

from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.model import DecisionModel
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.provider import ObservationResult
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.repository import Journal
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.workflow import MarketWorkflow


def fixture_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False, separators=(',', ':')).encode()).hexdigest()


def load_contract_fixture(root, selector, node, state):
    if type(selector) is not dict or set(selector) != {'path', 'sha256'}:
        raise ValueError('A contract fixture needs its exact path and SHA-256')
    root = Path(root).resolve()
    relative = Path(selector['path'])
    allowed = root / '.aidp-local/fixtures/contracts046'
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('Contract fixtures must stay in their reviewed project directory')
    path = (root / relative).resolve()
    if not path.is_relative_to(allowed) or path.suffix != '.json':
        raise ValueError('Contract fixtures must stay in their reviewed project directory')
    for ancestor in (root / relative, *(root / relative).parents):
        if ancestor == root:
            break
        if ancestor.is_symlink() or getattr(ancestor, 'is_junction', lambda: False)():
            raise ValueError('Contract fixtures cannot use linked paths')
    raw = path.read_bytes()
    if len(raw) > 4 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != selector['sha256']:
        raise ValueError('Contract fixture changed or exceeds its size limit')
    value = json.loads(raw)
    return validated_contract_binding(value, node, state)


def validated_contract_binding(value, node, state):
    required = {'version', 'synthetic', 'node', 'journal', 'clock', 'repeat',
                'input_sha256', 'source_test_refs', 'settings'}
    if (type(value) is not dict or set(value) != required or value['version'] != 1
            or value['synthetic'] is not True or value['node'] != node
            or type(value['repeat']) is not int or value['repeat'] not in (1, 2)
            or value['input_sha256'] != fixture_digest(state)
            or not value['source_test_refs']):
        raise ValueError('Contract fixture does not bind this synthetic node input')
    return value


class RecordedProvider:
    """Use only the injected consume-once service boundary, never an MCP client."""
    def __init__(self, services):
        self.services = services

    async def start(self, call):
        return await self.services.call('mcp', 'start', {'call': call})

    async def read(self, run_id):
        return await self.services.call('mcp', 'read', {'run_id': run_id})

    async def observations(self, receipt, call, *, allow_partial_pages=False):
        arguments = {'receipt': receipt, 'call': call}
        if allow_partial_pages:
            arguments['allow_partial_pages'] = True
        value = await self.services.call('mcp', 'observations', arguments)
        if type(value) is not dict or set(value) != {'items', 'cost', 'coverage'}:
            raise ValueError('Recorded observations must preserve the provider result contract')
        return ObservationResult(value['items'], value['cost'], value['coverage'])


def journal_observation(value):
    tables = value['tables']
    tools = list(tables['ai_mi_tool_runs'].values())
    return {'table_counts': {name: len(rows) for name, rows in tables.items()},
            'run_ids': sorted(tables['ai_mi_research_runs']),
            'report_ids': sorted(tables['ai_mi_reports']),
            'operation_states': sorted(item['state'] for item in value['operations'].values()),
            'provider_run_ids': sorted(item['provider_run_id'] for item in tools if item['provider_run_id']),
            'known_cost_microusd': sum(item['spent_microusd'] or 0 for item in tools),
            'unknown_cost_tools': sum(item['spent_microusd'] is None for item in tools),
            'reserved_amount_microusd': sum(item['reserved_microusd'] for item in value['operations'].values()),
            'active_schedules': sum(item['status'] == 'active' for item in tables['ai_mi_schedules'].values())}


async def run_contract_node(root, selector, node, state, runtime, binding, snapshot_factory, bridge_factory, config=None):
    if runtime.get('external_effects_allowed') is not False:
        raise ValueError('Contract fixtures permit only local injected services')
    fixture = load_contract_fixture(root, selector, node, state)
    journal = Journal(snapshot_factory(fixture['journal']), fixture['journal']['budget_microusd'])
    model = DecisionModel(bridge_factory(runtime['model'], binding,
        observations_dir=(Path(root) / '.aidp-local' / 'contract-model-responses'
                          if hasattr(runtime['model'], '_complete') else None)))
    settings = fixture['settings']
    flow = MarketWorkflow(journal, model, RecordedProvider(runtime['services']),
        run_cap_microusd=settings['run_cap_microusd'], step_cap_microusd=settings['step_cap_microusd'],
        max_steps=settings['max_steps'], public_discovery=False, usage_cost_verified=True,
        clock=lambda: datetime.fromisoformat(fixture['clock']))
    graph = flow.build(runtime['checkpointer'])
    before = journal_observation(journal.read()[0])
    calls_before = len(runtime['services'].calls)
    combined, observations = {}, {}
    class RecordedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            instant = datetime.fromisoformat(fixture['clock'])
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

    # Direct synthetic cases run in an isolated worker; admission uses the same frozen clock.
    with patch('app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.execution.datetime', RecordedClock):
        for index in range(fixture['repeat']):
            updates = await graph.nodes[node].ainvoke(copy.deepcopy(state), config=config)
            combined.update(updates)
            observations[str(index)] = journal_observation(journal.read()[0])
    observed = {'version': 1, 'synthetic': True,
        'node': node, 'before': before, 'after_each': observations,
        'service_calls': runtime['services'].calls[calls_before:], 'projection': 'not_configured'}
    if node == 'normalize_evidence':
        observed['normalized_evidence'] = flow.research.evidence(state['run_id'])
    return {**combined, 'contract_observed': observed}


class ContractGraph:
    def __init__(self, graph, runtime, root, binding, snapshot_factory, bridge_factory):
        self.graph = graph
        self.nodes = {}
        for name, actual in graph.nodes.items():
            self.nodes[name] = self._node(name, actual, runtime, root, binding, snapshot_factory, bridge_factory)

    @staticmethod
    def _node(name, actual, runtime, root, binding, snapshot_factory, bridge_factory):
        async def invoke(state, config=None):
            if 'contract_fixture' not in state:
                return await actual.ainvoke(state, config=config)
            state = copy.deepcopy(state)
            selector = state.pop('contract_fixture')
            return await run_contract_node(root, selector, name, state, runtime, binding, snapshot_factory, bridge_factory, config)
        return RunnableLambda(invoke)

    def __getattr__(self, name):
        return getattr(self.graph, name)


def attach_contract_cases(graph, runtime, root, binding, *, snapshot_factory, bridge_factory):
    return ContractGraph(graph, runtime, root, binding, snapshot_factory, bridge_factory)
