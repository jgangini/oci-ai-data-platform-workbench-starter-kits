"""Follow-ups and schedule proposals do not silently start new investigations."""
import copy

from langgraph.checkpoint.memory import InMemorySaver
import pytest

from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.v2_state import V2RunStore
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.v2_workflow import V2Workflow
from test_market_intelligence_v2 import V2FixtureModel,V2Snapshot,V2FixtureCollector,v2_fixture,v2_run
from test_market_intelligence_v2_runtime import v2_native_fixture,v2_native_turn


def followup_model(model,*,intent='reuse_report',reject=False):
    ask=model.ask
    async def followup(name,schema,payload):
        if name=='v2_select_report':
            model.calls.append(name)
            return {'request_id':payload['default_request_id'] or payload['reports'][0]['request_id']}
        if name=='v2_summarize':
            model.calls.append(name)
            return {'text':'1. Hallazgo atribuido.\n2. Muestra limitada.\n3. Datos de la captura original.',
                    'evidence_ids':payload['allowed_evidence_ids'],'limitations':[]}
        if name=='v2_verify_summary':
            model.calls.append(name)
            return {'accepted':not reject,'issues':[{'claim':'A changed claim','evidence_id':'previous_report',
                'quoted_text':payload['prior_report'][:100],'reason':'The summary added a fact.'}] if reject else []}
        result=await ask(name,schema,payload)
        if name=='v2_understand' and 'resume' in payload['question'].lower():
            result.update(intent=intent,sources=[])
        return result
    model.ask=followup


def saved_fixture():
    store,model,collector,graph=v2_fixture(('web','x'))
    v2_run(graph)
    return store.read()[0]


def followup_graph(prior,*,reject=False,intent='reuse_report',report_loader=None):
    model=V2FixtureModel()
    followup_model(model,reject=reject,intent=intent)
    store=V2RunStore(V2Snapshot(),'followup-candidate')
    collector=V2FixtureCollector()
    workflow=V2Workflow(store,model,collector,enabled_sources=('web','x'),prior_report=prior,report_loader=report_loader)
    return workflow.build(InMemorySaver()),store,model,collector


def run_followup(graph):
    import asyncio
    async def run():
        return await graph.ainvoke({'request_id':'followup','question':'Resume el informe anterior.'},
            config={'configurable':{'thread_id':'followup'}})
    return asyncio.run(run())


def test_followup_reuses_only_its_owned_report_and_artifacts_with_zero_captures():
    prior=saved_fixture()
    original=copy.deepcopy(prior)
    graph,store,model,collector=followup_graph(prior)
    state=run_followup(graph)
    assert state['response']['coverage']['no_new_capture'] is True
    assert state['response']['artifacts']==prior['response']['artifacts']
    assert model.calls==['v2_understand','v2_select_report','v2_summarize','v2_verify_summary'] and collector.calls==[]
    assert state['response']['references']==prior['response']['references'] and prior==original


def test_unbacked_summary_preserves_previous_verified_report_without_research():
    prior=saved_fixture()
    graph,store,model,collector=followup_graph(prior,reject=True)
    state=run_followup(graph)
    assert state['response']['status']=='partial'
    assert state['response']['markdown']!=prior['response']['markdown'] and collector.calls==[]
    assert state['response']['coverage']['transformation_verified'] is False
    assert state['response']['references']==[] and state['response']['artifacts']==[]
    assert prior['response']['markdown']==saved_fixture()['response']['markdown']


def test_followup_without_report_asks_once_without_planning_or_capturing():
    graph,store,model,collector=followup_graph(None)
    state=run_followup(graph)
    assert state['response']['status']=='needs_clarification'
    assert model.calls==['v2_understand'] and collector.calls==[]


def test_foreign_saved_report_is_rejected_before_any_model_call():
    prior=saved_fixture()
    prior['scope']['owner_id']='other-user'
    with pytest.raises(ValueError,match='another authenticated session'):
        followup_graph(prior)


