"""Gold-owned autonomous Activity admission and one-shot dispatch reservation.

No HTTP/model calls occur here. Dispatch uncertainty remains burned: a durable
marker forbids another POST, independently of native claim ownership and expiry.
The runner reads the fixed work/input.json object and resolves its existing saved
business result before using these helpers; supplied input bytes are not browser
or model arguments. Helpers validate those protected bytes without model calls.
"""
from dataclasses import dataclass
from datetime import timedelta
import hashlib
import json
from uuid import UUID, uuid4, uuid5

from app.sonda_contract import VERSION, encoded, no_duplicate_keys
from app.labs.ai_social_network.notebooks.agent.sonda_study import case_projection
from app.labs.ai_social_network.notebooks.sonda_tools import tool_catalog_rows
from app.labs.ai_social_network.notebooks.sonda_io import worker_request
from app.sonda_activity_runtime import (
    ActivityJournal_admission_keys, ActivityJournal__put,
    ActivityProducer_AdmissionDescriptor, ActivityProducer_CurrentPointer,
    ActivityProducer_AdmissionClaim, ActivityProducer_PRODUCER_VERSION,
    ActivityProducer__producer_now, ActivityProducer__producer_uuid,
    ActivityProducer_parse_producer_document, ActivityProducer_producer_encoded,
    ActivityProducer_validate_admission, ActivityProducer_validate_claim_binding,
)


class AutonomousActivityPending(RuntimeError):
    """Prior dispatch/claim or elapsed start deadline requires result reconciliation."""


@dataclass(frozen=True)
class AutonomousActivityAdmission:
    descriptor: object
    root: str
    current_key: str

    @property
    def ref(self):
        return str(self.descriptor.admission_id)


def _admission_hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _admission_scope(binding):
    return {key:str(ActivityProducer__producer_uuid(binding[key])) for key in ('registry_id','generation_id')}


def _admission_work(binding, request, raw):
    request=worker_request(encoded(request).decode('utf-8'))
    if not isinstance(raw,bytes) or not 1<=len(raw)<=4194304 or _admission_hash(raw)!=request['sha256']:
        raise ValueError('Protected autonomous input hash differs')
    work_id=ActivityProducer__producer_uuid(request['work_id'])
    scope=_admission_scope(binding)
    if work_id!=uuid5(UUID(scope['generation_id']),request['sha256']):
        raise ValueError('Autonomous work identity differs from its protected input')
    value=json.loads(raw,object_pairs_hook=no_duplicate_keys)
    if (not isinstance(value,dict) or set(value)!={'configuration','work','state','execution_results'}
            or encoded(value)!=raw):
        raise ValueError('Protected autonomous input must be the canonical case projection')
    configuration,work,_=case_projection(value['configuration'],value['work'],value['state'],value['execution_results'])
    expected={**scope,'contract_version':VERSION,'contract_sha256':binding.get('contract_sha256')}
    if work['scope']!=expected:
        raise ValueError('Autonomous case contract/scope differs from its protected binding')
    return scope,work_id,configuration,work


def _admission_document(objects,key,kind,*,optional=False):
    blob=objects.get('artifacts',key)
    if blob is None:
        if optional:
            return None
        raise ValueError('Required autonomous admission object is missing')
    value=ActivityProducer_parse_producer_document(blob.data)
    if not isinstance(value,kind):
        raise ValueError('Autonomous admission object type differs')
    return value


def _admission_paths(handle):
    descriptor=ActivityProducer_AdmissionDescriptor.model_validate(handle.descriptor)
    root,key=ActivityJournal_admission_keys(descriptor.scope,str(descriptor.run.agent_id),
        descriptor.native_session_sha256,str(descriptor.admission_id))
    if (handle.root,handle.current_key)!=(root,key):
        raise ValueError('Autonomous admission handle path differs')
    return descriptor,root,key


def _admission_current(objects,handle,now):
    descriptor,root,key=_admission_paths(handle)
    current=_admission_document(objects,key,ActivityProducer_CurrentPointer)
    claim=_admission_document(objects,root+'claim.json',ActivityProducer_AdmissionClaim,optional=True)
    expected=(descriptor.scope,descriptor.run.agent_id,descriptor.admission_id,descriptor.run.run_id,
        descriptor.run.request_id,descriptor.run.attempt_id,descriptor.native_session_sha256,descriptor.expires_at)
    actual=(current.scope,current.agent_id,current.admission_id,current.run_id,current.request_id,
        current.attempt_id,current.native_session_sha256,current.expires_at)
    if actual!=expected:
        raise ValueError('Autonomous current pointer belongs to another admission')
    if claim is not None:
        ActivityProducer_validate_claim_binding(claim,descriptor,owner_nonce=claim.owner_nonce)
    if current.status!='admitted' or claim is not None:
        raise AutonomousActivityPending('Autonomous admission is consumed or unresolved')
    if not descriptor.created_at<=now<descriptor.expires_at:
        raise AutonomousActivityPending('Autonomous admission start deadline elapsed')
    ActivityProducer_validate_admission(descriptor,current,admission_id=str(descriptor.admission_id),
        scope=descriptor.scope.model_dump(mode='json'),agent_id=str(descriptor.run.agent_id),
        native_session_sha256=descriptor.native_session_sha256,input_sha256=descriptor.input_sha256,
        now=now,existing_claim=claim,work_id=str(descriptor.run.work_id),
        work_input_sha256=descriptor.work_input_sha256,investigation_id=descriptor.run.investigation_id)
    return current


