"""Financial figures are quoted, attributed and converted by code before visualisation."""
import asyncio

import pytest

from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.financial import market_financial_rows
from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.financial import market_period_changes
from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.native import market_chat_input
from tests.modules.ai_market_intelligence.test_market_intelligence_graph import market_evidence, market_fixture, market_run


def fact_fixture():
    row=market_evidence('financial_document',)
    row['text']='Koba, individual, ingresos 2025: COP 1.234,50 millones.'
    fact={'evidence_id':row['evidence_id'],'entity':'Koba','basis':'individual','metric':'Ingresos',
          'period':'2025','currency':'COP','unit':'currency','scale':1000000,'amount_text':'1.234,50',
          'decimal_separator':',','quoted_text':row['text'],'value_kind':'exact'}
    return fact,row


def test_financial_exact_quote_and_declared_locale_scale_produce_canonical_value():
    fact,row=fact_fixture()
    result=market_financial_rows([fact],{row['evidence_id']:row})[0]
    assert result['financial']['points']==[{'period':'2025','value':1234500000.0}]
    assert result['financial']['entity']=='Koba · individual' and result['financial']['currency']=='COP'
    assert result['text']==row['text'] and result['evidence_id']==row['evidence_id']


@pytest.mark.parametrize('amount',['over $245 billion','más de 100 millones','between 10 and 20','approximately 2.5 million'])
def test_reported_financial_amounts_keep_the_literal_qualification_without_becoming_points(amount):
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.financial import market_bind_financial_facts
    fact,row=fact_fixture()
    row['text']='The publication reports '+amount+' for fiscal2025.'
    result=market_bind_financial_facts({'financial_facts':[{**fact,'amount_text':amount,'value_kind':'reported_text'}]},[row])
    bound=result['financial_facts'][0]
    assert bound['amount_text']==row['text'] and amount in bound['quoted_text']
    assert market_financial_rows(result['financial_facts'],{row['evidence_id']:row})==[]


@pytest.mark.parametrize('amount',['more than 246 billion','over 245'])
def test_reported_text_cannot_bypass_literal_and_complete_amount_validation(amount):
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.financial import market_bind_financial_facts
    fact,row=fact_fixture()
    row['text']='Turnover 245 billion.'
    with pytest.raises(ValueError,match='Reported financial text'):
        market_bind_financial_facts({'financial_facts':[{**fact,'amount_text':amount,'value_kind':'reported_text'}]},[row])


@pytest.mark.parametrize('text,amount',[('Revenue over $245 billion.','245 billion'),
    ('Revenue (100 million).','100 million'),('Ingresos de más de\n$245 billion.','$245 billion'),
    ('Ingresos de más de\n245\nmillones de unidades.','245')])
def test_reported_text_recovers_the_complete_qualification_and_accounting_context(text,amount):
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.financial import market_bind_financial_facts
    fact,row=fact_fixture();row['text']=text
    result=market_bind_financial_facts({'financial_facts':[{**fact,'amount_text':amount,'value_kind':'reported_text'}]},[row])
    assert result['financial_facts'][0]['amount_text']==text
    assert market_financial_rows(result['financial_facts'],{row['evidence_id']:row})==[]
    with pytest.raises(ValueError,match='complete source paragraph'):
        market_financial_rows([{**fact,'amount_text':amount,'quoted_text':text,'value_kind':'reported_text'}],{row['evidence_id']:row})


@pytest.mark.parametrize('text,amount',[('Revenue 2.5 billion.','5 billion'),('Revenue -100 million.','100 million')])
def test_reported_text_cannot_drop_decimal_parts_or_signs(text,amount):
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.financial import market_bind_financial_facts
    fact,row=fact_fixture();row['text']=text
    with pytest.raises(ValueError,match='complete numeric token'):
        market_bind_financial_facts({'financial_facts':[{**fact,'amount_text':amount,'value_kind':'reported_text'}]},[row])


def test_reported_financial_paragraph_cannot_be_truncated_to_fit_its_bound():
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.financial import market_bind_financial_facts
    fact,row=fact_fixture();row['text']='A'*1001+' over $245 billion.'
    with pytest.raises(ValueError,match='complete-text bound'):
        market_bind_financial_facts({'financial_facts':[{**fact,'amount_text':'over $245 billion','value_kind':'reported_text'}]},[row])