def test_rejected_followup_cannot_return_a_report_about_the_previous_company():
    prior=saved_fixture()
    prior['brief']['entity']='Arbor Solar'
    prior['response']['markdown']='Informe anterior de Arbor Solar.'
    graph,store,model,collector=followup_graph(prior,reject=True)
    original=model.ask
    async def switched(name,schema,payload):
        result=await original(name,schema,payload)
        if name=='v2_understand':
            result['entity']='Nimbo Telecom'
        return result
    model.ask=switched
    result=run_followup(graph)
    assert result['response']['markdown']!=prior['response']['markdown']
    assert result['response'].get('coverage',{}).get('no_new_capture',True)
    assert collector.calls==[]


def test_followup_selects_an_older_owned_company_report_instead_of_the_default():
    earlier=saved_fixture()
    earlier.update(request_id='arbor-report',question='Informe de Arbor Energy Services')
    earlier['brief']['entity']='Arbor Energy Services'
    earlier['response']['markdown']='Informe propio de Arbor Energy Services.'
    recent=copy.deepcopy(earlier)
    recent.update(request_id='nimbo-report',question='Informe de Nimbo Telecom')
    recent['brief']['entity']='Nimbo Telecom'
    recent['response']['markdown']='Informe más reciente de Nimbo Telecom.'
    graph,store,model,collector=followup_graph(recent,report_loader=lambda:[recent,earlier])
    original=model.ask
    async def select_earlier(name,schema,payload):
        if name=='v2_select_report':
            assert set(schema['properties']['request_id']['anyOf'][0]['enum'])=={'arbor-report','nimbo-report'}
            return {'request_id':'arbor-report'}
        if name=='v2_understand':
            result=await original(name,schema,payload)
            result['clarification']='Sólo veo el informe predeterminado; falta identificar el otro.'
            return result
        if name=='v2_summarize':
            assert payload['prior_report']==earlier['response']['markdown']
            assert payload['prior_brief']['entity']=='Arbor Energy Services'
        if name=='v2_verify_summary':
            assert payload['question']=='Resume el informe anterior.'
            assert payload['prior_brief']['entity']=='Arbor Energy Services'
        return await original(name,schema,payload)
    model.ask=select_earlier
    result=run_followup(graph)
    assert result['response']['coverage']['reused_request_id']=='arbor-report'
    assert store.read()[0]['report_selection']=={'request_id':'arbor-report'} and collector.calls==[]
    assert not any(e['type']=='clarification.required' for e in store.read()[0]['events'])


@pytest.mark.parametrize('choice,status',[(None,'needs_clarification'),('unowned-report','failed')])
def test_followup_missing_or_unowned_selection_never_summarizes_or_captures(choice,status):
    graph,store,model,collector=followup_graph(saved_fixture())
    original=model.ask
    async def select(name,schema,payload):
        if name=='v2_select_report':
            model.calls.append(name)
            return {'request_id':choice}
        return await original(name,schema,payload)
    model.ask=select
    result=run_followup(graph)
    assert result['response']['status']==status and result['response']['references']==[]
    assert model.calls==['v2_understand','v2_select_report'] and collector.calls==[]


def test_foreign_loaded_report_is_rejected_before_report_selection_or_summary():
    prior=saved_fixture()
    foreign=copy.deepcopy(prior)
    foreign['scope']['owner_id']='another-owner'
    graph,store,model,collector=followup_graph(prior,report_loader=lambda:[foreign])
    result=run_followup(graph)
    assert result['response']['status']=='failed'
    assert model.calls==['v2_understand'] and collector.calls==[]


