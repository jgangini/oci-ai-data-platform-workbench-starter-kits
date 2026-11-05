"""Behavior checks for the current request graph and public events; no paid services."""
import asyncio
import copy
import importlib
import sys

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.runtime_core import Conflict, Scope
from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.artifacts import artifacts
from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.state import MarketRunStore
from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.workflow import MarketWorkflow


class MarketSnapshot:
    maximum_bytes=4*1024*1024

    def __init__(self):
        self.scope=Scope('fixture-tenant','fixture-user','d27966a4-6b46-4734-a672-b648a3e7e375')
        self.value=None
        self.etag=0

    def read(self):
        return copy.deepcopy(self.value),self.etag

    def write(self,value,etag):
        if etag!=self.etag:
            raise Conflict('Concurrent fixture update')
        self.value=copy.deepcopy(value)
        self.etag+=1


def market_evidence(source='web',number=1):
    return {'evidence_id':f'evidence-{source}-{number}','source':source,'discovery_channel':'web',
            'url':f'https://example.com/{source}/{number}','text':f'El producto tuvo {number} reclamaciones en la muestra revisada.',
            'published_at':'2026-11-01T12:00:00+00:00','observed_at':'2026-11-02T12:00:00+00:00',
            'receipt_id':f'receipt-{source}-{number}','content_basis':'page_text','financial':None,'limitations':[]}


def test_market_context_omissions_are_durable_and_force_partial_even_when_model_has_no_gap():
    store,model,collector,graph=market_fixture(('web',))
    rows=[{**market_evidence('web',number),'text':'A'*12000} for number in range(1,21)]
    async def collect(**kwargs):
        return {'status':'complete','evidence':rows,'limitations':[],'operation_id':'large-documents'}
    collector.collect=collect
    events,state=market_run(graph)
    coverage=state['response']['coverage']['domain_outcomes']['web']['evidence_coverage']
    assert coverage['total']==20 and 0<coverage['included']<20 and coverage['omitted']==20-coverage['included']
    assert state['response']['status']=='partial'
    assert any('Análisis parcial: se revisaron' in limit for limit in state['response']['limitations'])
    draft=next(event for event in events if event['type']=='draft.upsert')
    assert draft['coverage']['evidence_coverage']==coverage


def test_calendar_date_intake_is_normalized_to_inclusive_utc_days_before_capture():
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.research_contracts import MarketBrief
    raw={'entity':'Tiendas D1','market':'Colombia','objective':'Publicaciones de octubre',
         'start':'2026-10-01','end':'2026-10-31','sources':['facebook'],
         'clarification':None,'assumptions':[],'retrieval_mode':'conversation'}
    brief=MarketBrief.model_validate(raw)
    assert brief.start=='2026-10-01T00:00:00+00:00'
    assert brief.end=='2026-10-31T23:59:59.999999+00:00'
    offset=MarketBrief.model_validate({**raw,'start':'2026-10-01T00:00:00-05:00'})
    assert offset.start=='2026-10-01T05:00:00+00:00'
    for value in ('2026-02-30','2026-10-01T12:00:00','not a date'):
        with pytest.raises(ValueError):
            MarketBrief.model_validate({**raw,'start':value})


def test_date_only_model_output_reaches_social_collection_without_another_model_call():
    store,model,collector,graph=market_fixture(('facebook',))
    original=model.ask
    async def calendar(name,schema,payload):
        result=await original(name,schema,payload)
        if name=='market_understand':
            result.update(start='2026-10-01',end='2026-10-31')
        return result
    model.ask=calendar
    market_run(graph)
    assert collector.calls[0]['brief']['start']=='2026-10-01T00:00:00+00:00'
    assert collector.calls[0]['brief']['end']=='2026-10-31T23:59:59.999999+00:00'
    assert model.calls.count('market_understand')==1


