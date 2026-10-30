"""Study semantics with deterministic inference callbacks, never cloud or Actor calls."""
import asyncio
import copy
import json
from pathlib import Path
from uuid import UUID

import pytest

from app import sonda_contract as contract
from app.labs.ai_social_network.notebooks.agent import sonda_study as core
from app.labs.ai_social_network.notebooks import sonda_delta as gold

ROOT = Path(__file__).resolve().parents[3]
NOW = '2026-01-01T00:30:00Z'


def fixture(name):
    return json.loads((ROOT/'contracts/sonda/v1/fixtures'/name).read_text(encoding='utf-8'))


class Model:
    identity = {'model_id':'fixture-model', 'model_revision':'fixture.1', 'prompt_version':'fixture.1'}

    def __init__(self, *, research=False, propose=False, bad_quotes=False, error=False, bad_numbers=False, target=None):
        self.calls = []
        self.research, self.propose = research, propose
        self.bad_quotes, self.error, self.bad_numbers, self.target = bad_quotes, error, bad_numbers, target

    async def __call__(self, role, payload):
        assert role in ('evaluate', 'plan', 'report', 'propose')
        self.calls.append((role, copy.deepcopy(payload)))
        if role == 'evaluate':
            if self.error:
                raise RuntimeError('Sensitive provider diagnostic must not escape')
            text = payload['observation']['text']
            return {'score':0.95 if 'price' in text.lower() else 0.05, 'reason':'Evidence matches the labelled example.',
                    'quotes':['invented quotation'] if self.bad_quotes else [text]}
        if role == 'plan':
            query_id = fixture('01-configuration.json')['payload']['queries'][1]['query_id']
            return {'requests':[{'step':'supporting-evidence', 'query_id':query_id, 'purpose':'Seek supporting evidence.',
                     'target_override':self.target, 'max_items':5}] if self.research else [], 'propose':self.propose}
        if role == 'report':
            return {'title':'Informe de evidencia', 'summary':'There are 9999999 relevant documents.' if self.bad_numbers
                    else 'La evidencia disponible se revisó contra el criterio aprobado.'}
        record = copy.deepcopy(payload['configuration']['criteria'][0])
        record['threshold'] = 0.85
        # A malicious model cannot give itself operator approval.
        record.update(origin='operator', status='active', approval={'status':'approved','decided_by':'model','decided_at':NOW})
        return {'changes':[{'operation':'update', 'change':{'target_kind':'criterion','record':record},
                            'rationale':'Propuesta pendiente de revisión humana.'}]}