@pytest.mark.parametrize('context',['(1).','(2025).'])
def test_reported_paragraph_keeps_contextual_numbers_without_reinterpreting_them_as_amounts(context):
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.financial import market_bind_financial_facts
    fact,row=fact_fixture();row['text']='Revenue over $245 billion '+context
    result=market_bind_financial_facts({'financial_facts':[{**fact,'amount_text':'over $245 billion','value_kind':'reported_text'}]},[row])
    assert result['financial_facts'][0]['amount_text']==row['text']
    assert market_financial_rows(result['financial_facts'],{row['evidence_id']:row})==[]


@pytest.mark.parametrize('text,amount',[('Año 2023: 100','23'),('Importe -100','100'),
    ('Importe 1,234.50','234.50'),('Importe 1.234,50','1.234'),('Importe +100','100'),
    ('Importe −100','100'),('Importe − 100','100'),('Importe (100)','100')])
def test_financial_amount_cannot_be_a_subtoken_or_lose_its_sign(text,amount):
    fact,row=fact_fixture()
    row['text']=text
    with pytest.raises(ValueError,match='complete numeric token'):
        market_financial_rows([{**fact,'quoted_text':text,'amount_text':amount}],{row['evidence_id']:row})


def test_literal_unicode_minus_is_preserved_and_scaled_as_negative():
    fact,row=fact_fixture()
    row['text']='Resultado 2025: COP −100 millones.'
    result=market_financial_rows([{**fact,'amount_text':'−100','quoted_text':row['text']}],{row['evidence_id']:row})
    assert result[0]['financial']['points'][0]['value']==-100000000


def test_analyze_binds_financial_quote_to_included_page_without_model_reconstruction():
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.research_contracts import MarketAnalysis
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.workflow import MarketWorkflow
    store,model,collector,_=market_fixture(('financial_document',))
    store.begin('financial','Extrae ingresos')
    fact,row=fact_fixture()
    contracts=[]
    async def extract(name,schema,payload):
        contracts.append(schema)
        return {'findings':[],'refinement_query':None,'gap':None,
                'financial_facts':[{key:value for key,value in fact.items() if key!='quoted_text'}]}
    model.ask=extract
    workflow=MarketWorkflow(store,model,collector,enabled_sources=('financial_document',))
    result=asyncio.run(workflow.ask('market_analyze',MarketAnalysis,{'task':{'source':'financial_document'},'evidence':[row]},
                                 allowed_evidence={row['evidence_id']}))
    contract=contracts[0]['$defs']['MarketFinancialFact']
    assert 'quoted_text' not in contract['properties'] and 'quoted_text' not in contract['required']
    assert contract['properties']['evidence_id']['enum']==[row['evidence_id']]
    assert result['financial_facts']==[fact]
    assert market_financial_rows(result['financial_facts'],{row['evidence_id']:row})[0]['financial']['points'][0]['value']==1234500000


def test_empty_financial_capture_does_not_send_an_empty_enum_or_allow_facts():
    from jsonschema import Draft202012Validator
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.research_contracts import MarketAnalysis
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.workflow import MarketWorkflow
    store,model,collector,_=market_fixture(('financial_document',))
    store.begin('empty-financial','Compara ingresos')
    observed=[]
    async def check(name,schema,payload):
        observed.append(schema)
        assert 'enum' not in schema['$defs']['MarketFinancialFact']['properties']['evidence_id']
        return {'findings':[],'refinement_query':None,'gap':'No hay documentos.','financial_facts':[]}
    model.ask=check
    workflow=MarketWorkflow(store,model,collector,enabled_sources=('financial_document',))
    result=asyncio.run(workflow.ask('market_analyze',MarketAnalysis,{'task':{'source':'financial_document'},'evidence':[]},allowed_evidence=set()))
    validator=Draft202012Validator(observed[0]);validator.validate(result)
    fact,_=fact_fixture();fact.pop('quoted_text')
    errors=list(validator.iter_errors({**result,'financial_facts':[fact]}))
    assert any(error.validator=='maxItems' and list(error.path)==['financial_facts'] for error in errors)
    assert result['financial_facts']==[] and len(observed)==1