def test_report_selection_is_durable_and_does_not_retarget_after_recovery():
    import asyncio
    prior=saved_fixture()
    store=V2RunStore(V2Snapshot(),'followup-candidate')
    store.begin('recover','Resume el informe anterior.')
    store.save(brief=prior['brief'])
    model=V2FixtureModel()
    followup_model(model)
    available=[prior]
    flow=V2Workflow(store,model,None,enabled_sources=('web',),report_loader=lambda:available)
    assert asyncio.run(flow.resolve_report())['request_id']==prior['request_id']
    assert asyncio.run(flow.resolve_report())['request_id']==prior['request_id']
    assert model.calls==['v2_select_report']
    available[:]=[{**prior,'request_id':'different-report'}]
    with pytest.raises(ValueError,match='no longer available'):
        asyncio.run(flow.resolve_report())
    assert model.calls==['v2_select_report']


def test_summary_review_receives_the_cited_document_and_rejects_a_wrong_source_association():
    prior=saved_fixture()
    ids=list(prior['evidence'])
    first,second=ids[:2]
    prior['evidence'][first]['text']='El fabricante del producto es Empresa A.'
    prior['evidence'][second]['text']='El número de tiendas publicado es 12.'
    prior['response']['markdown']='El fabricante es Empresa A. El número publicado es 12.'
    graph,store,model,collector=followup_graph(prior)
    ask=model.ask
    async def wrong_citation(name,schema,payload):
        if name=='v2_summarize':
            assert {row['evidence_id'] for row in payload['evidence']}==set(ids)
            return {'text':'El fabricante es Empresa A.','evidence_ids':[second],'limitations':[]}
        if name=='v2_verify_summary':
            assert [row['evidence_id'] for row in payload['evidence']]==[second]
            assert 'fabricante' not in payload['evidence'][0]['text']
            return {'accepted':False,'issues':[{'claim':payload['block']['text'],'evidence_id':second,
                'quoted_text':prior['evidence'][second]['text'],'reason':'The cited document does not identify the manufacturer.'}]}
        return await ask(name,schema,payload)
    model.ask=wrong_citation
    state=run_followup(graph)
    assert state['response']['status']=='partial'
    assert state['response']['markdown']!=prior['response']['markdown'] and collector.calls==[]
    assert state['response']['references']==[] and state['response']['artifacts']==[]


def test_schedule_proposal_has_no_job_notification_or_capture_effect():
    graph,store,model,collector=followup_graph(None,intent='schedule_proposal')
    state=run_followup(graph)
    proposal=state['response']['proposal']
    assert state['response']['kind']=='schedule_proposal'
    assert state['response']['status']=='partial' and proposal['frequency'] is None
    assert proposal['approved'] is False and proposal['effects_created']==0
    assert model.calls==['v2_understand','v2_extract_proposal'] and collector.calls==[]


@pytest.mark.parametrize('question,frequency,zone,destination',[
    ('Investiga semanalmente noticias sobre café colombiano.', 'semanalmente', None, None),
    ('Investiga café cada lunes, zona America/Lima, aviso en el chat.', 'cada lunes', 'America/Lima', 'el chat'),
])
def test_schedule_preserves_explicit_details_without_inventing_missing_fields(question,frequency,zone,destination):
    import asyncio
    graph,store,model,collector=followup_graph(None,intent='schedule_proposal')
    original=model.ask
    async def proposed(name,schema,payload):
        if name=='v2_extract_proposal':
            model.calls.append(name)
            assert payload['question']==question and payload['enabled_sources']==['web','x']
            return {'entity':'café colombiano','objective':'Investigar noticias','sources':['web'],
                    'frequency':frequency,'timezone':zone,'destination':destination}
        result=await original(name,schema,payload)
        if name=='v2_understand':
            result.update(intent='schedule_proposal',entity=None,objective=None,sources=[])
        return result
    model.ask=proposed
    result=asyncio.run(graph.ainvoke({'request_id':'proposal','question':question},config={'configurable':{'thread_id':'proposal'}}))
    response=result['response']
    assert response['proposal']=={'entity':'café colombiano','objective':'Investigar noticias','sources':['web'],
        'frequency':frequency,'timezone':zone,'destination':destination,'request_text':question,'approved':False,'effects_created':0}
    assert 'café colombiano' in response['markdown'] and frequency in response['markdown']
    assert model.calls==['v2_understand','v2_extract_proposal'] and collector.calls==[]
    assert store.read()[0]['response']==response