class MarketFixtureCollector:
    def __init__(self):
        self.calls=[]
        self.pending=False

    async def collect(self,**arguments):
        self.calls.append(copy.deepcopy(arguments))
        source=arguments['task']['source']
        return {'status':'pending' if self.pending else 'complete',
                'evidence':[] if self.pending else [market_evidence(source,len(self.calls))],
                'limitations':[],'operation_id':'fixture-operation'}


class MarketFixtureModel:
    def __init__(self,sources=('web',)):
        self.sources=sources
        self.calls=[]
        self.clarification=None
        self.reject=False
        self.refine=False
        self.unsupported=False

    async def ask(self,name,schema,payload):
        self.calls.append(name)
        if name=='market_understand':
            return {'entity':'Fixture brand','market':'Colombia','objective':'Describir reclamaciones',
                    'start':'2026-10-26T00:00:00+00:00','end':'2026-11-02T23:59:59+00:00',
                    'sources':list(self.sources),'clarification':self.clarification,'assumptions':[]}
        if name=='market_plan':
            return {'tasks':[{'task_id':'task-'+source,'source':source,'role':'market_analysis',
                              'query':'Fixture brand reclamaciones','urls':[],'purpose':'Revisar la muestra'} for source in self.sources]}
        if name=='market_extract_proposal':
            return {'entity':payload['brief']['entity'],'objective':payload['brief']['objective'],
                    'sources':[],'frequency':None,'timezone':None,'destination':None}
        if name=='market_repair_units':
            return {'units':[{'unit_id':'u1','text':'La muestra contiene reclamaciones atribuidas.',
                'evidence_ids':[row['evidence_id'] for row in payload['evidence']],
                'limitations':['Muestra acotada; no es representativa.']}]}
        if name=='market_verify_repair':
            row=payload['evidence'][0]
            return {'reviews':[{'unit_id':unit['unit_id'],'accepted':not self.reject,
                'issues':[{'claim':unit['text'],'evidence_id':row['evidence_id'],
                    'quoted_text':row['text'],'reason':'No respalda la unidad.'}] if self.reject else []}
                for unit in payload['units']]}
        if name=='market_select_sources':
            return {'candidate_ids':[row['candidate_id'] for row in payload['candidates'][:3]]}
        if name=='market_analyze':
            identities=[row['evidence_id'] for row in payload['evidence']]
            return {'findings':[{'claim':'La muestra contiene reclamaciones atribuidas.', 'kind':'observation',
                                'supports':identities,'refutes':[],'limitations':['Muestra acotada.']}] if identities else [],
                    'refinement_query':'Fixture brand reclamaciones contraste' if self.refine else None,
                    'gap':'Falta contraste de otra publicación' if self.refine else None}
        if name in {'market_draft','market_consolidate'}:
            return {'text':'La muestra contiene reclamaciones atribuidas.',
                    'evidence_ids':['unowned-evidence'] if self.unsupported else [row['evidence_id'] for row in payload['evidence']],
                    'limitations':['Muestra acotada; no es representativa.']}
        if name=='market_verify':
            row=payload['evidence'][0]
            return {'accepted':not self.reject,'issues':[{'claim':payload['block']['text'],
                    'evidence_id':row['evidence_id'],'quoted_text':row['text'],'reason':'No respalda el borrador.'}] if self.reject else []}
        raise AssertionError(name)


def market_fixture(sources=('web',),event_limit=256):
    snapshot=MarketSnapshot()
    store=MarketRunStore(snapshot,'fixture-current-revision',event_limit=event_limit)
    model=MarketFixtureModel(sources)
    collector=MarketFixtureCollector()
    workflow=MarketWorkflow(store,model,collector,enabled_sources=sources)
    graph=workflow.build(InMemorySaver())
    return store,model,collector,graph


