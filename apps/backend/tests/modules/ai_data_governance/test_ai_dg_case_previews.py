import asyncio
from copy import deepcopy
import json
from unittest.mock import AsyncMock

import pytest

from app.modules.ai_data_governance.case_previews import validate_case_diagnosis, validate_case_treatment
from app.modules.ai_data_governance.contracts import GovIntent
from app.modules.ai_data_governance.governance_graph import build_governance_graph, READ_OPERATIONS, admitted_case_intent
from app.modules.ai_data_governance.native_governance import consultation_model_sources, restore_consultation_sources, render_governance_response
from tests.modules.ai_data_governance.test_ai_dg_response_rendering import answer_state, reviewed


def context():
    state=answer_state(); state['evidence'][0]['facts']['owner_ref']=None
    state['assets'][0]['version_id']=state['evidence'][0]['version_id']='c'*64
    review=reviewed(state).model_dump(mode='json')
    review['supported_claims'][0]['text']='No consta responsable en el catálogo observado.'
    diagnosis=dict(findings=review['supported_claims'],potential_impacts=[],limitations=[],treatment_needed=True)
    draft=dict(problem='No consta responsable en el catálogo',asset_ids=['asset'],expected_versions=['c'*64],
        suggested_action='Identificar al responsable y documentarlo',responsible_ref=None,
        verification_plan='Comprobar la asignación real del responsable',remaining_gaps=['Responsable pendiente'])
    return state,review,diagnosis,draft


def graph_fixture():
    from langgraph.checkpoint.memory import InMemorySaver
    state,review,diagnosis,draft=context()
    model=AsyncMock(side_effect=lambda phase,*_:json.dumps({'G1':{'intent':'treatment','asset_mentions':['research_runs']},
        'G3':review,'G4':diagnosis,'G5':draft,'G6':state['response']}[phase]))
    read=AsyncMock(side_effect=lambda operation,*_:state['evidence'] if operation=='read_asset_snapshot' else [])
    preview=AsyncMock(return_value=dict(available=True,action='OPEN',command='CONFIRMAR ABRIR '+'b'*32,
        expires_at='2026-11-01T01:00:00+00:00',case_ref=None,summary=draft['problem'],
        assessed_assets=[{**state['assets'][0],'version_id':'c'*64}],treatment=None))
    checks=AsyncMock()
    graph=build_governance_graph(authorize=AsyncMock(),resolve_assets=AsyncMock(return_value=state['assets']),
        read=read,call_model=model,capabilities=READ_OPERATIONS,checkpointer=InMemorySaver(),reauthorize_evidence=checks,
        prepare_case=preview)
    return graph,state,model,read,preview,checks


def test_real_stategraph_prepares_a_cited_diagnosis_and_exact_draft_then_only_a_confirmation_preview():
    graph,state,model,read,preview,checks=graph_fixture()
    result=asyncio.run(graph.ainvoke({'question':'Prepara un tratamiento para research_runs'},
        {'configurable':{'thread_id':'preview'}}))
    assert [call.args[0] for call in model.call_args_list]==['G1','G3','G4','G5','G6']
    assert result['treatment']['asset_ids']==['asset'] and result['diagnosis']['treatment_needed']
    assert result['response']['case_ref'] is None and result['response']['result_state']=='answered'
    assert result['case_preview']['action']=='OPEN' and preview.await_count==1
    assert len(checks.call_args_list)>=4
    text=render_governance_response(result)
    assert '[1] Catálogo:' in text and 'CONFIRMAR ABRIR '+'b'*32 in text
    assert 'Válida hasta: 2026-11-01 01:00:00 UTC' in text
    assert 'no ejecuta cambios' in text
    assert all('COMMIT' not in call.args[0] for call in read.call_args_list)


def test_next_question_does_not_reuse_a_draft_or_confirmation_from_a_checkpoint():
    graph,state,model,_,preview,_=graph_fixture()
    cfg={'configurable':{'thread_id':'preview-memory'}}
    asyncio.run(graph.ainvoke({'question':'Prepara un tratamiento para research_runs'},cfg))
    _,review,_,_=context()
    model.side_effect=lambda phase,*_:json.dumps({'G1':{'intent':'meaning','asset_mentions':['research_runs']},
        'G3':review,'G6':state['response']}[phase])
    result=asyncio.run(graph.ainvoke({'question':'¿Qué significa research_runs?'},cfg))
    assert result['diagnosis']=={} and result['treatment']=={} and result['case_preview']=={}
    assert preview.await_count==1 and len([c for c in model.call_args_list if c.args[0]=='G5'])==1
    assert 'CONFIRMAR' not in render_governance_response(result)


