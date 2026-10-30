"""Offline wrapper tests; callback stubs do not certify chat/Gold business data."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from uuid import UUID

import pytest

ROOT = Path(__file__).resolve().parents[3]/'contracts/sonda-activity/candidate-v1'
spec = importlib.util.spec_from_file_location('activity_results_candidate_tests', ROOT/'results.py')
r = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = r
spec.loader.exec_module(r)


def uid(number):
    return str(UUID(int=number))


def fixture(kind='chat_response'):
    source = json.loads((ROOT/'fixtures/runs.json').read_bytes())
    run = source['items'][0]
    payload = {'answer':'Respuesta pública 🌎', 'sources':[], 'publication':{'publication_id':'safe'},
        'search_context':{}, 'query_context':[], 'visualization':None}
    if kind != 'chat_response':
        run.update(source='autonomous_study', work_id=uid(80), investigation_id='a'*64)
    if kind == 'native_step':
        payload = dict(work_id=uid(80), input_sha256='b'*64, investigation_id='a'*64,
            phase='report_prepared', report_refs=[{'report_id':'c'*64,'revision':1}],
            evaluation_count=3, request_count=2, proposal_count=0, publication_status='pending')
    if kind == 'gold_report':
        run.update(execution_kind='gold_publication',component='aidp_gold_runner',agent_id=None,agent_name=None)
        payload = {'reports':[{'summary':'Informe con citas', 'status':'completed_with_limits'}], 'publication':{'id':'safe'}}
    envelope = dict(schema_version=r.RESULT_VERSION, scope=source['scope'], result_id=uid(50),
        run_id=run['run_id'], attempt_id=run['attempt_id'], kind=kind, payload=payload)
    return envelope, run


def approved(payload, *, scope, run):
    assert isinstance(payload, dict) and scope and run
    return True


def arguments(envelope, run, raw=None):
    raw = raw if raw is not None else r.result_bytes(envelope)
    return dict(expected_scope=envelope['scope'], expected_run=run,
        expected_ref={'result_id':envelope['result_id'],'sha256':hashlib.sha256(raw).hexdigest(),'byte_length':len(raw)},
        chat_validator=approved, gold_report_validator=approved, expected_work_input_sha256='b'*64)


@pytest.mark.parametrize('kind',['chat_response','native_step','gold_report'])
def test_valid_bound_artifacts_preserve_entire_payload(kind):
    envelope, run = fixture(kind)
    raw = r.result_bytes(envelope)
    result = r.parse_result(raw, **arguments(envelope, run))
    assert result.model_dump(mode='json') == envelope
    assert r.result_bytes(result) == raw


@pytest.mark.parametrize('field',['scope','result_id','run_id','attempt_id'])
def test_binding_rejection_before_semantic_callback(field):
    envelope, run = fixture()
    args = arguments(envelope, run)
    envelope[field] = {'registry_id':uid(99),'generation_id':uid(98)} if field=='scope' else uid(99)
    raw = r.result_bytes(envelope)
    args['expected_ref'].update(sha256=r.p.producer_sha256(raw),byte_length=len(raw))
    calls=[]
    args['chat_validator']=lambda *a,**kw:calls.append(1)
    with pytest.raises(r.ResultError, match='identity_mismatch'):
        r.parse_result(raw, **args)
    assert not calls


@pytest.mark.parametrize('field,value',[('sha256','e'*64),('byte_length',1)])
def test_reference_hash_and_exact_length(field,value):
    envelope,run=fixture()
    args=arguments(envelope,run)
    args['expected_ref'][field]=value
    with pytest.raises(r.ResultError,match='reference_mismatch'):
        r.parse_result(r.result_bytes(envelope),**args)


@pytest.mark.parametrize('kind,validator',[('chat_response','chat_validator'),('gold_report','gold_report_validator')])
@pytest.mark.parametrize('callback',[None,False,'validator',lambda *a,**kw:None,lambda *a,**kw:1,lambda *a,**kw:{}])
def test_missing_or_permissive_validator_does_not_approve(kind,validator,callback):
    envelope,run=fixture(kind)
    args=arguments(envelope,run)
    args[validator]=callback
    with pytest.raises(r.ResultError):
        r.parse_result(r.result_bytes(envelope),**args)


def test_validator_exception_is_sanitized():
    envelope,run=fixture()
    args=arguments(envelope,run)
    def fail(*a,**kw):
        raise ValueError('PRIVATE SQL and credentials')
    args['chat_validator']=fail
    with pytest.raises(r.ResultError,match='^activity_result_invalid$'):
        r.parse_result(r.result_bytes(envelope),**args)


def test_validator_cannot_transform_payload():
    envelope,run=fixture()
    args=arguments(envelope,run)
    def mutate(payload,**kw):
        payload['answer']='Alterado'
        return True
    args['chat_validator']=mutate
    with pytest.raises(r.ResultError,match='semantic_rejection'):
        r.parse_result(r.result_bytes(envelope),**args)
    assert envelope['payload']['answer']=='Respuesta pública 🌎'


@pytest.mark.parametrize('change',['extra','version','kind','bad_uuid','noncanonical_uuid','scope_extra','scope_uuid'])
def test_closed_schema_and_canonical_identity(change):
    envelope,run=fixture()
    if change=='extra': envelope['authority']='gold_writer'
    elif change=='version': envelope['schema_version']='v2'
    elif change=='kind': envelope['kind']='worker_committed'
    elif change=='bad_uuid': envelope['result_id']='../result'
    elif change=='noncanonical_uuid': envelope['result_id']=uid(50).replace('-','')
    elif change=='scope_extra': envelope['scope']['bucket']='x'
    else: envelope['scope']['registry_id']=envelope['scope']['registry_id'].replace('-','')
    raw=r.p.producer_encoded(envelope)
    with pytest.raises(r.ResultError):
        r.parse_result(raw,**arguments(envelope,run,raw))


@pytest.mark.parametrize('raw_change',['duplicate_root','duplicate_payload','whitespace','nonfinite','trailing','empty'])
def test_raw_json_and_canonical_byte_guards(raw_change):
    envelope,run=fixture()
    raw=r.result_bytes(envelope)
    if raw_change=='duplicate_root': raw=raw.replace(b'"kind":"chat_response"',b'"kind":"chat_response","kind":"chat_response"')
    elif raw_change=='duplicate_payload': raw=raw.replace(b'"sources":[]',b'"sources":[],"sources":[]')
    elif raw_change=='whitespace': raw=b' '+raw
    elif raw_change=='nonfinite': raw=raw.replace(b'"visualization":null',b'"visualization":NaN')
    elif raw_change=='trailing': raw+=b'{}'
    else: raw=b''
    with pytest.raises(r.ResultError):
        r.parse_result(raw,**arguments(envelope,run,raw))


@pytest.mark.parametrize('field,value',[('publication_status','published'),('committed',True),('evaluation_count',True),
    ('request_count',-1),('proposal_count',9007199254740992),('phase','completed'),('path','/secret')])
def test_native_step_cannot_claim_publication_or_unbounded_counts(field,value):
    envelope,run=fixture('native_step')
    envelope['payload'][field]=value
    raw=r.p.producer_encoded(envelope)
    with pytest.raises(r.ResultError):
        r.parse_result(raw,**arguments(envelope,run,raw))


@pytest.mark.parametrize('field,value',[('work_id',uid(99)),('investigation_id','d'*64),('input_sha256','d'*64)])
def test_native_work_binding(field,value):
    envelope,run=fixture('native_step')
    envelope['payload'][field]=value
    raw=r.result_bytes(envelope)
    with pytest.raises(r.ResultError,match='work_binding'):
        r.parse_result(raw,**arguments(envelope,run))


def test_native_requires_protected_input_hash():
    envelope,run=fixture('native_step')
    args=arguments(envelope,run)
    args.pop('expected_work_input_sha256')
    with pytest.raises(r.ResultError):
        r.parse_result(r.result_bytes(envelope),**args)


@pytest.mark.parametrize('kind',['chat_response','native_step','gold_report'])
def test_kind_cannot_borrow_other_executor(kind):
    envelope,run=fixture(kind)
    if kind=='gold_report':
        run.update(execution_kind='native_invocation',component='native_agent',agent_id=uid(70),agent_name='worker')
    else:
        run.update(execution_kind='gold_publication',component='aidp_gold_runner',agent_id=None,agent_name=None)
    with pytest.raises(r.ResultError,match='kind_binding'):
        r.parse_result(r.result_bytes(envelope),**arguments(envelope,run))


def test_chat_cannot_borrow_autonomous_native_run():
    envelope,run=fixture()
    run.update(source='autonomous_study',work_id=uid(80),investigation_id='a'*64)
    with pytest.raises(r.ResultError,match='kind_binding'):
        r.parse_result(r.result_bytes(envelope),**arguments(envelope,run))


def test_gold_publication_preserves_huginn_origin_without_impersonating_an_agent():
    envelope, run = fixture('gold_report')
    run['source'] = 'huginn_chat'
    assert r.parse_result(r.result_bytes(envelope), **arguments(envelope, run)).kind == 'gold_report'


@pytest.mark.parametrize('phase,refs', [('evaluated',[{'report_id':'c'*64,'revision':1}]),
    ('report_prepared',[])])
def test_native_phase_cannot_disagree_with_report_references(phase, refs):
    envelope, run = fixture('native_step')
    envelope['payload'].update(phase=phase, report_refs=refs)
    raw = r.p.producer_encoded(envelope)
    with pytest.raises(r.ResultError):
        r.parse_result(raw, **arguments(envelope, run, raw))


def test_unchanged_work_has_no_new_outputs_or_publication_claim():
    envelope,run=fixture('native_step')
    envelope['payload'].update(phase='no_change',report_refs=[],evaluation_count=0,request_count=0,proposal_count=0)
    assert r.parse_result(r.result_bytes(envelope),**arguments(envelope,run)).payload.phase=='no_change'
    envelope['payload']['request_count']=1
    raw=r.p.producer_encoded(envelope)
    with pytest.raises(r.ResultError):
        r.parse_result(raw,**arguments(envelope,run,raw))


def test_size_limit_is_bytes_not_characters():
    envelope,run=fixture()
    envelope['payload']['answer']='🌎'*262144
    raw=r.p.producer_encoded(envelope)
    assert len(raw)>1048576
    with pytest.raises(r.ResultError,match='size'):
        r.parse_result(raw,**arguments(envelope,run,raw))
    with pytest.raises(r.ResultError,match='size'):
        r.result_bytes(envelope)


def test_report_reference_duplicates_and_bounds():
    envelope,run=fixture('native_step')
    envelope['payload']['report_refs']*=2
    raw=r.p.producer_encoded(envelope)
    with pytest.raises(r.ResultError):
        r.parse_result(raw,**arguments(envelope,run,raw))


def test_semantic_callback_gets_exact_authorized_context_once():
    envelope,run=fixture('gold_report')
    calls=[]
    def contextual(payload,*,scope,run):
        calls.append((copy.deepcopy(payload),scope,run))
        return True
    args=arguments(envelope,run)
    args['gold_report_validator']=contextual
    r.parse_result(r.result_bytes(envelope),**args)
    assert len(calls)==1
    assert calls[0][0]==envelope['payload']
    assert calls[0][1]==envelope['scope']
    assert calls[0][2]==r.p.Run.model_validate(run).model_dump(mode='json')


@pytest.mark.parametrize('field,value',[('revision',True),('revision',0),('report_id','z'*64)])
def test_report_reference_strict_identity(field,value):
    envelope,run=fixture('native_step')
    envelope['payload']['report_refs'][0][field]=value
    raw=r.p.producer_encoded(envelope)
    with pytest.raises(r.ResultError):
        r.parse_result(raw,**arguments(envelope,run,raw))


def test_nonbytes_input_rejected_without_callback():
    envelope,run=fixture()
    calls=[]
    args=arguments(envelope,run)
    args['chat_validator']=lambda *a,**kw:calls.append(1)
    with pytest.raises(r.ResultError,match='size'):
        r.parse_result(r.result_bytes(envelope).decode(),**args)
    assert not calls