def case_work(phase='report'):
    """Independent typed fixture, not expectations calculated by the function under test."""
    config = fixture('01-configuration.json')
    captured = fixture('02-observations.json')['payload']
    criterion = contract.Criterion.model_validate(config['payload']['criteria'][0]).model_dump(mode='json')
    from datetime import datetime, timedelta, timezone
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    seconds = criterion['case_window_hours'] * 3600
    timestamp = datetime.fromisoformat(captured['observations'][0]['observed_at'].replace('Z','+00:00'))
    start = epoch + timedelta(seconds=((timestamp-epoch)//timedelta(seconds=seconds))*seconds)
    start=contract.canonical_utc(start.isoformat())
    case=contract.sha256(contract.encoded([criterion['criterion_id'],criterion['revision'],criterion['case_scope_key'],start]))
    work = {'scope':{key:config[key] for key in ('contract_version','contract_sha256','registry_id','generation_id')},
        'investigation_id':case, 'criterion':criterion, 'config_revision':1, 'window_start':start,
        'phase':phase, 'input_revision':core.digest(['published-work', 1]), 'requests_complete':True,
        'approved_query_ids':[row['query_id'] for row in config['payload']['queries']],
        'requests':[], 'budget_round':fixture('09-budget-round-open.json'), 'report_revision':0}
    if phase == 'evaluate':
        work['pending_inputs'] = [{'observation':row, 'config_revision':1,
            'source_id':captured['source_id'], 'query_id':captured['query_id']} for row in captured['observations']]
    else:
        evaluations=[]
        for observation,score in zip(captured['observations'],(0.95,0.05)):
            quote=observation['text']
            evidence=contract.Evidence.model_validate({'evidence_id':contract.sha256(contract.encoded([observation['observation_key'],quote])),
                'observation_key':observation['observation_key'],'document_key':observation['document_key'],
                'url':observation['url'],'quote':quote,'captured_at':observation['observed_at']}).model_dump(mode='json')
            value={'observation_key':observation['observation_key'],'criterion_id':criterion['criterion_id'],
                'criterion_revision':criterion['revision'],'config_revision':1,**Model.identity,
                'rule_sha256':contract.sha256(contract.encoded(criterion)),'score':score,'threshold':criterion['threshold'],
                'decision':'relevant' if score>=criterion['threshold'] else 'not_relevant','reason':'Labelled fixture verdict.',
                'evidence':[evidence],'evaluated_at':NOW}
            value['evaluation_key']=contract.evaluation_fingerprint(value)
            evaluations.append(contract.Evaluation.model_validate(value).model_dump(mode='json'))
        evidence=[row for evaluation in evaluations for row in evaluation['evidence']]
        indicators=[{'key':key,'label':key.replace('_',' '),'value':value,'unit':'documents',
            'period_start':None,'period_end':None,'evidence_ids':[row['evidence_id'] for row in evidence],
            'method':'count_distinct_documents','population_count':2,'missing_count':0}
            for key,value in (('documents','2'),('relevant_documents','1'))]
        work.update(evaluations=evaluations, evidence=evidence, indicators=indicators,
            facts={'distinct_documents':2, 'distinct_observations':2, 'relevant_documents':1, 'missing_evaluations':0},
            query_ids=[captured['query_id']])
    return work


def run_case(work=None, *, model=None, state=None, results=None, config=None, now=NOW):
    return asyncio.run(core.study_case(config or fixture('01-configuration.json'), work or case_work(), state or {},
        results or [], model=model or Model(), now=now))


def test_published_source_limits_bound_all_queries_in_the_same_plan():
    work=case_work()
    limits=json.loads((ROOT/'contracts/sonda-source-limits/v1/fixtures/01-initial.json').read_bytes())
    limits['policies'][0].update(max_items_per_execution=1,max_cost_per_round_microusd=None,max_starts_per_round=1)
    work['source_limits']=limits
    class TwoQueries(Model):
        async def __call__(self,role,payload):
            if role=='plan':
                queries=fixture('01-configuration.json')['payload']['queries'][:2]
                return {'requests':[{'step':'query-'+str(i),'query_id':query['query_id'],
                    'purpose':'Corroborar evidencia','max_items':5} for i,query in enumerate(queries)],'propose':False}
            return await super().__call__(role,payload)
    result=run_case(work,model=TwoQueries())
    assert len(result['investigation_requests'])==1
    assert result['investigation_requests'][0]['max_items']==1


def test_legacy_worded_step_cannot_repeat_a_completed_query_target():
    first=run_case(model=Model(research=True))['investigation_requests'][0]
    first['request_id']=str(UUID(int=8989))
    work=case_work()
    work['requests']=[{'request':first,'status':'completed','observations_committed':True}]
    result=run_case(work,model=Model(research=True))
    assert result['investigation_requests']==[]


def test_case_evaluation_commits_before_reporting_and_gold_keys_prevent_new_inference():
    model, work = Model(), case_work('evaluate')
    result = run_case(work, model=model)
    assert len(result['evaluations']) == 2 and not result['reports'] and not result['investigation_requests']
    assert [role for role,_ in model.calls] == ['evaluate','evaluate']
    assert result['state']['phase'] == 'evaluate' and result['state']['version'] == 1
    work['completed_evaluation_keys'] = [row['evaluation_key'] for row in result['evaluations']]
    work['input_revision'] = core.digest(['committed-evaluations', 1])
    replay = run_case(work, model=model, state=result['state'])
    assert not replay['evaluations'] and len(model.calls) == 2
    work['facts'] = {'distinct_documents':2, 'distinct_observations':2, 'relevant_documents':1, 'missing_evaluations':0}
    with pytest.raises(ValueError, match='pre-commit'):
        run_case(work, model=model)


def test_resumed_case_evaluates_newer_committed_research_revision_without_reassigning_case():
    work=case_work('evaluate')
    config=fixture('01-configuration.json')
    config['payload'].update(revision=2,previous_revision=1)
    for row in work['pending_inputs']:
        row['config_revision']=2
    result=run_case(work,config=config)
    assert result['state']['investigation_id']==work['investigation_id']
    assert all(row['config_revision']==2 for row in result['evaluations'])
    work['pending_inputs'][0]['config_revision']=3
    with pytest.raises(ValueError,match='captured criterion'):
        run_case(work,config=config)


def test_massive_case_reports_exact_gold_totals_with_a_small_sample_and_constant_state():
    model, work = Model(), case_work()
    work['facts'] = {'distinct_documents':1000000, 'distinct_observations':1200000,
                     'relevant_documents':600000, 'missing_evaluations':1000}
    for item in work['indicators']:
        item.update(value='1000000' if item['key']=='documents' else '600000', population_count=1000000)
    result = run_case(work, model=model)
    report = result['reports'][0]
    assert report['coverage']['distinct_documents'] == 1000000
    assert report['coverage']['distinct_observations'] == 1200000
    assert len(report['evaluations']) == 2
    assert report['indicators'][0]['value'] == '1000000'
    assert 'selección acotada' in ' '.join(report['coverage']['limitations'])
    assert len(core.encoded(result['state'])) < 1800
    assert not {'observations','discoveries','evaluations','snapshots','reports','configuration'} & result['state'].keys()
    for role,payload in model.calls:
        assert len(core.encoded(payload)) <= 98304
        if role == 'plan':
            assert payload['facts']['distinct_documents'] == 1000000


def test_case_replay_does_not_reinfer_or_increment_report_and_rejects_other_case_state():
    model, work = Model(), case_work()
    first = run_case(work, model=model)
    work['report_revision'] = first['state']['report_revision']
    calls = len(model.calls)
    second = run_case(work, model=model, state=first['state'])
    assert len(model.calls) == calls and not second['reports'] and second['state'] == first['state']
    wrong = copy.deepcopy(first['state'])
    wrong['investigation_id'] = 'a'*64
    with pytest.raises(ValueError, match='another case'):
        run_case(work, model=model, state=wrong)


@pytest.mark.parametrize('change', ['case','generation','scope','facts','evidence','unbounded','partial_requests'])
def test_case_work_fails_closed_before_model_for_invalid_hydration(change):
    model, work = Model(), case_work()
    if change == 'case': work['investigation_id'] = 'b'*64
    elif change == 'generation': work['scope']['generation_id'] = str(UUID(int=60001))
    elif change == 'scope': work['criterion']['approval']['status'] = 'pending'
    elif change == 'facts': work['facts']['missing_evaluations'] = 3
    elif change == 'evidence': work['evidence'][0]['quote'] += ' altered'
    elif change == 'unbounded': work['evaluations'] *= 51
    else: work['requests_complete'] = False
    with pytest.raises(ValueError):
        run_case(work, model=model)
    assert not model.calls


@pytest.mark.parametrize('failure', ['missing','unknown','closed','expired','paused'])
def test_case_research_uses_current_permission_and_real_budget_or_refuses(failure):
    model, work, config = Model(research=True), case_work(), fixture('01-configuration.json')
    if failure == 'missing': work['budget_round'] = None
    elif failure == 'unknown': work['budget_round']['payload']['remaining_microusd'] = None
    elif failure == 'closed': work['budget_round']['payload']['status'] = 'closed'
    elif failure == 'expired': work['budget_round']['payload']['expires_at'] = '2026-01-01T00:10:00Z'
    else: config['payload']['criteria'][0]['status'] = 'paused'
    result = run_case(work, model=model, config=config)
    assert not result['investigation_requests']


def test_case_uncertain_and_unpublished_completed_requests_wait_without_new_start():
    work = case_work()
    first = run_case(work, model=Model(research=True))
    request = first['investigation_requests'][0]
    assert first['reports'][0]['coverage']['pending_requests'] == 1
    for status in ('uncertain','completed'):
        model = Model(research=True)
        waiting = copy.deepcopy(work)
        waiting.update(report_revision=1, input_revision=core.digest([status]))
        waiting['requests'] = [{'request':request, 'status':status, 'observations_committed':False,
                                'observation_message_ids':[str(UUID(int=65000))]}]
        result = run_case(waiting, model=model, state=first['state'])
        assert not result['investigation_requests']
        assert 'plan' not in [role for role,_ in model.calls]
        assert result['reports'][0]['coverage']['pending_requests'] == 1


def test_case_proposal_stays_pending_and_model_sees_only_applicable_configuration():
    model = Model(propose=True)
    result = run_case(model=model)
    assert len(result['proposals']) == 1
    record = result['proposals'][0]['change']['record']
    assert record['origin'] == 'agent' and record['status'] == 'draft'
    assert record['approval']['status'] == 'pending'
    payload = next(value for role,value in model.calls if role=='propose')
    assert set(payload['configuration']) == {'sources','queries','criteria','connectors'}
    assert len(payload['configuration']['criteria']) == 1


def test_case_unknown_relevance_and_model_invented_number_are_not_fabricated_counts():
    work = case_work()
    work['facts'].update(relevant_documents=None, missing_evaluations=2)
    for indicator in work['indicators']:
        if indicator['key'] == 'relevant_documents':
            indicator.update(value=None, missing_count=2)
    result = run_case(work, model=Model(bad_numbers=True))
    assert result['reports'][0]['status'] == 'failed'
    assert '9999999' not in result['reports'][0]['summary']
    assert result['reports'][0]['indicators'][1]['value'] is None


def test_case_long_citations_are_selected_whole_before_inference_without_losing_gold_totals():
    model, work = Model(), case_work()
    for number in range(100-len(work['evidence'])):
        row = copy.deepcopy(work['evidence'][0])
        row.update(observation_key=core.digest(['long-observation',number]), quote=('Exact long quotation. '*200)[:4000])
        row['evidence_id'] = core.digest([row['observation_key'],row['quote']])
        work['evidence'].append(row)
    work['facts'].update(distinct_documents=1000000, distinct_observations=1000000)
    result = run_case(work, model=model)
    assert len(result['reports'][0]['evidence']) == 100
    assert result['reports'][0]['coverage']['distinct_documents'] == 1000000
    for role,payload in model.calls:
        assert len(core.encoded(payload)) <= 98304
        if role in ('plan','report'):
            assert 0 < len(payload['evidence']) < 100
            assert all(row in work['evidence'] for row in payload['evidence'])
    assert 'tamaño' in ' '.join(result['reports'][0]['coverage']['limitations'])


def test_case_evaluation_failure_remains_unknown_and_does_not_generate_a_report_from_partial_results():
    result = run_case(case_work('evaluate'), model=Model(bad_quotes=True))
    assert len(result['evaluations']) == 2 and not result['reports']
    assert all(row['decision']=='error' and row['score'] is None for row in result['evaluations'])


def test_case_history_cannot_omit_a_committed_request_or_import_another_execution():
    first = run_case(model=Model(research=True))
    work = case_work()
    work['report_revision'] = 1
    with pytest.raises(ValueError, match='complete committed request'):
        run_case(work, state=first['state'])
    request = first['investigation_requests'][0]
    work['requests'] = [{'request':request,'status':'uncertain','observations_committed':False}]
    result = execution(request, 'uncertain', number=60002)
    result['generation_id'] = str(UUID(int=60003))
    with pytest.raises(ValueError, match='committed case request'):
        run_case(work, state=first['state'], results=[result])


def test_case_no_evidence_preserves_server_counts_and_marks_the_report_limited():
    work = case_work()
    work.update(evidence=[], indicators=[])
    result = run_case(work)
    assert result['reports'][0]['coverage']['distinct_documents'] == 2
    assert result['reports'][0]['status'] == 'completed_with_limits'
    assert 'No hay citas disponibles' in ' '.join(result['reports'][0]['coverage']['limitations'])


def test_detection_uses_approved_threshold_exact_quotes_and_trusted_identity():
    model,work=Model(),case_work('evaluate')
    result=run_case(work,model=model)
    assert sorted(row['decision'] for row in result['evaluations'])==['not_relevant','relevant']
    observed={row['observation']['observation_key']:row['observation'] for row in work['pending_inputs']}
    for value in result['evaluations']:
        assert value['threshold']==0.8 and value['model_id']==model.identity['model_id']
        assert value['evaluation_key']==contract.evaluation_fingerprint(value)
        assert all(item['quote'] in observed[value['observation_key']]['text'] and item['url']==observed[value['observation_key']]['url'] for item in value['evidence'])
    assert result['state']['investigation_id']==work['investigation_id']


@pytest.mark.parametrize('options',[{'bad_quotes':True},{'error':True}])
def test_failed_evaluation_is_unknown_and_gold_completed_keys_prevent_automatic_retry(options):
    model,work=Model(**options),case_work('evaluate')
    first=run_case(work,model=model)
    assert all(row['decision']=='error' and row['score'] is None and not row['evidence'] for row in first['evaluations'])
    assert 'Sensitive provider diagnostic' not in json.dumps(first)
    work['completed_evaluation_keys']=[row['evaluation_key'] for row in first['evaluations']]
    work['input_revision']=core.digest(['error-verdicts-committed'])
    calls=len(model.calls)
    replay=run_case(work,model=model,state=first['state'])
    assert not replay['evaluations'] and len(model.calls)==calls


def test_research_request_is_data_only_deterministic_and_bounded_by_real_round_and_query_caps():
    work=case_work()
    work['budget_round']['payload'].update(remaining_microusd=2500,research_remaining_microusd=2500)
    budget_before=copy.deepcopy(work['budget_round'])
    result=run_case(work,model=Model(research=True))
    assert len(result['investigation_requests'])==1
    request=contract.InvestigationRequest.model_validate(result['investigation_requests'][0])
    assert request.max_items==2 and request.max_cost_microusd==2000
    assert str(request.budget_round_id)==work['budget_round']['payload']['budget_round_id']
    assert request.config_revision==1 and request.expires_at.isoformat()=='2026-01-01T01:00:00+00:00'
    assert result['reports'][0]['status']=='awaiting_evidence' and work['budget_round']==budget_before
    assert run_case(work,model=Model(research=True))['investigation_requests'][0]['request_id']==str(request.request_id)


@pytest.mark.parametrize('failure',['unknown_price','research_disallowed','future_round'])
def test_no_paid_request_with_unknown_price_disabled_rule_or_future_round(failure):
    work,config=case_work(),fixture('01-configuration.json')
    if failure=='unknown_price':
        config['payload']['connectors'][0]['max_cost_microusd_per_item']=None
        config['payload']['catalog_sha256']=contract.catalog_fingerprint(config['payload']['connectors'])
    elif failure=='research_disallowed':config['payload']['criteria'][0]['allow_research']=False
    else:work['budget_round']['payload'].update(opened_at='2026-01-01T00:40:00Z')
    result=run_case(work,config=config,model=Model(research=True))
    assert not result['investigation_requests'] and result['reports'][0]['coverage']['limitations']


@pytest.mark.parametrize('target',['https://evil.example/','http://127.0.0.1/','https://example.com.evil.example/','https://user:password@example.com/'])
def test_model_cannot_override_web_research_to_an_unapproved_or_private_host(target):
    assert not run_case(model=Model(research=True,target=target))['investigation_requests']


def test_budget_exhaustion_reports_available_evidence_without_restarting():
    first=run_case(model=Model(research=True))
    work=case_work()
    work.update(report_revision=1,input_revision=core.digest(['budget-exhausted']),requests=[{
        'request':first['investigation_requests'][0],'status':'budget_exhausted','observations_committed':True}])
    model=Model(research=True)
    result=run_case(work,model=model,state=first['state'])
    assert not result['investigation_requests'] and 'plan' not in [role for role,_ in model.calls]
    assert result['reports'][0]['coverage']['exhausted_budget']
    assert result['reports'][0]['status']=='completed_with_limits'


def test_model_invented_report_numbers_do_not_replace_deterministic_gold_indicators():
    work=case_work()
    report=run_case(work,model=Model(bad_numbers=True))['reports'][0]
    assert '9999999' not in report['summary']
    assert {row['key']:row['value'] for row in report['indicators']}=={'documents':'2','relevant_documents':'1'}
    assert work['indicators']==report['indicators']


def test_capture_snapshot_survives_later_pause_but_cannot_authorize_new_research():
    current=fixture('01-configuration.json')
    current['payload'].update(revision=2,previous_revision=1)
    for row in current['payload']['sources']+current['payload']['queries']:
        row.update(status='paused',revision=row['revision']+1)
    historical=case_work('evaluate')
    result=run_case(historical,config=current)
    assert all(row['config_revision']==1 and row['threshold']==0.8 for row in result['evaluations'])
    assert not run_case(case_work(),config=current,model=Model(research=True))['investigation_requests']
    assert current['payload']['criteria'][0]['threshold']==0.8


@pytest.mark.parametrize('field',['registry_id','generation_id','contract_sha256'])
def test_budget_scope_conflicts_fail_before_any_model_call(field):
    work,model=case_work(),Model()
    work['budget_round'][field]='a'*64 if field=='contract_sha256' else str(UUID(int=999))
    with pytest.raises(ValueError):run_case(work,model=model)
    assert not model.calls


def test_zero_is_known_free_pricing_and_is_different_from_an_unknown_price():
    config,work=fixture('01-configuration.json'),case_work()
    config['payload']['connectors'][0]['max_cost_microusd_per_item']=0
    config['payload']['catalog_sha256']=contract.catalog_fingerprint(config['payload']['connectors'])
    work['budget_round']['payload'].update(remaining_microusd=0,research_remaining_microusd=0)
    request=run_case(work,config=config,model=Model(research=True))['investigation_requests'][0]
    assert request['max_items']==5 and request['max_cost_microusd']==0


@pytest.mark.parametrize('change',['unknown_old_revision','changed_same_revision','reopen'])
def test_gold_owns_budget_immutability_and_closed_round_replay_rules(monkeypatch,change):
    closed=fixture('10-budget-round-closed.json')['payload']
    message=fixture('09-budget-round-open.json')
    if change=='changed_same_revision':message['payload']['remaining_items']+=1
    elif change=='reopen':message['payload']['revision']=closed['revision']+1
    monkeypatch.setattr(gold,'_configuration',lambda *args:fixture('01-configuration.json')['payload'])
    def find(spark,binding,suffix,**where):
        if suffix=='budget_rounds':return [{'payload_json':gold.canonical(closed)}]
        if suffix=='budget_round_revisions' and change=='changed_same_revision':
            return [{'payload_sha256':gold.fingerprint(fixture('09-budget-round-open.json')['payload'])}]
        return []
    monkeypatch.setattr(gold,'_find',find)
    with pytest.raises(ValueError):gold._prepare(None,{},message)


def test_gold_known_old_budget_revision_cannot_reopen_closed_current_state(monkeypatch):
    # Known immutable versions may replay; the conditional MERGE, not message UUID, protects the head.
    message=fixture('09-budget-round-open.json')
    monkeypatch.setattr(gold,'_configuration',lambda *args:{})
    monkeypatch.setattr(gold,'_find',lambda *args,**kwargs:[{'payload_sha256':gold.fingerprint(message['payload'])}])
    assert gold._prepare(None,{},message)=={}
    writes=[]
    monkeypatch.setattr(gold,'_rows',lambda *args,**kwargs:writes.append((args[2],kwargs)))
    gold._effect(None,{},message,{})
    assert next(options for table,options in writes if table=='budget_rounds')['condition']=='s.revision > t.revision'


def test_human_rejection_preserves_pending_proposal_without_reemitting_or_activating_it():
    first=run_case(model=Model(propose=True))
    proposal=copy.deepcopy(first['proposals'][0])
    assert proposal['state']=='pending' and proposal['change']['record']['origin']=='agent'
    assert proposal['change']['record']['revision']==2 and proposal['base_config_revision']==1
    current=fixture('01-configuration.json')
    current['payload'].update(revision=2,previous_revision=1,proposal_decisions=[{
        'proposal_id':proposal['proposal_id'],'decision':'rejected','operator_id':'fixture-operator',
        'decided_at':NOW,'config_revision':2,'reason':'Keep the existing criterion.'}])
    work=case_work()
    work.update(report_revision=1,input_revision=core.digest(['human-rejected-proposal']))
    result=run_case(work,config=current,model=Model(propose=True),state=first['state'])
    assert not result['proposals'] and first['proposals'][0]==proposal
    assert current['payload']['criteria'][0]['threshold']==0.8


def test_report_passes_full_gold_metrics_without_summing_observation_versions_or_replacing_null():
    work=case_work()
    work['facts']['distinct_observations']=3
    reference=[work['evidence'][0]['evidence_id']]
    work['indicators'].extend([{'key':key,'label':key,'value':value,'unit':key,'period_start':None,'period_end':None,
        'evidence_ids':reference,'method':'reported_metric_sum','population_count':2,'missing_count':missing}
        for key,value,missing in (('comments','0',1),('likes',None,2))])
    report=run_case(work)['reports'][0]
    assert report['coverage']['distinct_documents']==2 and report['coverage']['distinct_observations']==3
    values={row['key']:row for row in report['indicators']}
    assert values['comments']['value']=='0' and values['comments']['missing_count']==1
    assert values['likes']['value'] is None and values['likes']['missing_count']==2


def test_reviewed_mapped_decimal_is_passed_exactly_with_units_period_and_evidence():
    work=case_work()
    mapped={'key':'price_change','label':'Price change','value':'-2.750','unit':'USD','period_start':NOW,'period_end':NOW,
        'evidence_ids':[work['evidence'][0]['evidence_id']],'method':'source_mapped_value','population_count':1,'missing_count':0}
    work['indicators'].append(mapped)
    report=run_case(work)['reports'][0]
    value=next(row for row in report['indicators'] if row['method']=='source_mapped_value')
    assert value['value']=='-2.750' and value['unit']=='USD' and value['evidence_ids']
    assert value['period_start']==value['period_end']==NOW


def test_real_later_budget_round_resumes_case_waiting_for_authorization_without_reevaluation():
    work,model=case_work(),Model(research=True)
    work['budget_round']=None
    first=run_case(work,model=model)
    assert not first['investigation_requests']
    work.update(report_revision=1,budget_round=fixture('09-budget-round-open.json'),input_revision=core.digest(['budget-published']))
    resumed=run_case(work,model=model,state=first['state'])
    assert len(resumed['investigation_requests'])==1
    assert 'evaluate' not in [role for role,_ in model.calls]


def test_equivalent_effective_web_targets_do_not_generate_two_paid_request_identities():
    class DuplicateTargets(Model):
        async def __call__(self,role,payload):
            value=await super().__call__(role,payload)
            if role=='plan':
                extra=copy.deepcopy(value['requests'][0])
                extra['target_override']='https://EXAMPLE.com:443/page/1#fragment'
                value['requests'].append(extra)
            return value
    assert len(run_case(model=DuplicateTargets(research=True))['investigation_requests'])==1


@pytest.mark.parametrize('recapture',['other_query','later_window'])
def test_gold_unchanged_recapture_adds_provenance_without_new_evaluation_or_case(monkeypatch,recapture):
    first=fixture('02-observations.json')
    capture=copy.deepcopy(first)
    capture['message_id']=str(UUID(int=930))
    capture['payload']['execution_id']=str(UUID(int=931))
    config=fixture('01-configuration.json')['payload']
    if recapture=='other_query':capture['payload']['query_id']=config['queries'][1]['query_id']
    else:
        for row in capture['payload']['observations']:row['observed_at']='2026-01-03T00:00:00Z'
    prior=gold.observation_rows(first,config)
    monkeypatch.setattr(gold,'_configuration',lambda *args:config)
    monkeypatch.setattr(gold,'_find',lambda spark,b,suffix,**where:[{'config_revision':1}] if suffix=='budget_rounds' else [])
    monkeypatch.setattr(gold,'_keys',lambda spark,b,suffix,key,values:prior[suffix] if suffix in ('observations','evaluations') else [])
    plan=gold._prepare(None,{},capture)
    assert plan['evaluations']==[] and plan['investigations']==[]
    assert len(plan['discoveries'])==2 and len(prior['observations'])==2
    assert all(row['observed_at']==gold.utc('2026-01-01T00:00:00Z') for row in prior['observations'])
    assert {row['observation_key'] for row in plan['observations']}=={row['observation_key'] for row in prior['observations']}


@pytest.mark.parametrize('threshold',[0.8,0.99])
def test_operator_threshold_controls_verdict_not_model_claims(threshold):
    work=case_work('evaluate')
    work['criterion']['threshold']=threshold
    config=fixture('01-configuration.json')
    config['payload']['criteria'][0]['threshold']=threshold
    result=run_case(work,config=config)
    assert all(row['threshold']==threshold for row in result['evaluations'])
    assert [row['decision'] for row in result['evaluations']]==(['relevant','not_relevant'] if threshold==0.8 else ['not_relevant','not_relevant'])


def test_inference_payload_mutations_cannot_change_approved_rule_or_observation():
    class Mutating(Model):
        async def __call__(self,role,payload):
            payload['criterion']['threshold']=0.0
            payload['observation']['metrics']['views']=9999999
            return {'score':0.7,'reason':'Synthetic verdict.','quotes':[payload['observation']['text']]}
    work=case_work('evaluate')
    original=copy.deepcopy(work)
    result=run_case(work,model=Mutating())
    assert all(row['decision']=='not_relevant' and row['threshold']==0.8 for row in result['evaluations'])
    assert work==original and all(row['observation']['metrics']['views'] is None for row in work['pending_inputs'])


def test_blank_content_yields_unknown_verdict_without_manufactured_quote_or_report_evidence():
    work=case_work('evaluate')
    row=work['pending_inputs'][0]['observation']
    row.update(text=' '*5000,title=None)
    row.update(contract.observation_fingerprints(row))
    work['pending_inputs']=work['pending_inputs'][:1]
    result=run_case(work)
    assert result['evaluations'][0]['score'] is None and result['evaluations'][0]['decision']=='error'
    assert result['evaluations'][0]['evidence']==[] and not result['reports']


def test_changed_content_is_one_new_version_evaluation_and_retains_original_case_identity():
    work=case_work('evaluate')
    original=case_work()
    work['completed_evaluation_keys']=[row['evaluation_key'] for row in original['evaluations']]
    row=work['pending_inputs'][0]['observation']
    document=row['document_key']
    row.update(text='Northstar announced a further price reduction.',observed_at='2026-01-01T00:45:00Z')
    row.update(contract.observation_fingerprints(row))
    result=run_case(work)
    assert len(result['evaluations'])==1 and result['state']['investigation_id']==work['investigation_id']
    assert row['document_key']==document and row['observation_key']!=original['evaluations'][0]['observation_key']


@pytest.mark.parametrize('field',['source_id','query_id'])
def test_pending_input_must_match_captured_criterion_source_and_query_scope(field):
    work,model=case_work('evaluate'),Model()
    work['criterion']['query_ids']=[work['pending_inputs'][0]['query_id']]
    work['pending_inputs'][0][field]=str(UUID(int=999))
    with pytest.raises(ValueError):run_case(work,model=model)
    assert not model.calls


def test_gold_message_identity_conflict_stops_before_effects_or_model(monkeypatch):
    original=fixture('02-observations.json')
    message=copy.deepcopy(original)
    message['payload']['provider_run_id']='different-run-under-the-same-message'
    binding={'registry_id':original['registry_id'],'generation_id':original['generation_id'],
        'contract_sha256':original['contract_sha256'],'catalog':'spark_catalog','gold_schema':'sonda_gold',
        'control_schema':'sonda_control','namespace':'synthetic','source_bucket':'source',
        'gold_bucket':'gold','artifacts_bucket':'artifacts'}
    monkeypatch.setattr(gold,'_recover_studies',lambda *args:None)
    monkeypatch.setattr(gold,'_recover_case_studies',lambda *args:None)
    monkeypatch.setattr(gold,'_pending',lambda *args:[])
    monkeypatch.setattr(gold,'_find',lambda *args,**kwargs:[{'message_sha256':gold.fingerprint(original)}])
    monkeypatch.setattr(gold,'_effect',lambda *args:pytest.fail('Conflicting identity cannot write business effects'))
    with pytest.raises(ValueError,match='Conflicting message identity'):
        gold.apply_messages(object(),binding,[message])


def execution(request, status, *, number, observations=None):
    value = fixture('04-execution-result.json')
    value['message_id'] = str(UUID(int=number))
    value['payload'].update({key:request[key] for key in ('request_id','config_revision','query_id','query_revision','budget_round_id')})
    value['payload'].update(execution_id=str(UUID(int=700)), status=status,
        observation_message_ids=observations or [], spent_microusd=None if status=='uncertain' else 1000,
        record_count=0 if status=='uncertain' else 1, coverage='unknown' if status=='uncertain' else 'partial')
    return value
