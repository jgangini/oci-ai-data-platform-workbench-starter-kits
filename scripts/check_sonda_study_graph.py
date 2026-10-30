"""Real offline LangGraph study regression; inference is a deterministic stub.

Run with the existing LangGraph 0.6.7 environment. No transport, SQL, credentials,
native memory service, Actor execution or live semantic-model claim is involved.
"""
import asyncio
import copy
import json
from pathlib import Path
import sys
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'apps/backend'))
from app.sonda_contract import observation_fingerprints
from app.labs.ai_social_network.notebooks.agent.sonda_study import (case_key, canonical_utc, digest,
    encoded, instant, study_case)


def fixture(name):
    return json.loads((ROOT/'contracts/sonda/v1/fixtures'/name).read_text(encoding='utf-8'))


class Inference:
    identity = {'model_id':'offline-study-fixture', 'model_revision':'fixture.1', 'prompt_version':'fixture.1'}

    def __init__(self):
        self.calls = []

    async def __call__(self, role, payload):
        self.calls.append(role)
        if role == 'evaluate':
            text = payload['observation']['text']
            return {'score':0.95 if 'price' in text.lower() else 0.05, 'reason':'Synthetic labelled verdict.', 'quotes':[text]}
        if role == 'plan':
            queries = payload['approved_queries']
            return {'requests':[{'step':'corroborate','query_id':queries[0]['query_id'],
                'purpose':'Request supporting evidence.', 'target_override':'https://example.com/supporting', 'max_items':2}]
                if queries else [], 'propose':False}
        if role == 'report':
            return {'title':'Estudio de evidencia', 'summary':'Se conserva el alcance y la procedencia de la evidencia disponible.'}
        raise AssertionError('No unsolicited proposal callback is expected')


