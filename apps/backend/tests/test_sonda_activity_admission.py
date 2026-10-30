"""Offline Gold dispatch simulations; no native inference, HTTP or live storage."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime,timedelta,timezone
import hashlib
import json
from pathlib import Path
import threading
from types import SimpleNamespace
from uuid import UUID,uuid5

import pytest

from app import sonda_activity_admission as admission
from app import sonda_activity_runtime as runtime
from app.sonda_contract import encoded, canonical_utc
from app.labs.ai_social_network.notebooks.sonda_tools import tool_catalog_rows

ROOT=Path(__file__).resolve().parents[3]
NOW=datetime(2026,10,30,12,tzinfo=timezone.utc)


@pytest.mark.parametrize('pin',[None,'f'*64])
def test_missing_or_foreign_core_pin_is_denied_before_admission_writes(pin):
    binding,request,raw=work_request()
    binding['contract_sha256']=pin
    objects=Objects()
    with pytest.raises(ValueError,match='case contract'):
        admission.prepare_autonomous_activity(objects,binding,request,raw,NOW)
    assert objects.puts==[]


class Objects:
    def __init__(self):
        self.rows={}
        self.puts=[]
        self.gets=[]
        self.lock=threading.Lock()
        self.lost=set()
        self.race=None
    def get(self,role,key):
        assert role=='artifacts'
        self.gets.append(key)
        return self.rows.get(key)
    def put(self,role,key,raw,expected):
        assert role=='artifacts'
        if self.race and key.endswith('/dispatch.json'):
            self.race.wait(timeout=2)
        with self.lock:
            previous=self.rows.get(key)
            if (previous.etag if previous else None)!=expected:
                raise RuntimeError('Conditional-write conflict')
            self.puts.append((key,raw,expected))
            self.rows[key]=SimpleNamespace(data=raw,etag=str(len(self.puts)))
        if any(key.endswith(suffix) for suffix in self.lost):
            raise TimeoutError('Lost response after durable write')
        return self.rows[key].etag


def work_request(mutator=None):
    config=json.loads((ROOT/'contracts/sonda/v1/fixtures/01-configuration.json').read_bytes())
    criterion=config['payload']['criteria'][0]
    scope={key:config[key] for key in ('contract_version','contract_sha256','registry_id','generation_id')}
    seconds=criterion['case_window_hours']*3600
    epoch=datetime(1970,1,1,tzinfo=timezone.utc)
    start=epoch+timedelta(seconds=int((NOW-epoch).total_seconds())//seconds*seconds)
    start=canonical_utc(start.isoformat())
    case=hashlib.sha256(encoded([criterion['criterion_id'],criterion['revision'],criterion['case_scope_key'],start])).hexdigest()
    work=dict(scope=scope,investigation_id=case,criterion=criterion,config_revision=config['payload']['revision'],
        window_start=start,phase='evaluate',input_revision='a'*64,requests_complete=True)
    value=dict(configuration=config,work=work,state={},execution_results=[])
    if mutator:
        mutator(value)
    raw=encoded(value)
    digest=hashlib.sha256(raw).hexdigest()
    request=dict(version=1,work_id=str(uuid5(UUID(scope['generation_id']),digest)),sha256=digest)
    binding={key:scope[key] for key in ('registry_id','generation_id','contract_sha256')}
    binding['investigator_agent_key']='00000000-0000-0000-0000-000000000777'
    return binding,request,raw


def prepare(objects=None,now=NOW,mutator=None):
    objects=objects or Objects()
    binding,request,raw=work_request(mutator)
    handle=admission.prepare_autonomous_activity(objects,binding,request,raw,now)
    return objects,binding,request,raw,handle


def test_prepare_uses_protected_case_facts_deterministic_ids_and_catalog_hash():
    objects,binding,request,raw,handle=prepare()
    d=handle.descriptor
    assert handle.ref==str(d.admission_id)
    assert d.run.work_id==UUID(request['work_id']) and d.work_input_sha256==request['sha256']
    assert d.run.investigation_id==json.loads(raw)['work']['investigation_id']
    assert d.run.source=='autonomous_study' and d.run.execution_kind=='native_invocation'
    assert str(d.run.agent_id)==binding['investigator_agent_key'] and d.run.synthetic is False
    assert d.input_sha256==hashlib.sha256(encoded(request)).hexdigest()
    assert d.native_session_sha256==hashlib.sha256(request['work_id'].encode()).hexdigest()
    assert d.tool_catalog_sha256==hashlib.sha256(encoded(tool_catalog_rows(json.loads(raw)['configuration']))).hexdigest()
    assert all(expected is None for _,_,expected in objects.puts)
    assert len(objects.puts)==2 and not any(key.endswith('claim.json') for key in objects.rows)
    assert str(d.run.attempt_id)==str(uuid5(UUID(request['work_id']),'sonda.activity.autonomous.v1.attempt'))


def test_prepared_recovery_reuses_exact_descriptor_without_ttl_refresh_or_new_writes():
    objects,binding,request,raw,first=prepare()
    before=list(objects.puts)
    second=admission.prepare_autonomous_activity(objects,binding,request,raw,NOW+timedelta(minutes=5))
    assert second.ref==first.ref and second.descriptor==first.descriptor and objects.puts==before
    with pytest.raises(admission.AutonomousActivityPending,match='expired'):
        admission.prepare_autonomous_activity(objects,binding,request,raw,NOW+timedelta(minutes=31))
    assert objects.puts==before


@pytest.mark.parametrize('failure',['hash','work_id','scope','version','canonical'])
def test_invalid_input_scope_request_and_identity_write_nothing(failure):
    objects=Objects();binding,request,raw=work_request()
    if failure=='hash':
        request['sha256']='f'*64
    elif failure=='work_id':
        request['work_id']='00000000-0000-0000-0000-000000000999'
    elif failure=='scope':
        binding['registry_id']='00000000-0000-0000-0000-000000000999'
    elif failure=='version':
        request['version']=True
    else:
        raw+=b' '
        request.update(sha256=hashlib.sha256(raw).hexdigest())
        request['work_id']=str(uuid5(UUID(binding['generation_id']),request['sha256']))
    with pytest.raises(ValueError):
        admission.prepare_autonomous_activity(objects,binding,request,raw,NOW)
    assert objects.puts==[]


def test_unapproved_case_is_not_admitted_and_unapproved_tools_change_catalog_without_execution():
    objects=Objects()
    def reject(value):
        value['work']['criterion']['approval']['status']='pending'
    binding,request,raw=work_request(reject)
    with pytest.raises(ValueError):
        admission.prepare_autonomous_activity(objects,binding,request,raw,NOW)
    assert objects.puts==[]
    _,_,_,_,original=prepare()
    def pending_query(value):
        value['configuration']['payload']['queries'][0]['approval']={'status':'pending','decided_by':None,'decided_at':None}
        value['configuration']['payload']['queries'][0]['status']='draft'
    _,_,_,_,changed=prepare(mutator=pending_query)
    assert changed.descriptor.tool_catalog_sha256!=original.descriptor.tool_catalog_sha256


@pytest.mark.parametrize('status',['running','uncertain','terminal'])
def test_native_or_uncertain_pointer_cannot_rearm_or_dispatch(status):
    objects,binding,request,raw,handle=prepare()
    current=runtime.ActivityProducer_parse_producer_document(objects.rows[handle.current_key].data)
    current=current.model_copy(update={'status':status,'owner_nonce':UUID(int=900)})
    objects.rows[handle.current_key]=SimpleNamespace(data=runtime.ActivityProducer_producer_encoded(current.model_dump(mode='json')),etag='changed')
    before=list(objects.puts)
    with pytest.raises(admission.AutonomousActivityPending):
        admission.prepare_autonomous_activity(objects,binding,request,raw,NOW)
    with pytest.raises(admission.AutonomousActivityPending):
        admission.reserve_autonomous_dispatch(objects,handle,NOW)
    assert objects.puts==before


def test_foreign_pointer_and_forged_handle_paths_fail_before_foreign_read():
    objects,_,_,_,handle=prepare()
    forged=replace(handle,root='foreign/')
    before=list(objects.gets)
    with pytest.raises(ValueError,match='path'):
        admission.reserve_autonomous_dispatch(objects,forged,NOW)
    assert objects.gets==before
    current=runtime.ActivityProducer_parse_producer_document(objects.rows[handle.current_key].data)
    current=current.model_copy(update={'admission_id':UUID(int=900)})
    objects.rows[handle.current_key]=SimpleNamespace(data=runtime.ActivityProducer_producer_encoded(current.model_dump(mode='json')),etag='changed')
    with pytest.raises(ValueError):
        admission.reserve_autonomous_dispatch(objects,handle,NOW)
    assert not any(key.endswith('/dispatch.json') for key in objects.rows)


def test_dispatch_is_distinct_from_native_claim_and_second_post_is_always_denied():
    objects,_,_,_,handle=prepare()
    posts=[]
    nonce=admission.reserve_autonomous_dispatch(objects,handle,NOW)
    posts.append(nonce)  # Caller could now perform its one existing POST.
    marker=json.loads(objects.rows[handle.root+'dispatch.json'].data)
    assert marker['dispatch_nonce']==nonce and marker['attempt_id']==str(handle.descriptor.run.attempt_id)
    assert marker['scope']==handle.descriptor.scope.model_dump(mode='json')
    assert not any(key.endswith('/claim.json') for key in objects.rows)
    for now in (NOW,NOW+timedelta(hours=1)):
        with pytest.raises(admission.AutonomousActivityPending):
            admission.reserve_autonomous_dispatch(objects,handle,now)
    assert len(posts)==1


def test_lost_own_ack_is_reconciled_by_exact_nonce_bytes_without_second_put():
    objects,_,_,_,handle=prepare()
    objects.lost.add('/dispatch.json')
    nonce=admission.reserve_autonomous_dispatch(objects,handle,NOW)
    dispatch=[row for row in objects.puts if row[0].endswith('/dispatch.json')]
    assert len(dispatch)==1 and json.loads(dispatch[0][1])['dispatch_nonce']==nonce
    with pytest.raises(admission.AutonomousActivityPending):
        admission.reserve_autonomous_dispatch(objects,handle,NOW)
    assert len([row for row in objects.puts if row[0].endswith('/dispatch.json')])==1


def test_two_dispatch_reservations_race_only_one_may_post():
    objects,_,_,_,handle=prepare()
    objects.race=threading.Barrier(2)
    def reserve():
        try:
            return admission.reserve_autonomous_dispatch(objects,handle,NOW)
        except Exception:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:reserve(),range(2)))
    assert sum(result is not None for result in results)==1
    assert len([row for row in objects.puts if row[0].endswith('/dispatch.json')])==1


def test_native_claim_after_preparation_blocks_dispatch_without_burning_or_replacing_claim():
    objects,_,_,_,handle=prepare()
    runtime.ActivityJournal_admit_native(objects,handle.descriptor.scope,str(handle.descriptor.run.agent_id),
        handle.descriptor.native_session_sha256,handle.descriptor.input_sha256,handle.ref,NOW,
        dict(work_id=str(handle.descriptor.run.work_id),work_input_sha256=handle.descriptor.work_input_sha256,
            investigation_id=handle.descriptor.run.investigation_id))
    before=dict(objects.rows)
    with pytest.raises(admission.AutonomousActivityPending):
        admission.reserve_autonomous_dispatch(objects,handle,NOW)
    assert objects.rows==before and handle.root+'claim.json' in objects.rows


def test_lost_preparation_acks_reuse_exact_own_bytes_without_new_identities():
    objects=Objects()
    objects.lost.update(('/descriptor.json','.json'))
    objects,binding,request,raw,handle=prepare(objects=objects)
    assert len(objects.puts)==2
    before=list(objects.puts)
    again=admission.prepare_autonomous_activity(objects,binding,request,raw,NOW+timedelta(minutes=1))
    assert again.ref==handle.ref and again.descriptor==handle.descriptor and objects.puts==before


def test_prior_dispatch_blocks_preparation_even_if_pointer_was_deleted():
    objects,binding,request,raw,handle=prepare()
    admission.reserve_autonomous_dispatch(objects,handle,NOW)
    del objects.rows[handle.current_key]
    before=list(objects.puts)
    with pytest.raises(admission.AutonomousActivityPending):
        admission.prepare_autonomous_activity(objects,binding,request,raw,NOW+timedelta(minutes=1))
    assert objects.puts==before and handle.current_key not in objects.rows


def test_corrupt_prepared_descriptor_is_not_replaced_or_adopted():
    objects,binding,request,raw,handle=prepare()
    changed=handle.descriptor.model_copy(update={'tool_catalog_sha256':'f'*64})
    objects.rows[handle.root+'descriptor.json']=SimpleNamespace(
        data=runtime.ActivityProducer_producer_encoded(changed.model_dump(mode='json')),etag='foreign')
    before=list(objects.puts)
    with pytest.raises(ValueError,match='facts changed'):
        admission.prepare_autonomous_activity(objects,binding,request,raw,NOW)
    with pytest.raises(ValueError,match='handle changed'):
        admission.reserve_autonomous_dispatch(objects,handle,NOW)
    assert objects.puts==before