def test_planning_analysis_draft_review_and_final_preserve_question_and_admitted_brief():
    store,model,_,graph=market_fixture(('youtube',))
    original=model.ask
    contexts=[]
    async def ask(name,schema,payload):
        if name in {'market_plan','market_analyze','market_draft','market_verify','market_consolidate'}:
            contexts.append((name,payload['question'],copy.deepcopy(payload['brief'])))
        return await original(name,schema,payload)
    model.ask=ask
    _,state=market_run(graph)
    assert state['response']['status']=='complete'
    assert {name for name,_,_ in contexts}=={'market_plan','market_analyze','market_draft','market_verify','market_consolidate'}
    assert all(question==store.read()[0]['question'] for _,question,_ in contexts)
    assert all(brief==store.read()[0]['brief'] for _,_,brief in contexts)
    assert contexts[0][2]['start']=='2026-10-26T00:00:00+00:00'


def test_financial_document_is_collected_before_auxiliary_web_in_financial_mode():
    store,model,collector,graph=market_fixture(('web','financial_document'))
    original=model.ask
    async def ask(name,schema,payload):
        result=await original(name,schema,payload)
        if name=='market_understand':result['retrieval_mode']='financial'
        if name=='market_plan':assert payload['now']==store.read()[0]['started_at']
        return result
    model.ask=ask
    market_run(graph)
    assert [call['task']['source'] for call in collector.calls]==['financial_document','web']


def test_uncited_document_is_available_to_reject_false_global_inventory_claim():
    store,model,collector,graph=market_fixture()
    first=market_evidence('web',1)
    second={**market_evidence('web',2),'text':'La página corporativa identifica al operador del sitio.'}
    original=model.ask
    reviews=[]
    async def collect(**kwargs):
        return {'status':'complete','evidence':[first,second],'limitations':[],'operation_id':'two-pages'}
    async def ask(name,schema,payload):
        result=await original(name,schema,payload)
        if name=='market_analyze':
            result['findings'][0]['supports']=[first['evidence_id']]
        elif name=='market_draft':
            assert payload['retrieved_inventory']['distinct_urls']==2
            result.update(evidence_ids=[first['evidence_id']],text='El documento citado contiene una reclamación atribuida.' if payload.get('rejected_block') else 'La única fuente recuperada es el primer documento.')
        elif name=='market_consolidate':
            result.update(evidence_ids=[first['evidence_id']],text='El documento citado contiene una reclamación atribuida.')
        elif name in {'market_verify','market_verify_repair'}:
            reviews.append(copy.deepcopy(payload))
            assert payload['cited_evidence_ids']==[first['evidence_id']]
            assert [r['evidence_id'] for r in payload['evidence']]==[first['evidence_id'],second['evidence_id']]
            assert payload['retrieved_inventory']=={'observations':2,'distinct_urls':2,'by_source':{'web':2}}
            if name=='market_verify' and 'única fuente recuperada' in payload['block']['text']:
                return {'accepted':False,'issues':[{'claim':payload['block']['text'],'evidence_id':second['evidence_id'],
                    'quoted_text':second['text'],'reason':'También se recuperó este otro documento.'}]}
        return result
    collector.collect,model.ask=collect,ask
    events,state=market_run(graph)
    assert state['response']['status']=='complete' and len(reviews)==3
    assert all('única fuente recuperada' not in event['text'] for event in events)
    assert len(state['response']['references'])==1 and len(store.read()[0]['evidence'])==2


def test_review_cannot_accept_when_context_omits_a_cited_page_even_if_an_uncited_page_fits():
    store,model,collector,_=market_fixture()
    store.begin('large-review','Revisa documentos')
    cited=[{**market_evidence('web',i),'text':'A'*12000} for i in range(1,9)]
    extra={**market_evidence('web',9),'text':'Documento breve no citado.'}
    store.save(evidence={row['evidence_id']:row for row in [*cited,extra]})
    workflow=MarketWorkflow(store,model,collector,enabled_sources=('web',))
    block={'text':'Resumen atribuido. '*200,'evidence_ids':[row['evidence_id'] for row in cited],'limitations':[]}
    review=asyncio.run(workflow.review_block(block))
    assert review['accepted'] is False and model.calls==[]
    assert review['issues'][0]['evidence_id'] in block['evidence_ids']
    assert review['issues'][0]['quoted_text'] in store.read()[0]['evidence'][review['issues'][0]['evidence_id']]['text']
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.contracts import FactualReview
    FactualReview.model_validate(review)


