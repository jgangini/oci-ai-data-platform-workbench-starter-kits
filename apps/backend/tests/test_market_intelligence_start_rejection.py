"""Only a proven pre-start rejection can release an uncertain reservation."""
import asyncio
import json
from types import SimpleNamespace

import pytest

pytest.importorskip('langchain_core', reason='Native tool exception is exercised by the runtime CI job')
from langchain_core.tools import ToolException

from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.provider import ApifyProvider
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.execution import ProviderStartRejected, execute_selected
from test_market_intelligence_execution import prepared


CALL = {'actor': 'apidojo/twitter-scraper-lite', 'callOptions': {'build': '0.0.1048'}}
REJECTION = ('Failed to call Actor \'apidojo/twitter-scraper-lite\': Build with number "0.0.1048" was not found.\n'
    'Please verify the Actor name, input parameters, and ensure the Actor exists.')
NATIVE_ERROR = "MCP tool 'call-actor' invocation failed: " + REJECTION


@pytest.mark.parametrize('form',['dictionary','sdk_string','exception'])
@pytest.mark.parametrize('field,expected',[('searchTerms','searchTerms'),('apiToken',None)])
def test_known_input_rejection_preserves_only_safe_classification_and_field(form,field,expected):
    message=(f"Failed to call Actor '{CALL['actor']}': Input is not valid: Field input.{field} is required. "
        'Rejected value: private-customer-value'
        '\nPlease ensure the input is correct and matches the Actor\'s input schema.\nInput schema:\n```json\n'
        '{"secret":"never-persist-this-schema"}\n```')
    tool=(RejectingTool(ToolException(message)) if form=='exception' else
          ReturningTool({'error':message} if form=='dictionary' else "MCP tool 'call-actor' invocation failed: "+message))
    provider=ApifyProvider({key:tool for key in ('call-actor','get-actor-run','get-dataset-items')})
    with pytest.raises(ProviderStartRejected) as caught:
        asyncio.run(provider.start(CALL))
    assert caught.value.diagnostic==({'reason':'invalid_input','input_field':expected} if expected else {'reason':'invalid_input'})
    assert 'private-customer' not in json.dumps(caught.value.diagnostic)
    assert 'schema' not in json.dumps(caught.value.diagnostic)


class RejectingTool:
    def __init__(self, error):
        self.error = error
        self.calls = 0

    async def ainvoke(self, arguments):
        self.calls += 1
        raise self.error


class ReturningTool:
    def __init__(self, value):
        self.value, self.calls = value, 0

    async def ainvoke(self, arguments):
        self.calls += 1
        return self.value


@pytest.mark.parametrize('value,rejected', [
    ({'error': REJECTION}, True),
    (NATIVE_ERROR, True), (REJECTION, False),
    (NATIVE_ERROR.replace('0.0.1048', 'other-build'), False),
    (NATIVE_ERROR.replace('apidojo/twitter-scraper-lite', 'other/actor'), False),
    (NATIVE_ERROR.replace("tool 'call-actor'", "tool 'get-actor-run'"), False),
    ('prefix ' + NATIVE_ERROR, False), (NATIVE_ERROR + '\n', False),
    (NATIVE_ERROR[:-1], False), (NATIVE_ERROR + ' runId=possible-run', False),
    (json.dumps(NATIVE_ERROR), False), ({'content': NATIVE_ERROR}, False),
    ([NATIVE_ERROR], False), ({'_meta': {'error': NATIVE_ERROR}}, False),
    ({'error': NATIVE_ERROR}, False),
    ("MCP tool 'call-actor' invocation failed: timeout", False),
    ("MCP tool 'call-actor' invocation failed: unauthorized", False),
    ("MCP tool 'call-actor' invocation failed: unhandled errors in a TaskGroup (1 sub-exception)", False),
    ({'error': REJECTION.replace('0.0.1048', 'another-build')}, False),
    ({'error': REJECTION.replace('apidojo/twitter-scraper-lite', 'another/actor')}, False),
    ({'error': 'prefix ' + REJECTION}, False), ({'error': REJECTION + '\n'}, False),
    ({'error': REJECTION[:-1]}, False), ({'error': REJECTION, 'runId': 'possible-run'}, False),
    ({'error': REJECTION, 'status': 'RUNNING'}, False), ({'error': REJECTION, '_meta': {}}, False),
    ({'content': [{'type': 'text', 'text': REJECTION}], 'isError': True}, False),
    ({'structuredContent': {'error': REJECTION}}, False),
    ({'content': [{'type': 'text', 'text': json.dumps({'error': REJECTION})}]}, False),
    (SimpleNamespace(model_dump=lambda **kwargs: {'error': REJECTION}), False),
    (json.dumps({'error': REJECTION}), False),
    *[({'error': value}, False) for value in ('', None, False, {})],
])
def test_only_exact_raw_sdk_error_forms_can_prove_build_rejection(value, rejected):
    tool = ReturningTool(value)
    provider = ApifyProvider({key: tool for key in ('call-actor', 'get-actor-run', 'get-dataset-items')})
    ledger, run, source = prepared()
    key = ledger.reserve(run, source, None, CALL, 10000, 'Bounded public search',
        authorization_hash=ledger.authorize(run, source, None)['authorization_hash'])
    with pytest.raises(ProviderStartRejected if rejected else RuntimeError):
        asyncio.run(execute_selected(ledger, key, CALL, provider.start))
    saved = ledger.journal.read()[0]['operations'][key]
    assert saved['state'] == ('failed' if rejected else 'uncertain')
    assert saved['spent_microusd'] == (0 if rejected else None) and 'provider_run_id' not in saved
    assert tool.calls == 1
    assert asyncio.run(execute_selected(ledger, key, CALL, provider.start))['state'] == 'reconcile'
    assert tool.calls == 1