def test_empty_schedule_extraction_cannot_publish_a_complete_proposal():
    graph,store,model,collector=followup_graph(None,intent='schedule_proposal')
    original=model.ask
    async def proposed(name,schema,payload):
        if name=='v2_extract_proposal':
            return dict(entity=None,objective=None,sources=[],frequency=None,timezone=None,destination=None)
        return await original(name,schema,payload)
    model.ask=proposed
    result=run_followup(graph)
    assert result['response']['status']=='partial'
    assert result['response']['proposal']['request_text']=='Resume el informe anterior.'
    assert result['response']['proposal']['approved'] is False and collector.calls==[]
    assert store.read()[0]['events'][-1]['status']=='partial'


def test_schedule_intake_missing_cadence_keeps_a_reviewable_partial_proposal():
    graph,store,model,collector=followup_graph(None,intent='schedule_proposal')
    model.clarification='Faltan frecuencia, zona horaria y destino.'
    result=run_followup(graph)
    assert result['response']['kind']=='schedule_proposal' and result['response']['status']=='partial'
    assert result['response']['proposal']['frequency'] is None
    assert result['response']['proposal']['timezone'] is None
    assert result['response']['proposal']['destination'] is None
    assert model.calls==['v2_understand','v2_extract_proposal'] and collector.calls==[]
    assert not any(e['type']=='clarification.required' for e in store.read()[0]['events'])


@pytest.mark.parametrize('unsupported',[
    {'frequency':'cada día'}, {'timezone':'America/Bogota'}, {'destination':'correo externo'}, {'sources':['instagram']},
])
def test_schedule_rejects_unprovided_details_and_unavailable_sources_without_effects(unsupported):
    graph,store,model,collector=followup_graph(None,intent='schedule_proposal')
    original=model.ask
    async def proposed(name,schema,payload):
        if name=='v2_extract_proposal':
            return {'entity':'Tema','objective':'Investigar','sources':[],
                    'frequency':None,'timezone':None,'destination':None,**unsupported}
        return await original(name,schema,payload)
    model.ask=proposed
    result=run_followup(graph)
    assert result['response']['status']=='failed' and collector.calls==[]
    assert 'proposal' not in result['response']


@pytest.mark.parametrize('gap',[
    'No hay comparación pública de rendimiento entre estas marcas.',
    'No hay evidencia de 2026 sobre políticas o restricciones.',
])
def test_unverified_analysis_gap_stays_private_in_final_response_and_events(gap):
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.v2_wire import v2_wire_line
    store,model,collector,graph=v2_fixture()
    model.reject=True
    original=model.ask
    async def gap_analysis(name,schema,payload):
        result=await original(name,schema,payload)
        if name=='v2_analyze':
            result['gap']=gap
        return result
    model.ask=gap_analysis
    _,result=v2_run(graph)
    value=store.read()[0]
    assert value['domain_outcomes']['web']['gap']==gap
    response=result['response']
    assert response['coverage']['domain_outcomes']['web']['gap']=='La evidencia recuperada no permitió verificar todos los aspectos del encargo.'
    assert response['references']==[] and response['blocks']==[]
    assert gap not in v2_wire_line('response',response=response)
    assert all(gap not in v2_wire_line('event',event=event) for event in value['events'])
    _,replay=v2_run(graph)
    assert replay['response']==response and len(collector.calls)==1


def test_native_followup_uses_same_session_report_without_new_provider_starts(tmp_path,monkeypatch):
    runtime,storage,model,provider,calls=v2_native_fixture(tmp_path,monkeypatch)
    first=v2_native_turn(runtime)
    starts=len(provider.starts)
    followup_model(model)
    packets=v2_native_turn(runtime,question='Resume el informe anterior.',request_id='summary-1')
    assert packets[-1]['response']['coverage']['no_new_capture'] is True and len(provider.starts)==starts
    assert packets[-1]['response']['artifacts']==first[-1]['response']['artifacts']
    foreign=v2_native_turn(runtime,question='Resume el informe anterior.',request_id='summary-1',session='other-session')
    assert foreign[-1]['response']['status']=='needs_clarification' and len(provider.starts)==starts


