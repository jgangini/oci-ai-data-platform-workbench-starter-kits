"""Frozen evaluation reruns v2 decisions, never an inherited answer or a legacy graph."""
import ast
import copy
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from market_v2_flow_factory import v2_evaluation_graph,v2_checked_json
from test_market_intelligence_v2 import v2_fixture,v2_run,V2FixtureModel


def test_frozen_evaluation_discards_prior_answer_and_approvals_but_keeps_captured_documents():
    store,model,collector,graph=v2_fixture()
    v2_run(graph)
    captured=store.read()[0]
    original=copy.deepcopy(captured)
    fresh_model=V2FixtureModel()
    fresh,new_store,frozen_collector=v2_evaluation_graph(captured,fresh_model,InMemorySaver(),revision='candidate-v2')
    _,state=v2_run(fresh,request_id='fresh-evaluation')
    assert state['delivered'] and fresh_model.calls[:2]==['v2_understand','v2_plan']
    assert frozen_collector.calls and new_store.read()[0]['candidate_revision']=='candidate-v2'
    assert captured==original and new_store.read()[0]['request_id']=='fresh-evaluation'


def test_frozen_evaluation_rejects_a_different_question_before_calling_model():
    store,model,collector,graph=v2_fixture()
    v2_run(graph)
    fresh_model=V2FixtureModel()
    fresh,_,_=v2_evaluation_graph(store.read()[0],fresh_model,InMemorySaver(),revision='candidate-v2')
    import asyncio
    with pytest.raises(ValueError,match='different question'):
        asyncio.run(fresh.ainvoke({'request_id':'other','question':'Another question'},
                                 config={'configurable':{'thread_id':'fixture'}}))
    assert fresh_model.calls==[]


def test_frozen_followup_reuses_the_same_owned_report_without_retrieval():
    from test_market_intelligence_v2_followup import saved_fixture,followup_model
    prior=saved_fixture()
    frozen=copy.deepcopy(prior)
    frozen['question']='Resume el informe en tres puntos.'
    frozen['brief'].update(intent='reuse_report',sources=[])
    model=V2FixtureModel()
    followup_model(model)
    graph,store,collector=v2_evaluation_graph(frozen,model,InMemorySaver(),revision='followup-evaluation',
        conversation_context={'prior_report':prior})
    import asyncio
    state=asyncio.run(graph.ainvoke({'request_id':'frozen-followup','question':frozen['question']},
        config={'configurable':{'thread_id':'frozen-followup'}}))
    assert state['response']['kind']=='report_followup'
    assert state['response']['coverage']['no_new_capture'] is True and collector.calls==[]
    assert model.calls==['v2_understand','v2_select_report','v2_summarize','v2_verify_summary']
    assert store.read()[0]['candidate_revision']=='followup-evaluation'
    prior['scope']['owner_id']='foreign-owner'
    with pytest.raises(ValueError,match='another authenticated session'):
        v2_evaluation_graph(frozen,V2FixtureModel(),InMemorySaver(),revision='followup-evaluation',
            conversation_context={'prior_report':prior})


def test_frozen_clarification_is_evaluated_without_inventing_captured_documents():
    store,_,_,graph=v2_fixture()
    v2_run(graph)
    frozen=store.read()[0]
    frozen['evidence']={}
    frozen['brief'].update(clarification='Comparte el enlace público del anuncio.',sources=[])
    model=V2FixtureModel()
    actual=model.ask
    async def clarify(name,schema,payload):
        result=await actual(name,schema,payload)
        if name=='v2_understand':
            result.update(clarification=frozen['brief']['clarification'],sources=[])
        return result
    model.ask=clarify
    fresh,_,collector=v2_evaluation_graph(frozen,model,InMemorySaver(),revision='clarification-evaluation',enabled_sources=('web',))
    _,state=v2_run(fresh)
    assert state['response']['status']=='needs_clarification' and collector.calls==[]
    assert model.calls==['v2_understand']
    frozen['brief']['clarification']=None
    with pytest.raises(ValueError,match='requires captured documents'):
        v2_evaluation_graph(frozen,model,InMemorySaver(),revision='clarification-evaluation',enabled_sources=('web',))


def test_frozen_evaluation_preserves_the_explicit_financial_request_context():
    store,_,_,graph=v2_fixture(('world_bank',))
    v2_run(graph)
    context={'financial_selections':{'world_bank':{'arguments':{'countries':['CO']}}}}
    model=V2FixtureModel(('world_bank',))
    ask=model.ask
    inputs=[]
    async def capture(name,schema,payload):
        if name=='v2_understand':
            inputs.append(payload['conversation_context'])
        return await ask(name,schema,payload)
    model.ask=capture
    fresh,_,_=v2_evaluation_graph(store.read()[0],model,InMemorySaver(),revision='candidate-v2',
                                  conversation_context=context)
    v2_run(fresh,request_id='financial-frozen')
    assert inputs==[{**context,'history_lookup_available':False}]
    assert context=={'financial_selections':{'world_bank':{'arguments':{'countries':['CO']}}}}


def test_v2_factory_has_no_legacy_business_schema_or_workflow_imports():
    source=Path(__file__).with_name('market_v2_flow_factory.py').read_text(encoding='utf-8')
    imports=[node.module for node in ast.walk(ast.parse(source)) if isinstance(node,ast.ImportFrom)]
    assert not any(name and name.rsplit('.',1)[-1] in {'schema','repository','workflow','market_flow_factory'} for name in imports)


def test_frozen_input_must_match_hash_and_owning_root(tmp_path):
    path=tmp_path/'input.json'
    path.write_text('{}',encoding='utf-8')
    with pytest.raises(ValueError,match='changed'):
        v2_checked_json(tmp_path,'input.json','a'*64)
    with pytest.raises(ValueError,match='owning project'):
        v2_checked_json(tmp_path,'../outside.json','a'*64)


def test_frozen_corpus_overflow_is_rejected_before_model_setup():
    from market_v2_flow_factory import V2FrozenCollector
    from test_market_intelligence_v2 import v2_evidence
    with pytest.raises(ValueError,match='partition and review'):
        V2FrozenCollector([v2_evidence(number=i) for i in range(51)])


def test_frozen_social_targets_normalize_aliases_and_exclude_other_account_rows():
    import asyncio
    from market_v2_flow_factory import V2FrozenCollector
    from test_market_intelligence_v2 import v2_evidence
    rows=[{**v2_evidence('instagram',number),'url':f'https://instagram.com/p/{number}',
           'requested_url':f'https://instagram.com/{name}'} for number,name in [(1,'first'),(2,'second')]]
    collector=V2FrozenCollector(rows,explicit_urls=['https://www.instagram.com/first/'])
    task={'source':'instagram','urls':['https://www.instagram.com/first/']}
    result=asyncio.run(collector.collect(task=task,operation_key='one'))
    assert [row['evidence_id'] for row in result['evidence']]==[rows[0]['evidence_id']]
    with pytest.raises(ValueError,match='proposed or missing'):
        asyncio.run(collector.collect(task={**task,'urls':['https://instagram.com/second']},operation_key='two'))


def test_preinitialized_model_client_cannot_skip_identity_or_guardrail_verification():
    from types import SimpleNamespace
    from market_v2_flow_factory import build_graph
    with pytest.raises(ValueError,match='fresh model client'):
        build_graph({'external_effects_allowed':False,'model':SimpleNamespace(client=object())})