@pytest.mark.parametrize('name', ['get-actor-run', 'get-dataset-items'])
@pytest.mark.parametrize('value', [{'error': REJECTION}, NATIVE_ERROR])
def test_read_errors_cannot_be_classified_as_rejected_starts(name, value):
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.provider import ProviderMCPError
    tool = ReturningTool(value)
    provider = ApifyProvider({key: tool for key in ('call-actor', 'get-actor-run', 'get-dataset-items')})
    if type(value) is str:
        assert asyncio.run(provider.invoke(name, CALL)) == value
    else:
        with pytest.raises(ProviderMCPError):
            asyncio.run(provider.invoke(name, CALL))


@pytest.mark.parametrize('error, rejected', [(ToolException(REJECTION), True),
    (ToolException(REJECTION.replace('0.0.1048', 'other-build')), False),
    (RuntimeError(REJECTION), False), (ToolException('timeout after possible start'), False)])
def test_rejection_requires_exact_actor_build_and_native_tool_exception(error, rejected):
    tool = RejectingTool(error)
    provider = ApifyProvider({key: tool for key in ('call-actor', 'get-actor-run', 'get-dataset-items')})
    ledger, run, source = prepared()
    key = ledger.reserve(run, source, None, CALL, 10000, 'Bounded public search',
        authorization_hash=ledger.authorize(run, source, None)['authorization_hash'])
    with pytest.raises(ProviderStartRejected if rejected else RuntimeError):
        asyncio.run(execute_selected(ledger, key, CALL, provider.start))
    saved = ledger.journal.read()[0]['operations'][key]
    assert tool.calls == 1 and 'provider_run_id' not in saved
    assert saved['state'] == ('failed' if rejected else 'uncertain')
    assert saved.get('spent_microusd') == (0 if rejected else None)


def test_rejection_cannot_release_an_identified_run_or_a_different_permit():
    ledger, run, source = prepared()
    key = ledger.reserve(run, source, None, CALL, 10000, 'Bounded public search',
        authorization_hash=ledger.authorize(run, source, None)['authorization_hash'])
    permit = ledger.claim_start(key)
    with pytest.raises(ValueError, match='another permit'):
        ledger.reject_start(key, 'wrong', 'build_not_found')
    ledger.record_provider(key, permit, 'identified-run', 'running', {})
    with pytest.raises(ValueError, match='provider run'):
        ledger.reject_start(key, permit, 'build_not_found')
    assert ledger.journal.read()[0]['operations'][key]['state'] == 'running'


@pytest.mark.parametrize('failed_actor', ['apify/website-content-crawler', 'apidojo/twitter-scraper-lite'])
@pytest.mark.parametrize('sdk_form', ['root_dictionary', 'root_string'])
def test_graph_continues_after_known_rejection_without_repeating_it_or_losing_evidence(failed_actor, sdk_form):
    from test_market_intelligence_workflow import configured, turn
    flow, model, provider, graph = configured()
    original = provider.start
    calls = []

    async def start(call):
        calls.append(call)
        if call['actor'] != failed_actor:
            return await original(call)
        message = (f"Failed to call Actor '{failed_actor}': Build with number \"{call['callOptions']['build']}\" was not found.\n"
                   'Please verify the Actor name, input parameters, and ensure the Actor exists.')
        tool = ReturningTool({'error': message} if sdk_form == 'root_dictionary'
                             else "MCP tool 'call-actor' invocation failed: " + message)
        native = ApifyProvider({key: tool for key in ('call-actor', 'get-actor-run', 'get-dataset-items')})
        return await native.start(call)

    provider.start = start
    result = turn(graph, 'rejected-source-' + failed_actor.split('/')[0])
    records = flow.records()
    value, _ = flow.journal.read()
    failed = next(row for row in records.rows('ai_mi_tool_runs') if row['state'] == 'failed')
    operation = value['operations'][failed['tool_run_id']]
    assert len(calls) == 2 and sum(call['actor'] == failed_actor for call in calls) == 1
    assert operation['start_rejection'] == 'build_not_found' and operation['spent_microusd'] == 0
    assert not operation.get('provider_run_id')
    evidence = flow.research.evidence(result['run_id'])
    report = records.get('ai_mi_reports', result['response']['report_id'])
    assert len(evidence) == 1 and evidence[0]['url'] in report['markdown']
    assert json.loads(report['cited_evidence_ids_json']) == [evidence[0]['evidence_id']]
    assert 'No se pudo iniciar la búsqueda' in report['markdown']
    assert 'No es una búsqueda sin resultados' in report['limitations_json']
    plans = [payload for name, payload in model.calls if name == 'plan_research']
    if failed_actor == 'apify/website-content-crawler':
        assert failed['source_key'] not in {item['source_key'] for item in plans[-1]['catalog']}
        assert any('No se pudo iniciar la búsqueda' in limit for limit in plans[-1]['provider_limitations'])
    before = flow.journal.read()[0]
    replay = asyncio.run(flow.execute_step({'tool_id': failed['tool_run_id'], 'call': next(call for call in calls if call['actor'] == failed_actor)}))
    assert replay['route'] == 'reconcile_step'
    assert asyncio.run(flow.reconcile_step({'tool_id': failed['tool_run_id']}))['route'] == 'plan_research'
    after = flow.journal.read()[0]
    assert len(calls) == 2 and after['revision'] == before['revision'] + 1
    assert {key: value for key, value in after.items() if key != 'revision'} == {
        key: value for key, value in before.items() if key != 'revision'}


