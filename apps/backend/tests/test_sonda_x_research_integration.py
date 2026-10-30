"""Real tool preparation and case admission against synthetic approved evidence."""
import copy
import asyncio
import json
from pathlib import Path
from uuid import UUID

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app import sonda_contract as core
from app.labs.ai_social_network.notebooks.agent import sonda_research_tools as tools
from app.labs.ai_social_network.notebooks.agent import sonda_study as study

ROOT = Path(__file__).resolve().parents[3]/'contracts/sonda/v1/fixtures'
NOW = study.instant('2026-01-01T00:30:00Z')


def fixture(name):
    return json.loads((ROOT/name).read_bytes())


def x_case():
    message = fixture('01-configuration.json')
    config = message['payload']
    connector = config['connectors'][0]
    connector.update(connector_id='x',operation='search_posts',modes=['posts'])
    source = config['sources'][0]
    source.update(connector_id='x',platform='x',category='social',allowed_domains=[])
    query = config['queries'][0]
    query.update(mode='posts',target='#testhuginn')
    config.update(connectors=[connector],sources=[source],queries=[query])
    config['catalog_sha256'] = core.catalog_fingerprint(config['connectors'])
    config = core.Configuration.model_validate(config).model_dump(mode='json')
    observation = fixture('02-observations.json')['payload']['observations'][0]
    observation.update(document_type='post',platform='x',provider_id='synthetic-x-1',
        parent_document_key=None,url='https://x.com/qa_handle/status/123',author='qa_handle',
        title=None,text='#testhuginn no volveré a ese lugar. Mejor Coca-Cola, tiene mejor sabor.')
    observation.update(core.observation_fingerprints(observation))
    observation = core.Observation.model_validate(observation).model_dump(mode='json')
    evidence = study.evidence_for(observation,observation['text'])
    context = {'observations':[{'source_id':source['source_id'],'query_id':query['query_id'],
        'observation':observation}],'evidence':[evidence],'relevant_keys':[observation['observation_key']]}
    work = {'criterion':config['criteria'][0],'investigation_id':'a'*64,
        'scope':{key:message[key] for key in ('contract_version','contract_sha256','registry_id','generation_id')},
        'research_observations':context['observations'],'evidence':context['evidence'],
        'evaluations':[{'decision':'relevant','observation_key':observation['observation_key']}],
        'requests':[],'approved_query_ids':[query['query_id']],
        'budget_round':fixture('09-budget-round-open.json'),'source_limits':None}
    return config,context,work


def prepared(config,context,target):
    tool = tools.build_research_tools(config,study.rule_context(config['criteria'][0]),
        [config['queries'][0]['query_id']],research_context=context)[0]
    result = tool.invoke({'type':'tool_call','id':'synthetic-call','name':tool.name,
        'args':{'purpose':'Corroborar el contexto público citado.','max_items':1,'target_override':target}})
    return result.artifact


@pytest.mark.parametrize('target', ['@qa_handle','Coca-Cola'])
def test_real_tool_prepares_evidence_bound_request_without_provider_call(target):
    config,context,work = x_case()
    step = prepared(config,context,target)
    assert step['target_override'] == target
    requests,limits = study.case_research(config,work,{'requests':[step]},NOW)
    assert limits == [] and len(requests) == 1
    assert requests[0]['target_override'] == target
    assert requests[0]['evidence_ids'] == [work['evidence'][0]['evidence_id']]
    assert requests[0]['max_items'] == 1


@pytest.mark.parametrize('change', ['source','query','irrelevant','absent'])
def test_factory_rejects_dynamic_target_outside_trusted_case_context(change):
    config,context,_ = x_case()
    if change in ('source','query'):
        context['observations'][0][change+'_id'] = str(UUID(int=999))
    elif change == 'irrelevant':
        context['relevant_keys'] = []
    else:
        context = None
    with pytest.raises(ValueError):
        prepared(config,context,'Coca-Cola')


def test_same_phrase_case_preserves_request_identity_and_deduplicates_completed_work():
    config,context,work = x_case()
    first = prepared(config,context,'Coca-Cola')
    second = prepared(config,context,'coca-cola')
    assert first['step'] == second['step']
    requests,_ = study.case_research(config,work,{'requests':[first]},NOW)
    work['requests'] = [{'request':requests[0],'status':'completed','observations_committed':True}]
    again,_ = study.case_research(config,work,{'requests':[second]},NOW)
    assert again == []


def test_case_admission_rechecks_evidence_even_for_direct_typed_plan():
    config,context,work = x_case()
    step = prepared(config,context,'@qa_handle')
    invalid = copy.deepcopy(work)
    invalid['research_observations'][0]['query_id'] = str(UUID(int=999))
    requests,limits = study.case_research(config,invalid,{'requests':[step]},NOW)
    assert requests == [] and limits


def test_unreviewed_x_target_capability_is_not_exposed_in_tool_schema():
    config,_,_ = x_case()
    config['connectors'][0]['research_fields'] = []
    config['catalog_sha256'] = core.catalog_fingerprint(config['connectors'])
    tool = tools.build_research_tools(config,study.rule_context(config['criteria'][0]),
        [config['queries'][0]['query_id']])[0]
    assert 'target_override' not in tool.args_schema.model_json_schema()['properties']


@pytest.mark.parametrize('conflicting', [False,True])
def test_model_batch_deduplicates_equivalent_x_targets_but_not_different_purposes(conflicting):
    config,context,_ = x_case()
    bound = tools.build_research_tools(config,study.rule_context(config['criteria'][0]),
        [config['queries'][0]['query_id']],research_context=context)
    class Model:
        def bind_tools(self,definitions,**kwargs):
            return self
        async def ainvoke(self,messages):
            calls = [{'type':'tool_call','id':str(index),'name':bound[0].name,
                'args':{'purpose':'Otro propósito' if conflicting and index else 'Corroborar al autor',
                    'max_items':1,'target_override':target}}
                for index,target in enumerate(('@qa_handle','@QA_HANDLE'))]
            return AIMessage(content='',tool_calls=calls)
    operation = tools.plan_research_tools(Model(),[HumanMessage(content='Investiga el contexto.')],bound)
    if conflicting:
        with pytest.raises(ValueError,match='Conflicting duplicate'):
            asyncio.run(operation)
    else:
        result = asyncio.run(operation)
        assert len(result['requests']) == 1
        assert result['requests'][0]['target_override'] == '@qa_handle'