def market_run(graph,request_id='fixture-request'):
    async def run():
        config={'configurable':{'thread_id':request_id}}
        events=[event async for event in graph.astream({'request_id':request_id,'question':'¿Qué reclamaciones aparecen en la muestra?'},
                                                      config,stream_mode='custom')]
        state=await graph.aget_state(config)
        return events,state.values
    return asyncio.run(run())


@pytest.mark.parametrize('expand',[False,True])
def test_plan_schema_and_payload_are_limited_to_the_sources_admitted_by_intake(expand):
    selected=('web','x','youtube')
    store,model,collector,_=market_fixture(selected)
    actual=model.ask
    async def ask(name,schema,payload):
        result=await actual(name,schema,payload)
        if name=='market_plan':
            assert tuple(payload['enabled_sources'])==selected
            assert schema['$defs']['MarketTask']['properties']['source']['enum']==list(selected)
            assert schema['properties']['tasks']['maxItems']==3
            if expand:
                result['tasks'].append({'task_id':'unadmitted-facebook','source':'facebook','role':'market_analysis',
                    'query':'Fixture brand','urls':[],'purpose':'Not admitted by intake'})
        return result
    model.ask=ask
    graph=MarketWorkflow(store,model,collector,enabled_sources=(*selected,'facebook')).build(InMemorySaver())
    _,state=market_run(graph)
    if expand:
        assert state['response']['status']=='failed' and collector.calls==[]
    else:
        assert state['delivered'] and len(collector.calls)==3


def test_market_streams_verified_business_content_before_final_without_legacy_records(monkeypatch):
    import builtins
    original=builtins.__import__
    def reject_business_schema(name,*args,**kwargs):
        if name in {'schema','repository','execution','research','catalog'}:
            raise AssertionError('Current must not load the business tables')
        return original(name,*args,**kwargs)
    monkeypatch.setattr(builtins,'__import__',reject_business_schema)
    store,model,collector,graph=market_fixture(('facebook','instagram'))
    events,state=market_run(graph)
    drafts=[event for event in events if event['type']=='draft.upsert']
    assert [event['source_key'] for event in drafts]==['facebook','instagram']
    assert all(event['role_key']=='market_analysis' and event['discovery_channel']=='web' for event in drafts)
    assert all(event['evidence_ids'] and 'reclamaciones' in event['text'] for event in drafts)
    assert events[-1]['type']=='run.completed' and drafts[0]['sequence']<events[-1]['sequence']
    assert state['response']['status']=='complete' and state['delivered']
    assert state['response']['artifacts'][0]['kind']=='bar'
    assert len(collector.calls)==2 and model.calls.count('market_plan')==1
    assert not {'tables','schema_sha256','projection_receipts'}.intersection(store.read()[0])


def test_market_material_clarification_starts_no_collection():
    store,model,collector,graph=market_fixture()
    model.clarification='¿A qué entidad te refieres?'
    events,state=market_run(graph)
    assert collector.calls==[] and model.calls==['market_understand']
    assert events[0]['type']=='clarification.required'
    assert state['response']['status']=='needs_clarification'


def test_market_rejected_prose_is_never_sent_as_a_provisional_finding():
    store,model,collector,graph=market_fixture()
    model.reject=True
    events,state=market_run(graph)
    assert not any(event['type'].startswith('draft.') for event in events)
    assert state['response']['status']=='partial'
    assert 'No fue posible publicar hallazgos verificados' in state['response']['markdown']
    assert 'No se obtuvo evidencia' not in state['response']['markdown']
    assert state['response']['references']==[] and store.read()[0]['evidence']
    assert model.calls.count('market_draft')==1 and model.calls.count('market_repair_units')==1
    assert model.calls.count('market_verify')==1 and model.calls.count('market_verify_repair')==1
    assert len(collector.calls)==1