def test_explicit_failed_source_does_not_expand_to_other_registered_sources():
    from test_market_intelligence_repository import SCOPE
    from test_market_intelligence_workflow import configured, turn
    flow, model, provider, graph = configured()
    original = model.ask

    async def select_x(name, schema, payload):
        answer = await original(name, schema, payload)
        if name == 'understand':
            answer['selected_source_key'] = SCOPE.key('x')
        return answer

    calls = []

    async def reject(call):
        calls.append(call)
        raise ProviderStartRejected()

    model.ask, provider.start = select_x, reject
    for index in range(3):
        result = turn(graph, 'explicit-failed-source-' + str(index), 'Busca solo en X opiniones de D1 Colombia')
        assert result['pending'] is None and result['reason'] == 'unresolved'
        assert 'No se pudo iniciar la búsqueda' in result['response']['markdown']
        assert 'primero debe revisarse su disponibilidad' in result['response']['markdown']
        assert 'otro periodo' not in result['response']['markdown']
        assert 'suggestion' not in result['response']
    assert len(calls) == 3 and all(call['actor'] == 'apidojo/twitter-scraper-lite' for call in calls)
    assert flow.records().rows('ai_mi_documents') == []
    assert flow.records().rows('ai_mi_schedule_proposals') == flow.records().rows('ai_mi_schedules') == []
    assert 'detect_opportunities' not in [name for name, _ in model.calls]


def test_valid_empty_searches_can_still_offer_scheduling_after_real_recurrence():
    from test_market_intelligence_workflow import configured, turn
    flow, model, provider, graph = configured()
    original = model.ask

    async def empty_assessment(name, schema, payload):
        if name == 'assess_evidence':
            assert not payload['evidence']
            return {'assessments': [], 'continue_research': False, 'reason': 'Valid empty fixture', 'gap': ''}
        return await original(name, schema, payload)

    async def empty_result(receipt, call):
        return [], 0

    model.ask, provider.observations = empty_assessment, empty_result
    for index in range(3):
        result = turn(graph, 'valid-empty-recurrence-' + str(index))
        assert result['reason'] == 'no_data'
    assert len(provider.starts) == 3 and result['pending']['kind'] == 'schedule'
    assert len(flow.records().rows('ai_mi_schedule_proposals')) == 1
    assert flow.records().rows('ai_mi_schedules') == []


def test_partial_evidence_survives_rejection_and_remains_eligible_for_schedule_proposal():
    from test_market_intelligence_workflow import configured, turn
    flow, _, provider, graph = configured()
    original = provider.start

    async def reject_x(call):
        if call['actor'] == 'apidojo/twitter-scraper-lite':
            raise ProviderStartRejected()
        return await original(call)

    provider.start = reject_x
    for index in range(3):
        result = turn(graph, 'partial-rejection-recurrence-' + str(index))
        assert flow.research.evidence(result['run_id'])
    assert result['pending']['kind'] == 'schedule'
    assert len(flow.records().rows('ai_mi_schedule_proposals')) == 1
    assert flow.records().rows('ai_mi_schedules') == []


def test_claim_time_limit_is_not_misreported_as_uncertain_start(monkeypatch):
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence import execution
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.workflow import MarketWorkflow
    ledger, run, source = prepared()
    key = ledger.reserve(run, source, None, CALL, 10000, 'Bounded public search',
        authorization_hash=ledger.authorize(run, source, None)['authorization_hash'])
    before = ledger.journal.read()[0]
    tool = ReturningTool({'runId': 'must-not-start', 'status': 'SUCCEEDED'})
    provider = ApifyProvider({name: tool for name in ('call-actor', 'get-actor-run', 'get-dataset-items')})
    flow = MarketWorkflow(ledger.journal, None, provider)
    monkeypatch.setattr(execution, 'run_elapsed_seconds', lambda *args: 301)
    outcome = asyncio.run(flow.execute_step({'run_id': run, 'tool_id': key, 'call': CALL}))
    assert outcome == {'reason': 'time', 'route': 'draft_report'}
    assert tool.calls == 0 and ledger.journal.read()[0] == before