def test_page_binding_rejects_foreign_or_nonfinancial_pages_and_bounds_long_page_quotes():
    import json
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.financial import market_bind_financial_facts
    fact,row=fact_fixture()
    row['text']='Documento '*550+row['text']+' Anexo'*550
    result=market_bind_financial_facts({'financial_facts':[fact]*20},[row])
    assert len(json.dumps(result).encode())<20000
    assert all(len(value['quoted_text'])<=250 and value['quoted_text'] in row['text'] for value in result['financial_facts'])
    assert len(market_financial_rows(result['financial_facts'],{row['evidence_id']:row}))==20
    for evidence in ([],[{**row,'source':'web'}],[{**row,'evidence_id':'omitted-page'}]):
        with pytest.raises(ValueError,match='included document pages'):
            market_bind_financial_facts({'financial_facts':[fact]},evidence)


def test_currency_decoration_is_bound_to_its_literal_source_without_inferring_currency():
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.financial import market_bind_financial_facts
    fact,row=fact_fixture()
    row['text']='Total net sales 391,035 383,285\nTotal net sales $ 391,035 $ 383,285'
    values=[{**fact,'currency':None,'amount_text':amount,'decimal_separator':'.','period':period}
            for amount,period in [('$ 391,035','2024'),('$ 383,285','2023')]]
    bound=market_bind_financial_facts({'financial_facts':values},[row])['financial_facts']
    assert [f['amount_text'] for f in bound]==['391,035','383,285']
    assert all(f['currency'] is None and f['quoted_text'] in row['text'] and '$' in f['quoted_text'] for f in bound)
    rows=market_financial_rows(bound,{row['evidence_id']:row})
    assert [r['financial']['points'][0]['value'] for r in rows]==[391035000000.0,383285000000.0]
    assert all(r['financial']['currency'] is None for r in rows)


@pytest.mark.parametrize('amount',['114%','114 %','−2.5 %'])
def test_percent_decoration_binds_exact_source_as_percentage_points_without_rescaling(amount):
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.financial import market_bind_financial_facts
    fact,row=fact_fixture();row['text']='Reported change: '+amount+'.'
    value={**fact,'amount_text':amount,'unit':'percent','currency':None,'scale':1,'decimal_separator':'.'}
    bound=market_bind_financial_facts({'financial_facts':[value]},[row])['financial_facts']
    expected=float(amount.replace('%','').replace('−','-').strip())
    assert bound[0]['amount_text']==amount.replace('%','').strip()
    assert market_financial_rows(bound,{row['evidence_id']:row})[0]['financial']['points']==[{'period':'2025','value':expected}]


@pytest.mark.parametrize('text,amount,unit',[
    ('Change 114%','114 %','percent'),('Change 1140 %','114 %','percent'),
    ('Change -114 %','114 %','percent'),('Change (114 %)','114 %','percent'),
    ('Change 114 %','114 %','count'),('Change 114 %','114 %','currency')])
def test_percent_binding_never_changes_spacing_sign_subtokens_or_unit(text,amount,unit):
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.financial import market_bind_financial_facts
    fact,row=fact_fixture();row['text']=text
    with pytest.raises(ValueError,match='complete numeric token'):
        market_bind_financial_facts({'financial_facts':[{**fact,'amount_text':amount,'unit':unit,'currency':None,'scale':1}]},[row])


def test_normalized_percent_cannot_bypass_accounting_sign_validation():
    fact,row=fact_fixture();row['text']='Change (114 %)'
    value={**fact,'amount_text':'114','quoted_text':row['text'],'unit':'percent','currency':None,'scale':1}
    with pytest.raises(ValueError,match='complete numeric token'):
        market_financial_rows([value],{row['evidence_id']:row})


@pytest.mark.parametrize('text,amount,unit',[('Amount 100','$100','currency'),
    ('Amount $1000','$100','currency'),('Amount -$100','$100','currency'),
    ('Amount − $100','$100','currency'),('Amount ($100)','$100','currency'),
    ('Amount (US$100)','$100','currency'),('Amount -US$100','$100','currency'),
    ('Amount −CAD $100','$100','currency'),('Amount +US$100','$100','currency'),
    ('Amount $100','$100','count'),
    ('Amount -$100','100','currency'),('Amount ($100)','100','currency'),
    ('Amount $1.234,50','$1,234.50','currency')])
def test_currency_binding_never_adds_symbols_or_loses_signs_or_changes_locale(text,amount,unit):
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.financial import market_bind_financial_facts
    fact,row=fact_fixture();row['text']=text
    with pytest.raises(ValueError,match='complete numeric token'):
        market_bind_financial_facts({'financial_facts':[{**fact,'unit':unit,'amount_text':amount}]},[row])