def work_fixture(config,capture):
    from datetime import datetime,timedelta,timezone
    criterion=config['payload']['criteria'][0]
    epoch=datetime(1970,1,1,tzinfo=timezone.utc)
    seconds=criterion['case_window_hours']*3600
    observed=instant(capture['payload']['observations'][0]['observed_at'])
    start=epoch+timedelta(seconds=((observed-epoch)//timedelta(seconds=seconds))*seconds)
    return {'scope':{key:config[key] for key in ('contract_version','contract_sha256','registry_id','generation_id')},
        'investigation_id':case_key(criterion,start),'criterion':criterion,'config_revision':1,
        'window_start':canonical_utc(start.isoformat()),'phase':'evaluate',
        'input_revision':digest(['synthetic-hydration',1]),'requests_complete':True,
        'pending_inputs':[{'observation':row,'config_revision':1,'source_id':capture['payload']['source_id'],
            'query_id':capture['payload']['query_id']} for row in capture['payload']['observations']]}


def report_projection(work,evaluations,*,revision,signal,documents,observations,relevant):
    # Explicit trusted fixture aggregates; this script does not emulate Gold SQL.
    value={key:copy.deepcopy(item) for key,item in work.items() if key not in ('pending_inputs','completed_evaluation_keys')}
    evidence=[item for row in evaluations for item in row['evidence']]
    value.update(phase='report',report_revision=revision,input_revision=digest([signal]),evaluations=evaluations,evidence=evidence,
        facts={'distinct_documents':documents,'distinct_observations':observations,'relevant_documents':relevant,'missing_evaluations':0},
        indicators=[],budget_round=fixture('09-budget-round-open.json'),
        approved_query_ids=[q['query_id'] for q in fixture('01-configuration.json')['payload']['queries']])
    return value


async def main():
    config,capture=fixture('01-configuration.json'),fixture('02-observations.json')
    work=work_fixture(config,capture)
    model=Inference()
    evaluated=await study_case(config,work,{},[],model=model,now='2026-01-01T00:30:00Z')
    assert len(evaluated['evaluations'])==2 and not evaluated['reports']
    assert [row['decision'] for row in evaluated['evaluations']]==['relevant','not_relevant']
    assert all(item['quote'] in capture['payload']['observations'][index]['text']
        for index,row in enumerate(evaluated['evaluations']) for item in row['evidence'])
    count=len(model.calls)
    replay=await study_case(config,work,evaluated['state'],[],model=model,now='2026-01-01T00:31:00Z')
    assert len(model.calls)==count and all(not replay[key] for key in ('evaluations','reports','investigation_requests','proposals'))
    # Gold deduplicates multi-query discoveries and supplies exact completed keys.
    work['completed_evaluation_keys']=[row['evaluation_key'] for row in evaluated['evaluations']]
    work['input_revision']=digest(['another-query-same-content'])
    duplicate=await study_case(config,work,replay['state'],[],model=model,now='2026-01-01T00:31:00Z')
    assert len(model.calls)==count and not duplicate['evaluations']
    reporting=report_projection(work,evaluated['evaluations'],revision=0,signal='evaluations-committed',documents=2,observations=2,relevant=1)
    reporting['query_ids']=[capture['payload']['query_id']]
    first=await study_case(config,reporting,duplicate['state'],[],model=model,now='2026-01-01T00:31:00Z')
    assert len(first['reports'])==1 and len(first['investigation_requests'])==1 and not first['proposals']
    request=first['investigation_requests'][0]
    assert request['max_cost_microusd']==2000 and first['reports'][0]['status']=='awaiting_evidence'
    assert first['reports'][0]['coverage']['distinct_documents']==2
    waiting_work=copy.deepcopy(reporting)
    waiting_work.update(report_revision=1,input_revision=digest(['uncertain-result']),requests=[{
        'request':request,'status':'uncertain','observations_committed':False}])
    plans=model.calls.count('plan')
    waiting=await study_case(config,waiting_work,first['state'],[],model=model,now='2026-01-01T00:32:00Z')
    assert model.calls.count('plan')==plans and not waiting['investigation_requests']
    assert waiting['reports'][0]['coverage']['pending_requests']==1
    waiting_work.update(report_revision=2,input_revision=digest(['completed-without-publication']))
    waiting_work['requests'][0]['status']='completed'
    unpublished=await study_case(config,waiting_work,waiting['state'],[],model=model,now='2026-01-01T00:33:00Z')
    assert unpublished['reports'][0]['status']=='awaiting_evidence' and model.calls.count('plan')==plans
    # A fresh Gold projection assigns research evidence to its original persisted case.
    supporting=copy.deepcopy(capture['payload']['observations'][0])
    supporting.update(url='https://example.com/supporting',text='Additional observed price reduction evidence.',observed_at='2026-01-02T00:05:00Z')
    supporting.update(observation_fingerprints(supporting))
    resumed_work=work_fixture(config,capture)
    resumed_work.update(input_revision=digest(['supporting-evaluation']),report_revision=3,requests=copy.deepcopy(waiting_work['requests']))
    resumed_work['requests'][0]['observations_committed']=True
    resumed_work['pending_inputs']=[{**resumed_work['pending_inputs'][0],'observation':supporting}]
    resumed=await study_case(config,resumed_work,unpublished['state'],[],model=model,now='2026-01-02T00:10:00Z')
    assert len(resumed['evaluations'])==1 and resumed['state']['investigation_id']==work['investigation_id']
    completed_work=report_projection(resumed_work,evaluated['evaluations']+resumed['evaluations'],revision=3,
        signal='research-evidence-published',documents=3,observations=3,relevant=2)
    completed=await study_case(config,completed_work,resumed['state'],[],model=model,now='2026-01-02T00:11:00Z')
    assert completed['reports'][0]['coverage']['distinct_documents']==3 and not completed['investigation_requests']
    count=len(model.calls)
    completed_work['report_revision']=4
    again=await study_case(config,completed_work,completed['state'],[],model=model,now='2026-01-02T00:12:00Z')
    assert len(model.calls)==count and not again['reports']
    wrong=copy.deepcopy(completed_work)
    wrong['scope']['generation_id']=str(UUID(int=9999))
    try:
        await study_case(config,wrong,again['state'],[],model=model,now='2026-01-02T00:13:00Z')
        raise AssertionError('Generation isolation must precede inference')
    except ValueError:
        assert len(model.calls)==count
    # Large full-population values remain independent of the bounded evidence sample.
    completed_work.update(input_revision=digest(['large-case-hydration']),
        facts={'distinct_documents':1000000,'distinct_observations':1200000,'relevant_documents':600000,'missing_evaluations':0})
    large=await study_case(config,completed_work,again['state'],[],model=model,now='2026-01-02T00:14:00Z')
    assert large['reports'][0]['coverage']['distinct_documents']==1000000
    assert len(encoded(large['state']))<1800 and not {'observations','snapshots','evaluations'} & large['state'].keys()
    assert large['state']['predecessor_revision']==again['state']['version']
    print('PASS production case StateGraph: evaluate/commit/report, evidence-bound verdicts, dedup, bounded research, uncertainty/publication reconciliation, original case, replay, generation isolation and compact full-population reporting (offline fixtures)')


if __name__=='__main__':
    asyncio.run(main())