@pytest.mark.parametrize('coverage',['insufficient','conflicting'])
def test_no_diagnosis_or_draft_generation_from_an_insufficient_or_conflicting_review(coverage):
    graph,state,model,_,_,_=graph_fixture()
    _,review,_,_=context();review['coverage']=coverage
    response={**state['response'],'result_state':'partial'}
    model.side_effect=lambda phase,*_:json.dumps({'G1':{'intent':'treatment','asset_mentions':['research_runs']},
        'G3':review,'G6':response}[phase])
    result=asyncio.run(graph.ainvoke({'question':'Prepara tratamiento para research_runs'},
        {'configurable':{'thread_id':'insufficient-'+coverage}}))
    assert result['diagnosis']=={} and result['treatment']=={}
    assert not any(c.args[0] in {'G4','G5'} for c in model.call_args_list)


def test_a_model_diagnosis_cannot_make_catalog_metadata_into_an_observed_dependency():
    state,review,diagnosis,_=context()
    diagnosis['potential_impacts']=[dict(text='Afectaría a otra tabla',evidence_ids=['proof'],kind='inferred')]
    with pytest.raises(ValueError,match='observed relationship'):
        validate_case_diagnosis(diagnosis,review,state['evidence'])
    state['evidence'][0].update(kind='RUNTIME_LINEAGE',facts={'links':[{'from':'a','to':'b'}]})
    assert validate_case_diagnosis(diagnosis,review,state['evidence'])['potential_impacts']


def test_an_uncited_static_reference_does_not_validate_an_empty_lineage_impact():
    state,review,diagnosis,_=context()
    state['evidence'][0].update(kind='RUNTIME_LINEAGE',facts={'links':[]})
    static={**state['evidence'][0],'evidence_id':'static-proof','kind':'STATIC_CODE',
        'facts':{'references':[{'source_path':'/Workspace/jobs/orders.py'}]}}
    state['evidence'].append(static)
    review['supported_claims'].append(dict(text='Referencia estática observada',evidence_ids=['static-proof'],kind='observed'))
    diagnosis['potential_impacts']=[dict(text='No es posible acotar el impacto',evidence_ids=['proof'],kind='inferred')]
    with pytest.raises(ValueError,match='observed relationship'):
        validate_case_diagnosis(diagnosis,review,state['evidence'])
    diagnosis['potential_impacts']=[dict(text='El cambio podría afectar al código que referencia este activo',
        evidence_ids=['static-proof'],kind='inferred')]
    assert validate_case_diagnosis(diagnosis,review,state['evidence'])['potential_impacts']


def test_an_unsupported_g4_impact_stops_before_draft_response_or_confirmation():
    graph,state,model,_,preview,_=graph_fixture()
    _,review,diagnosis,_=context()
    diagnosis['potential_impacts']=[dict(text='No puede acotarse el impacto',evidence_ids=['proof'],kind='inferred')]
    model.side_effect=lambda phase,*_:json.dumps({'G1':{'intent':'treatment','asset_mentions':['research_runs']},'G3':review,'G4':diagnosis}[phase])
    with pytest.raises(ValueError,match='observed relationship'):
        asyncio.run(graph.ainvoke({'question':'Propón un plan de evaluación de calidad para research_runs'},
            {'configurable':{'thread_id':'g4-unsupported-impact'}}))
    assert [call.args[0] for call in model.call_args_list]==['G1','G3','G4']
    preview.assert_not_awaited()


@pytest.mark.parametrize('refs',[['unknown'],['asset'],['version']])
def test_diagnosis_rejects_unreviewed_or_wrong_reference_domains(refs):
    state,review,diagnosis,_=context();diagnosis['findings'][0]['evidence_ids']=refs
    with pytest.raises(ValueError,match='unsupported claim'):
        validate_case_diagnosis(diagnosis,review,state['evidence'])


@pytest.mark.parametrize('assets,versions',[(['another'],['version']),(['asset'],['other-version']),
    (['asset'],[]),(['asset','asset'],['version','version']),(['case:'+('a'*64)],['version'])])