def test_unknown_currency_keeps_scaled_chart_values_in_base_units_without_a_millions_label():
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.artifacts import artifacts
    fact,row=fact_fixture()
    fact['currency']=None
    normalized=market_financial_rows([fact],{row['evidence_id']:row})
    chart=artifacts(normalized)[0]
    assert chart['unit']=='unidades monetarias' and chart['currency'] is None
    assert chart['rows'][0]['value']==1234500000.0
    with pytest.raises(ValueError):
        market_financial_rows([{**fact,'unit':'millones'}],{row['evidence_id']:row})


@pytest.mark.parametrize('unit,currency,scale',[('count','COP',1),('percent',None,1000),('ratio',None,1000000)])
def test_nonmonetary_units_cannot_acquire_currency_or_extra_scale(unit,currency,scale):
    fact,row=fact_fixture()
    with pytest.raises(ValueError):
        market_financial_rows([{**fact,'unit':unit,'currency':currency,'scale':scale}],{row['evidence_id']:row})


def test_period_variations_keep_exact_intervals_nulls_and_both_supporting_sources():
    points=[{'period':'2022','value':100.0,'evidence_ids':['a']},
            {'period':'2024','value':125.0,'evidence_ids':['b']},
            {'period':'2025','value':None,'evidence_ids':['c']}]
    rows=market_period_changes(points)
    assert rows[0]['change_percent'] is None
    assert rows[1]['previous_period']=='2022' and rows[1]['change_absolute']==25.0 and rows[1]['change_percent']==25.0
    assert rows[1]['change_evidence_ids']==['a','b'] and rows[2]['change_absolute'] is None
    assert 'change_percent' not in points[0]


def test_zero_and_negative_baselines_do_not_produce_misleading_percentages():
    points=[{'period':'2023-01-01','value':0.0,'evidence_ids':['a']},
            {'period':'2023-01-03','value':-10.0,'evidence_ids':['b']},
            {'period':'2023-01-04','value':5.0,'evidence_ids':['c']}]
    rows=market_period_changes(points)
    assert rows[1]['change_absolute']==-10.0 and rows[1]['change_percent'] is None
    assert rows[2]['change_absolute']==15.0 and rows[2]['change_percent'] is None


def test_eleven_valid_financial_groups_all_reach_the_final_report_and_terminal_event():
    store,model,collector,graph=market_fixture(('world_bank',))
    rows=[]
    for index in range(11):
        row=market_evidence('world_bank',)
        row.update(evidence_id=f'indicator-{index}',text=f'Indicator {index}: 10',financial={
            'metric':'value','entity':'CO','indicator':f'indicator-{index}','unit':'units','currency':None,
            'points':[{'period':'2024','value':10.0}]})
        rows.append(row)
    async def collect(**kwargs):
        return {'status':'complete','evidence':rows,'limitations':[],'operation_id':'eleven-indicators'}
    collector.collect=collect
    events,state=market_run(graph)
    assert len(state['response']['artifacts'])==11
    assert len(events[-1]['artifact_ids'])==11 and state['response']['status']=='complete'


@pytest.mark.parametrize('change',[{'evidence_id':'foreign'},{'quoted_text':'Invented figure 4'},
    {'amount_text':'3.141,59'},{'amount_text':'1,234.50'},{'amount_text':'NaN'}])
def test_financial_foreign_or_inexact_or_ambiguous_amount_is_rejected(change):
    fact,row=fact_fixture()
    with pytest.raises(ValueError):
        market_financial_rows([{**fact,**change}],{row['evidence_id']:row})


def test_explicit_financial_api_selection_is_validated_without_model_authority():
    import json
    selection={'market_data':{'tickers':['AAPL'],'start_date':'2025-01-01','end_date':'2025-01-15'}}
    command=market_chat_input(json.dumps({'request_id':'stock-1','question':'Compara AAPL','financial_selections':selection}),
        'session','trace')
    assert command['financial_selections']['market_data']['approved_scope']=={'tickers':['AAPL']}
    selection['market_data']['tickers']=['ALL']
    with pytest.raises(ValueError,match='Invalid financial identifier'):
        market_chat_input(json.dumps({'request_id':'stock-1','question':'Compara','financial_selections':selection}),'session','trace')


@pytest.mark.parametrize('model_dates',[('2026-11-02T00:00:00Z','2026-11-02T00:00:00Z'),
                                       ('2025-01-02','2025-01-09'),(None,None)])