def test_market_one_corrective_draft_is_reviewed_without_another_capture_or_bad_publication():
    store,model,collector,graph=market_fixture()
    ask=model.ask
    async def repair(name,schema,payload):
        result=await ask(name,schema,payload)
        if name=='market_draft':
            result['text']='Unsupported attribution.' if not payload.get('revision') else 'La muestra contiene reclamaciones atribuidas.'
        if name=='market_verify' and payload['block']['text']=='Unsupported attribution.':
            result={'accepted':False,'issues':[{'claim':payload['block']['text'],
                'evidence_id':payload['evidence'][0]['evidence_id'],'quoted_text':payload['evidence'][0]['text'],
                'reason':'Attribution does not match the document.'}]}
        return result
    model.ask=repair
    events,state=market_run(graph)
    assert model.calls.count('market_draft')==1 and model.calls.count('market_repair_units')==1
    assert model.calls.count('market_verify')==2 and model.calls.count('market_verify_repair')==1
    assert len(collector.calls)==1 and state['response']['status']=='complete'
    assert len([event for event in events if event['type']=='draft.upsert'])==1
    assert all('Unsupported attribution.' not in event['text'] for event in events)
    assert len(store.read()[0]['draft_repairs'])==1
    assert next(iter(store.read()[0]['draft_repairs'])).startswith('web:')


def test_repair_reservation_cannot_be_reset_by_another_collection_of_the_same_source():
    store,_,_,_=market_fixture()
    store.begin('one','Research')
    assert store.reserve_draft_repair('web','a'*64) is True
    assert store.reserve_draft_repair('web','a'*64) is True
    assert store.reserve_draft_repair('web','b'*64) is False


def test_market_unknown_citation_fails_before_publication():
    store,model,collector,graph=market_fixture()
    model.unsupported=True
    events,state=market_run(graph)
    assert state['response']['status']=='failed'
    assert state['response']['failure']=={'stage':'draft','error_type':'ValueError'}
    assert [event['type'] for event in events]==['run.failed']
    assert store.snapshot.value['evidence'] and store.snapshot.value['response'] is None


def test_market_analysis_schema_constrains_citations_and_rejects_the_observed_sentence_as_id():
    store,model,collector,graph=market_fixture()
    ask=model.ask
    async def invalid_analysis(name,schema,payload):
        result=await ask(name,schema,payload)
        if name=='market_analyze':
            allowed=[row['evidence_id'] for row in payload['evidence']]
            assert schema['$defs']['MarketFinding']['properties']['supports']['items']['enum']==allowed
            result['findings'][0]['supports']=['El fragmento no nombra un fabricante.']
        return result
    model.ask=invalid_analysis
    events,state=market_run(graph)
    assert state['response']['failure']=={'stage':'analyze','error_type':'ValueError'}
    assert [event['type'] for event in events]==['run.failed']
    assert 'market_draft' not in model.calls and len(collector.calls)==1


def test_market_one_justified_refinement_per_source_then_mandatory_close():
    store,model,collector,graph=market_fixture(('x',))
    model.refine=True
    events,state=market_run(graph)
    assert len(collector.calls)==2 and model.calls.count('market_plan')==1
    assert len({call['operation_key'] for call in collector.calls})==2
    assert any(event['type']=='draft.corrected' and event['supersedes'] for event in events)
    assert state['delivered'] and events[-1]['type']=='run.completed'
    assert state['response']['status']=='partial'
    assert 'Falta contraste' in store.read()[0]['domain_outcomes']['x']['gap']
    assert all('Falta contraste' not in gap for gap in state['response']['limitations'])
    assert state['response']['coverage']['domain_outcomes']['x']['gap']=='La evidencia recuperada no permitió verificar todos los aspectos del encargo.'