def test_draft_cannot_add_rebase_duplicate_or_replace_assessed_business_assets_with_a_case(assets,versions):
    state,_,_,draft=context();draft.update(asset_ids=assets,expected_versions=versions)
    with pytest.raises(ValueError):validate_case_treatment(draft,state['assets'])


def test_g4_and_g5_aliases_round_trip_exact_sources_assets_and_versions_without_guessing():
    state,review,diagnosis,draft=context()
    payload,aliases=consultation_model_sources('G4',dict(evidence=state['evidence'],review=review))
    assert payload['review']['supported_claims'][0]['evidence_ids']==['E1']
    model_diagnosis=deepcopy(diagnosis);model_diagnosis['findings'][0]['evidence_ids']=['E1']
    restored=json.loads(restore_consultation_sources('G4',json.dumps(model_diagnosis),aliases))
    assert restored==diagnosis
    payload,aliases=consultation_model_sources('G5',dict(evidence=state['evidence'],diagnosis=diagnosis,assets=state['assets']))
    assert payload['assets'][0]['asset_id']=='A1' and payload['assets'][0]['version_id']=='V1'
    assert payload['diagnosis']['findings'][0]['evidence_ids']==['E1']
    raw={**draft,'asset_ids':['A1'],'expected_versions':['V1']}
    assert json.loads(restore_consultation_sources('G5',json.dumps(raw),aliases))==draft
    raw['asset_ids']=['E1']
    with pytest.raises(ValueError):restore_consultation_sources('G5',json.dumps(raw),aliases)
    raw.update(asset_ids=['A1'],expected_versions=['A1'])
    with pytest.raises(ValueError):restore_consultation_sources('G5',json.dumps(raw),aliases)


def test_treatment_for_an_explicit_case_cannot_be_silently_reclassified_as_a_new_case():
    ref='a'*64
    intent=admitted_case_intent(GovIntent(intent='treatment',asset_mentions=['orders']),
        'Prepara tratamiento para el caso '+ref+' sobre orders',[])
    assert intent.case_reference==ref and not intent.clarification
    invented=admitted_case_intent(GovIntent(intent='decision',case_reference='b'*64,decision_action='approve'),
        'Aprueba el tratamiento',[])
    assert invented.case_reference is None and invented.clarification


@pytest.mark.parametrize('reference',['a'*65,'a'*63+'g','A'*64,'a'*63,'abc123','17','@token'])
def test_malformed_case_references_cannot_become_an_opening_when_the_model_mentions_a_table(reference):
    intent=admitted_case_intent(GovIntent(intent='treatment',asset_mentions=['orders']),
        'Prepara tratamiento para el caso '+reference+' sobre orders',[])
    assert intent.clarification and intent.case_reference is None


def test_a_confirmation_cannot_be_rendered_without_its_exact_visible_scope_and_summary():
    from app.modules.ai_data_governance.case_previews import render_case_preview

    with pytest.raises(ValueError):
        render_case_preview(dict(available=True,action='OPEN',command='CONFIRMAR ABRIR '+'a'*32))


@pytest.mark.parametrize('question,mention',[
    ('Prepara un tratamiento para orders','orders'),
    ('Abre un caso para orders','orders'),
    ('Prepara la apertura de un caso para orders','orders'),
    ('Solicita la apertura de un caso de gobierno para orders','orders'),
    ('Prepare the opening of a case for orders','orders'),
    ('Crea un caso nuevo sobre orders','orders'),
    ('Prepare a new case for orders','orders'),
    ('Prepara tratamiento para catalog.schema.case','catalog.schema.case'),
    ('Prepara tratamiento para '+('long_table_'*5),'long_table_'*5),
])
def test_an_unambiguous_new_case_or_a_table_name_does_not_require_a_nonexistent_case_reference(question,mention):
    intent=admitted_case_intent(GovIntent(intent='treatment',asset_mentions=[mention]),question,[])
    assert intent.case_reference is None and intent.clarification is None