def test_documentary_intake_owns_capture_window_and_ignores_unused_model_dates(model_dates):
    store,model,collector,graph=market_fixture()
    ask=model.ask
    async def zero_duration(name,schema,payload):
        result=await ask(name,schema,payload)
        if name=='market_understand':
            result.update(retrieval_mode='financial',start=model_dates[0],end=model_dates[1])
        return result
    model.ask=zero_duration
    _,state=market_run(graph)
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.runtime_core import research_window
    start,end=research_window(store.read()[0]['brief'] and {key:store.read()[0]['brief'][key] for key in ('start','end')})
    assert (end-start).total_seconds()==86400 and state['response']['status']=='complete'


def test_explicit_financial_selection_constrains_intake_to_new_research():
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.workflow import MarketWorkflow
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.research_contracts import MarketBrief
    store,model,collector,_=market_fixture(('world_bank',))
    store.begin('financial','Compare the selected data')
    workflow=MarketWorkflow(store,model,collector,enabled_sources=('world_bank',))
    original=model.ask
    contracts=[]
    async def incorrect_intent(name,schema,payload):
        contracts.append(schema['properties']['intent']['enum'])
        return {**await original(name,schema,payload),'intent':'reuse_report'}
    model.ask=incorrect_intent
    payload={'question':'Compare the selected data','conversation_context':{
        'financial_selections':{'world_bank':{'arguments':{'countries':['CO']}}}}}
    with pytest.raises(ValueError,match='explicit financial research'):
        asyncio.run(workflow.ask('market_understand',MarketBrief,payload))
    assert contracts==[['research']] and collector.calls==[]


def test_verified_financial_series_streams_provisional_then_final_with_compaction():
    store,model,collector,graph=market_fixture(('financial_document',))
    fact,row=fact_fixture()
    ask=model.ask
    final_inputs=[]
    draft_inputs=[]
    reviews=[]
    async def collect(**kwargs):
        return {'status':'complete','evidence':[row],'limitations':[],'operation_id':'document-1'}
    async def numeric(name,schema,payload):
        result=await ask(name,schema,payload)
        if name=='market_analyze':
            result['financial_facts']=[fact]
        if name=='market_consolidate':
            final_inputs.append(payload)
        if name=='market_draft':
            draft_inputs.append(payload)
        if name=='market_verify':
            reviews.append(payload)
        return result
    model.ask,collector.collect=numeric,collect
    events,state=market_run(graph)
    charts=[event for event in events if event['type']=='artifact.upsert']
    assert [event['status'] for event in charts]==['provisional','final']
    assert charts[0]['artifact_ids']==charts[1]['artifact_ids']
    assert state['response']['artifacts'][0]['rows'][0]['value']==1234500000.0
    assert draft_inputs[0]['computed_artifacts'][0]['rows'][0]['value']==1234500000.0
    assert draft_inputs[0]['computed_artifacts'][0]['unit']=='COP'
    assert final_inputs[0]['artifacts'][0]['rows']==state['response']['artifacts'][0]['rows']
    assert state['response']['artifacts'][0]['status']=='final'
    assert 'status' not in final_inputs[0]['artifacts'][0] and 'status' not in reviews[-1]['computed_artifacts'][0]
    assert reviews[-1]['financial_facts']==[fact]
    assert reviews[-1]['computed_artifacts'][0]['rows'][0]['value']==1234500000.0
    assert any(event['type']=='draft.compacted' and event['supersedes'] for event in events)


def test_same_verified_prose_does_not_suppress_new_financial_points():
    store,model,collector,graph=market_fixture(('financial_document',))
    model.refine=True
    fact,row=fact_fixture()
    row['text']='Koba individual, COP millones: 2024 1.100,00; 2025 1.234,50.'
    first={**fact,'period':'2024','amount_text':'1.100,00','quoted_text':row['text']}
    second={**fact,'quoted_text':row['text']}
    ask=model.ask
    count=0
    async def collect(**kwargs):
        return {'status':'complete','evidence':[row],'limitations':[],'operation_id':'document-1'}
    async def numeric(name,schema,payload):
        nonlocal count
        result=await ask(name,schema,payload)
        if name=='market_analyze':
            count+=1
            result['financial_facts']=[first] if count==1 else [first,second]
        return result
    model.ask,collector.collect=numeric,collect
    events,state=market_run(graph)
    assert sum(event['type']=='draft.upsert' for event in events)==1
    chart=next(artifact for artifact in state['response']['artifacts'] if artifact['kind']=='line')
    assert [point['period'] for point in chart['rows']]==['2024','2025']