def test_old_v2_session_without_report_pointer_recovers_the_original_after_a_summary(tmp_path,monkeypatch):
    import json
    runtime,storage,model,provider,_=v2_native_fixture(tmp_path,monkeypatch)
    original=v2_native_turn(runtime)[-1]['response']['markdown']
    followup_model(model)
    v2_native_turn(runtime,question='Resume el informe anterior.',request_id='first-summary')
    starts=len(provider.starts)
    for key,(raw,etag) in list(storage.objects.items()):
        value=json.loads(raw)
        if '/sessions/' in key:
            value['context']['report']=None
        elif key.endswith('/request.json') and value['request_id']=='request-1':
            value['response'].pop('kind')
        storage.objects[key]=(json.dumps(value).encode(),etag)
    ask=model.ask
    reports=[]
    async def observed(name,schema,payload):
        if name=='v2_summarize':
            reports.append(payload['prior_report'])
        return await ask(name,schema,payload)
    model.ask=observed
    v2_native_turn(runtime,question='Resume también el detalle original.',request_id='second-summary')
    session=next(json.loads(raw) for key,(raw,_) in storage.objects.items() if '/sessions/' in key)
    assert reports==[original] and len(provider.starts)==starts
    assert session['context']['report']['request_id']=='request-1'


def test_second_native_summary_keeps_the_original_report_as_its_canonical_source(tmp_path,monkeypatch):
    runtime,storage,model,provider,calls=v2_native_fixture(tmp_path,monkeypatch)
    original= model.ask
    async def complete_report(name,schema,payload):
        result=await original(name,schema,payload)
        if name=='v2_consolidate':
            result['text']='Hallazgo A. Detalle B conservado en el informe original.'
        return result
    model.ask=complete_report
    first=v2_native_turn(runtime)
    full=first[-1]['response']['markdown']
    starts=len(provider.starts)
    followup_model(model)
    wrapped=model.ask
    contexts=[]
    async def abbreviated(name,schema,payload):
        result=await wrapped(name,schema,payload)
        if name=='v2_summarize':
            contexts.append(payload['prior_report'])
            result['text']='Hallazgo A.'
        return result
    model.ask=abbreviated
    v2_native_turn(runtime,question='Resume en un punto.',request_id='summary-1')
    v2_native_turn(runtime,question='Resume también el detalle B.',request_id='summary-2')
    assert contexts==[full,full] and 'Detalle B' in full and len(provider.starts)==starts


def test_native_named_older_report_becomes_the_next_implicit_followup_source(tmp_path,monkeypatch):
    import json
    runtime,storage,model,provider,_=v2_native_fixture(tmp_path,monkeypatch)
    first=v2_native_turn(runtime)[-1]['response']
    v2_native_turn(runtime,question='Investiga una compañía diferente.',request_id='other-company')
    starts=len(provider.starts)
    followup_model(model)
    original=model.ask
    selections=[]
    async def older(name,schema,payload):
        if name=='v2_select_report':
            selections.append(payload['default_request_id'])
            return {'request_id':'request-1'}
        return await original(name,schema,payload)
    model.ask=older
    response=v2_native_turn(runtime,question='Resume el primer informe.',request_id='named-older')[-1]['response']
    assert response['coverage']['reused_request_id']=='request-1' and response['artifacts']==first['artifacts']
    v2_native_turn(runtime,question='Resume también los detalles.',request_id='implicit')
    assert selections==['other-company','request-1'] and len(provider.starts)==starts
    session=next(json.loads(raw) for key,(raw,_) in storage.objects.items() if '/sessions/' in key)
    assert session['context']['report']['request_id']=='request-1'