@pytest.mark.parametrize('question',[
    'Prepara tratamiento para el caso con referencia abc123 sobre orders',
    'Prepara tratamiento para el caso de identificador 17 sobre orders',
    'Abre un caso para orders y revisa también el caso abc123',
    'Prepara la apertura del caso sobre orders',
    'Prepara la reapertura de un caso sobre orders',
    'Prepara la apertura de un caso para orders y revisa el caso abc123',
    'Prepara la apertura de un caso existente sobre orders',
    'Prepara la apertura de un caso anterior sobre orders',
    'Prepara la apertura de un caso ya registrado sobre orders',
    'Prepare the opening of a case that already exists for orders',
])
def test_unresolved_case_context_is_not_inferred_as_creation_from_a_preposition_or_another_new_case(question):
    intent=admitted_case_intent(GovIntent(intent='treatment',asset_mentions=['orders']),question,[])
    assert intent.clarification and intent.case_reference is None


def test_opening_a_new_case_keeps_the_model_intent_and_reaches_treatment_preview():
    graph,_,model,read,preview,_=graph_fixture()
    result=asyncio.run(graph.ainvoke(
        {'question':'Prepara la apertura de un caso de gobierno para research_runs. Quiero ver la confirmación antes de registrar nada.'},
        {'configurable':{'thread_id':'nominal-case-opening'}}))
    assert result['intent']['case_reference'] is None and result['intent']['clarification'] is None
    assert [call.args[0] for call in model.await_args_list]==['G1','G3','G4','G5','G6']
    assert result['case_preview']['action']=='OPEN' and preview.await_count==1
    assert result['response']['result_state']=='answered' and result['response']['case_ref'] is None
    assert read.await_count and all('COMMIT' not in call.args[0] for call in read.await_args_list)


def test_visible_treatment_is_exact_bounded_text_and_cannot_introduce_links_or_html():
    from app.modules.ai_data_governance.case_previews import render_case_preview

    state,_,_,draft=context()
    draft.update(problem='<script>no ejecutar</script>',suggested_action='[abrir](https://invalid.example)',
        verification_plan='![imagen](https://invalid.example/img)',remaining_gaps=['Falta responsable'])
    preview=dict(available=True,action='PROPOSE',command='CONFIRMAR PROPONER '+'a'*32,
        assessed_assets=state['assets'],treatment=draft,summary='Revisión del responsable',
        expires_at='2026-11-01T01:00:00+00:00',case_ref='b'*64)
    text=render_case_preview(preview)
    assert 'Revisión del responsable' in text and 'revisión '+('c'*12) in text
    assert '<script>' not in text and '&lt;script&gt;' in text
    assert '](https:' not in text and 'CONFIRMAR PROPONER '+'a'*32 in text
    assert 'pendiente de identificar' in text and 'Falta responsable' in text
    preview['command']='CONFIRMAR APROBAR '+'a'*32
    with pytest.raises(ValueError):render_case_preview(preview)


def test_a_diagnosis_without_a_concrete_treatment_does_not_call_g5():
    graph,state,model,_,_,_=graph_fixture()
    _,review,diagnosis,_=context();diagnosis['treatment_needed']=False
    model.side_effect=lambda phase,*_:json.dumps({'G1':{'intent':'treatment','asset_mentions':['research_runs']},
        'G3':review,'G4':diagnosis,'G6':state['response']}[phase])
    result=asyncio.run(graph.ainvoke({'question':'¿Hay algo que tratar en research_runs?'},
        {'configurable':{'thread_id':'no-treatment'}}))
    assert result['treatment']=={} and not any(call.args[0]=='G5' for call in model.call_args_list)


def test_model_proposed_responsibility_requires_a_known_metadata_reference_and_is_never_an_assignment():
    state,_,_,draft=context();draft['responsible_ref']='invented-owner'
    with pytest.raises(ValueError,match='invent a responsible'):
        validate_case_treatment(draft,state['assets'],evidence=state['evidence'])
    state['evidence'][0]['facts']['owner_ref']='documented-owner';draft['responsible_ref']='documented-owner'
    assert validate_case_treatment(draft,state['assets'],evidence=state['evidence'])['responsible_ref']=='documented-owner'

