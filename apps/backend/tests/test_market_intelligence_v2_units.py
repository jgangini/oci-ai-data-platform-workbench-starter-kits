"""A repair publishes only explicitly approved, independently cited units."""
import pytest

from test_market_intelligence_v2 import v2_fixture,v2_run


def unit_fixture(verdicts=None,*,foreign=False):
    store,model,collector,graph=v2_fixture()
    original=model.ask
    async def review(name,schema,payload):
        result=await original(name,schema,payload)
        if name=='v2_verify' and not store.read()[0].get('draft_repairs'):
            row=payload['evidence'][0]
            return {'accepted':False,'issues':[{'claim':payload['block']['text'],'evidence_id':row['evidence_id'],
                'quoted_text':row['text'],'reason':'Repair the proposed attribution.'}]}
        if name=='v2_repair_units':
            identity=payload['evidence'][0]['evidence_id']
            return {'units':[
                {'unit_id':'u1','text':'Una reclamación se atribuye a esta muestra.',
                    'evidence_ids':['unowned'] if foreign else [identity],'limitations':[]},
                {'unit_id':'u2','text':'El total confirmado es 999.','evidence_ids':[identity],'limitations':[]}]}
        if name=='v2_verify_repair':
            if verdicts is not None:
                return {'reviews':verdicts}
            row=payload['evidence'][0]
            return {'reviews':[{'unit_id':'u1','accepted':True,'issues':[]},
                {'unit_id':'u2','accepted':False,'issues':[{'claim':'El total confirmado es 999.',
                    'evidence_id':row['evidence_id'],'quoted_text':row['text'],'reason':'The document does not report999.'}]}]}
        return result
    model.ask=review
    return store,model,collector,graph


def test_partial_unit_repair_preserves_good_content_without_publishing_rejected_claims():
    store,model,collector,graph=unit_fixture()
    events,state=v2_run(graph)
    assert state['response']['status']=='partial' and state['response']['references']
    assert all('999' not in event['text'] for event in events)
    assert any(event['type']=='draft.upsert' and 'Una reclamación' in event['text'] for event in events)
    outcome=store.read()[0]['domain_outcomes']['web']
    assert outcome['repair_unit_ids']==['u1'] and outcome['status']=='partial'
    assert len(collector.calls)==1 and len(store.read()[0]['draft_repairs'])==1
    assert model.calls.count('v2_repair_units')==model.calls.count('v2_verify_repair')==1


@pytest.mark.parametrize('verdicts',[
    [],
    [{'unit_id':'u1','accepted':True,'issues':[]}],
    [{'unit_id':'u1','accepted':True,'issues':[]},{'unit_id':'u1','accepted':True,'issues':[]}],
    [{'unit_id':'u1','accepted':True,'issues':[]},{'unit_id':'u3','accepted':True,'issues':[]}],
])
def test_missing_duplicate_or_unknown_unit_verdict_fails_before_any_content_publication(verdicts):
    store,model,collector,graph=unit_fixture(verdicts)
    events,state=v2_run(graph)
    assert state['response']['status']=='failed' and state['response']['references']==[]
    assert not any(event['type'].startswith('draft.') for event in events)
    assert len(collector.calls)==1


def test_unit_cannot_cite_unadmitted_evidence_even_before_its_review():
    store,model,collector,graph=unit_fixture(foreign=True)
    events,state=v2_run(graph)
    assert state['response']['status']=='failed' and state['response']['references']==[]
    assert 'v2_verify_repair' not in model.calls
    assert not any(event['type'].startswith('draft.') for event in events)


def test_bounded_unit_review_counts_the_units_before_selecting_evidence():
    import json
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.v2_workflow import V2Workflow
    from test_market_intelligence_v2 import v2_evidence
    store,model,collector,_=v2_fixture()
    store.begin('context-bound','Review a large captured sample.')
    rows=[{**v2_evidence('web',i),'text':'A'*10000} for i in range(12)]
    store.save(brief={'entity':'Fixture company'},evidence={r['evidence_id']:r for r in rows})
    cited=[r['evidence_id'] for r in rows[:6]]
    units=[{'unit_id':'u'+str(i+1),'text':'B'*3900,'evidence_ids':cited,'limitations':[]} for i in range(4)]
    flow=V2Workflow(store,model,collector,enabled_sources=('web',))
    combined=flow.combine_units(units)
    old_context,_=flow.review_context(combined)
    assert len(json.dumps({**old_context,'units':units},ensure_ascii=False).encode())>100000
    context,omitted=flow.review_context(combined,units=units)
    assert not omitted and 'block' not in context and context['units']==units
    assert len(json.dumps(context,ensure_ascii=False).encode())<100000