def test_web_closes_after_one_search_even_when_the_model_proposes_refinement():
    store,model,collector,graph=market_fixture(('web',))
    model.refine=True
    _,state=market_run(graph)
    assert len(collector.calls)==1 and store.read()[0]['refinements']=={}
    assert state['delivered'] and state['response']['status']=='partial'


def test_market_cached_delivery_reuses_evidence_and_never_collects_again():
    store,model,collector,graph=market_fixture()
    first,state=market_run(graph)
    prior_calls=list(model.calls)
    second,second_state=market_run(graph)
    assert len(collector.calls)==1 and model.calls==prior_calls
    assert second_state['response']==state['response'] and second==[]
    assert store.replay()['events']==first


def test_market_pending_capture_resumes_the_same_provider_operation():
    store,model,collector,graph=market_fixture()
    collector.pending=True
    events,state=market_run(graph)
    assert state['response']['status']=='pending' and not any(event['type']=='run.completed' for event in events)
    collector.pending=False
    events,state=market_run(graph)
    assert collector.calls[1]['previous_operation_id']=='fixture-operation'
    assert collector.calls[0]['operation_key']==collector.calls[1]['operation_key']
    assert state['response']['status']=='complete'


def test_market_replay_expiry_returns_authorized_snapshot_and_publication_is_idempotent():
    store,model,collector,graph=market_fixture(event_limit=1)
    events,state=market_run(graph)
    replay=store.replay(0)
    assert replay['events']==[] and replay['snapshot']['blocks']['source-web']['text']
    assert replay['snapshot']['response']==state['response']
    value=store.read()[0]
    final=events[-1]
    replayed=store.append(final['type'],text=final['text'],evidence_ids=final['evidence_ids'],
                         artifact_ids=final['artifact_ids'],status=final['status'],
                         coverage=final['coverage'],limitations=final['limitations'])
    assert replayed==final and store.read()[0]['next_sequence']==value['next_sequence']


@pytest.mark.parametrize('mutation',[{'scope':{'tenant_id':'other'}},{'candidate_revision':'other'}])
def test_market_rejects_cross_owner_and_stale_candidate_snapshots(mutation):
    store,model,collector,graph=market_fixture()
    store.begin('fixture-request','Question')
    store.snapshot.value.update(mutation)
    with pytest.raises(ValueError,match='another owner or candidate'):
        store.read()


def test_repeated_capture_of_the_same_publication_does_not_inflate_source_counts():
    first=market_evidence('facebook',)
    repeated={**first,'evidence_id':'new-capture','receipt_id':'another-receipt'}
    other=market_evidence('instagram')
    chart=next(artifact for artifact in artifacts([first,repeated,other]) if artifact['kind']=='bar')
    assert chart['rows']==[{'source':'facebook','value':1},{'source':'instagram','value':1}]
    assert set(chart['evidence_ids'])=={first['evidence_id'],repeated['evidence_id'],other['evidence_id']}


def test_market_financial_plot_keeps_nulls_and_separates_units():
    first=market_evidence()
    first['financial']={'metric':'value','entity':'CO','indicator':'INDICATOR','unit':'USD','currency':'USD',
                        'points':[{'period':'2024','value':None},{'period':'2025','value':12.0}]}
    second=market_evidence('world_bank',2)
    second['financial']={**first['financial'],'unit':'percent','currency':None}
    charts=[artifact for artifact in artifacts([first,second]) if artifact['kind']=='line']
    assert len(charts)==2 and all(chart['rows'][0]['value'] is None for chart in charts)
    assert {chart['unit'] for chart in charts}=={'USD','percent'}
    conflicting=copy.deepcopy(first)
    conflicting['evidence_id']='conflicting'
    conflicting['financial']['points'][1]['value']=13.0
    assert artifacts([first,conflicting])==[]