def test_insufficient_treatment_preserves_review_gaps_without_offering_a_write():
    from langgraph.checkpoint.memory import InMemorySaver
    state,review,_,_=context()
    review.update(coverage='insufficient',next_action='not_evaluable',supported_claims=[],
        gaps=['No hay mediciones vigentes para evaluar la calidad.'])
    model=AsyncMock(side_effect=lambda phase,*_:json.dumps({'G1':{'intent':'treatment','asset_mentions':['research_runs']},'G3':review}[phase]))
    async def authorize(*args,**kwargs):return None
    graph=build_governance_graph(authorize=authorize,resolve_assets=AsyncMock(return_value=state['assets']),
        read=AsyncMock(side_effect=lambda operation,*_:state['evidence'] if operation=='read_asset_snapshot' else []),
        call_model=model,capabilities=READ_OPERATIONS,checkpointer=InMemorySaver(),prepare_case=AsyncMock(return_value={'available':False,'reason':'No hay un tratamiento sustentado para registrar todavía.'}))
    result=asyncio.run(graph.ainvoke({'question':'Prepara un nuevo caso de gobierno para research_runs y evalúa la calidad disponible.'},
        config={'configurable':{'thread_id':'insufficient-treatment-gap'}}))
    assert result['response']['result_state']=='not_evaluable'
    assert result['response']['limitations']==review['gaps']
    assert 'No hay mediciones vigentes' in render_governance_response(result)
    assert [call.args[0] for call in model.await_args_list]==['G1','G3']
    assert result['case_preview']['available'] is False


def test_insufficient_limits_deduplicate_and_report_overflow_without_invalid_response():
    from app.modules.ai_data_governance.governance_graph import consultation_limitations
    assert consultation_limitations(['Falta medición'],['Falta medición'])==['Falta medición']
    original=[f'Brecha {i}' for i in range(37)]
    result=consultation_limitations(original[:5],original[5:])
    assert len(result)==32 and result[:31]==original[:31]
    assert '6 limitaciones adicionales' in result[-1]


@pytest.mark.parametrize('gap,escaped', [
    ('Falta medición [999]','Falta medición \\[999\\]'),
    ('Falta medición [1](https://invalid.example)','Falta medición \\[1\\]\\(https://invalid.example\\)'),
    ('Falta medición <img src=x>','Falta medición &lt;img src=x&gt;'),
])
def test_uncited_insufficient_limits_render_as_plain_text(gap,escaped):
    state=answer_state()
    state['response'].update(result_state='not_evaluable',answer_markdown='No puedo concluir la calidad.',cited_evidence_ids=[],limitations=[gap])
    rendered=render_governance_response(state)
    assert escaped in rendered
    assert gap not in rendered


@pytest.mark.parametrize('intent',['treatment','quality'])
def test_measurement_plan_and_quality_verdict_have_different_evidence_requirements(intent):
    graph,state,model,_,preview,_=graph_fixture()
    _,review,diagnosis,draft=context()
    review.update(coverage='partial',gaps=['No se recibieron mediciones de calidad pertinentes.'])
    review['supported_claims']=[dict(text='El catálogo identifica la revisión del activo.',evidence_ids=['proof'],kind='observed')]
    diagnosis.update(findings=[dict(text='Se recomienda identificar y obtener las mediciones aplicables al activo confirmado.',
        evidence_ids=['proof'],kind='recommendation')],limitations=review['gaps'])
    draft.update(problem='La evidencia recibida no permite evaluar la calidad.',
        suggested_action='Identificar las reglas aplicables y obtener mediciones de la revisión confirmada.',
        verification_plan='Comprobar método, revisión, cobertura y vigencia de los resultados obtenidos.',
        remaining_gaps=review['gaps'])
    response={**state['response'],'answer_markdown':'Se puede preparar un plan de medición para el activo del catálogo [1]. La calidad aún no está determinada.',
        'result_state':'partial','limitations':review['gaps']}
    model.side_effect=lambda phase,*_:json.dumps({'G1':{'intent':intent,'asset_mentions':['research_runs']},
        'G3':review,'G4':diagnosis,'G5':draft,'G6':response}[phase])
    result=asyncio.run(graph.ainvoke({'question':('Prepara un tratamiento para medir calidad de research_runs' if intent=='treatment'
        else '¿Cuál es la calidad de research_runs?')},{'configurable':{'thread_id':'measurement-'+intent}}))
    if intent=='treatment':
        assert [call.args[0] for call in model.call_args_list]==['G1','G3','G4','G5','G6']
        assert result['treatment']['responsible_ref'] is None and result['case_preview']['action']=='OPEN'
        assert result['response']['case_ref'] is None and preview.await_count==1
    else:
        assert [call.args[0] for call in model.call_args_list]==['G1']
        assert result['response']['result_state']=='not_evaluable'
        assert result['treatment']=={} and preview.await_count==0
        assert 'mediciones' in result['response']['answer_markdown']