def _admission_candidate(binding,request,raw,now):
    scope,work_id,configuration,work=_admission_work(binding,request,raw)
    identity=lambda role:uuid5(work_id,'sonda.activity.autonomous.v1.'+role)
    run=dict(run_id=identity('run'),request_id=identity('request'),attempt_id=identity('attempt'),
        work_id=work_id,investigation_id=work['investigation_id'],execution_kind='native_invocation',
        component='native_agent',agent_id=ActivityProducer__producer_uuid(binding['investigator_agent_key']),
        agent_name='ai_social_network_investigator',source='autonomous_study',created_at=now,synthetic=False)
    return ActivityProducer_AdmissionDescriptor(schema_version=ActivityProducer_PRODUCER_VERSION,
        admission_id=identity('admission'),scope=scope,run=run,
        native_session_sha256=_admission_hash(str(work_id).encode()),input_sha256=_admission_hash(encoded(request)),
        config_revision=work['config_revision'],tool_catalog_sha256=_admission_hash(encoded(tool_catalog_rows(configuration,validated=True))),
        created_at=now,expires_at=now+timedelta(minutes=30),work_input_sha256=request['sha256'])


def _admission_same_prepared(existing,candidate):
    value=candidate.model_dump(mode='json')
    saved=existing.model_dump(mode='json')
    value['created_at'],value['expires_at']=saved['created_at'],saved['expires_at']
    value['run']['created_at']=saved['run']['created_at']
    if value!=saved:
        raise ValueError('Immutable autonomous admission facts changed')


def prepare_autonomous_activity(objects,binding,request,protected_input_raw,now):
    """Return a private handle; caller passes handle.ref as metadata and reserves before POST."""
    now=ActivityProducer__producer_now(now)
    candidate=_admission_candidate(binding,request,protected_input_raw,now)
    root,key=ActivityJournal_admission_keys(candidate.scope,str(candidate.run.agent_id),
        candidate.native_session_sha256,str(candidate.admission_id))
    if objects.get('artifacts',root+'dispatch.json') is not None:
        raise AutonomousActivityPending('Autonomous dispatch is already reserved; resolve the original result')
    saved=_admission_document(objects,root+'descriptor.json',ActivityProducer_AdmissionDescriptor,optional=True)
    if saved is not None:
        _admission_same_prepared(saved,candidate)
        descriptor=saved
    else:
        descriptor=candidate
        ActivityJournal__put(objects,root+'descriptor.json',
            ActivityProducer_producer_encoded(descriptor.model_dump(mode='json')),None)
    if not descriptor.created_at<=now<descriptor.expires_at:
        raise AutonomousActivityPending('Prepared autonomous admission has expired; it cannot be rearmed')
    handle=AutonomousActivityAdmission(descriptor,root,key)
    current=_admission_document(objects,key,ActivityProducer_CurrentPointer,optional=True)
    if current is None:
        current=ActivityProducer_CurrentPointer(schema_version=ActivityProducer_PRODUCER_VERSION,
            scope=descriptor.scope,agent_id=descriptor.run.agent_id,native_session_sha256=descriptor.native_session_sha256,
            admission_id=descriptor.admission_id,run_id=descriptor.run.run_id,request_id=descriptor.run.request_id,
            attempt_id=descriptor.run.attempt_id,epoch=1,status='admitted',owner_nonce=None,expires_at=descriptor.expires_at)
        ActivityJournal__put(objects,key,ActivityProducer_producer_encoded(current.model_dump(mode='json')),None)
    _admission_current(objects,handle,now)
    return handle


def reserve_autonomous_dispatch(objects,handle,now):
    """Create one Gold dispatch marker; a prior marker always forbids another POST."""
    if not isinstance(handle,AutonomousActivityAdmission):
        raise TypeError('Autonomous dispatch requires its private admission handle')
    now=ActivityProducer__producer_now(now)
    _admission_paths(handle)
    saved=_admission_document(objects,handle.root+'descriptor.json',ActivityProducer_AdmissionDescriptor)
    if saved.model_dump(mode='json')!=handle.descriptor.model_dump(mode='json'):
        raise ValueError('Autonomous dispatch handle changed')
    _admission_current(objects,handle,now)
    key=handle.root+'dispatch.json'
    if objects.get('artifacts',key) is not None:
        raise AutonomousActivityPending('Autonomous dispatch is already reserved; resolve the original result')
    descriptor=handle.descriptor
    run=descriptor.run
    # This private Gold marker is not a native claim and grants no public authority.
    marker=dict(version=1,dispatch_nonce=str(uuid4()),admission_id=str(descriptor.admission_id),
        scope=descriptor.scope.model_dump(mode='json'),run_id=str(run.run_id),request_id=str(run.request_id),
        attempt_id=str(run.attempt_id),work_id=str(run.work_id),investigation_id=run.investigation_id,
        input_sha256=descriptor.input_sha256,work_input_sha256=descriptor.work_input_sha256,
        native_session_sha256=descriptor.native_session_sha256,config_revision=descriptor.config_revision,
        tool_catalog_sha256=descriptor.tool_catalog_sha256,reserved_at=now.isoformat())
    ActivityJournal__put(objects,key,encoded(marker),None)
    return marker['dispatch_nonce']
