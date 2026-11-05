"""Owner-bound request snapshots and replayable public blocks, without SQL or Records."""
from __future__ import annotations

import copy

from .runtime_core import Conflict, digest, encoded, utc_now
from .research_contracts import MarketEvent


class MarketCancelled(RuntimeError):
    pass


def market_attribution(evidence, identities):
    if any(identity not in evidence for identity in identities):
        raise ValueError('A public event cites evidence outside this authenticated request')
    selected=[evidence[identity] for identity in identities]
    sources=sorted({row['source'] for row in selected})
    discovery=sorted({row['discovery_channel'] for row in selected})
    return {'source_key':sources[0] if len(sources)==1 else None,'source_keys':sources,
            'discovery_channel':discovery[0] if len(discovery)==1 else None}


def market_commit_terminal(value,event_type,response):
    if response is None:
        return
    if value['response'] is not None and value['response']!=response:
        raise Conflict('The request already has a committed terminal response')
    cancelled=event_type=='run.cancelled'
    value.update(response=copy.deepcopy(response),status='cancelled' if cancelled else 'completed',
                 cancelled=value['cancelled'] or cancelled)


class MarketRunStore:
    def __init__(self, snapshot, revision, *, event_limit=256):
        if not isinstance(revision, str) or not revision or type(event_limit) is not int or not 1 <= event_limit <= 1000:
            raise ValueError('A fixed candidate revision and bounded event retention are required')
        self.snapshot, self.scope, self.revision = snapshot, snapshot.scope, revision
        self.event_limit = event_limit

    def read(self):
        value, etag = self.snapshot.read()
        if value is not None and (value.get('version') != 2 or value.get('scope') != self.scope.__dict__
                                  or value.get('candidate_revision') != self.revision
                                  or value.get('session_binding')!=getattr(self.snapshot,'session_binding',None)):
            raise ValueError('The stored request belongs to another owner or candidate')
        return value, etag

    def transact(self, change):
        for _ in range(5):
            value, etag = self.read()
            if value is None:
                raise ValueError('Research request is not initialized')
            result = change(value)
            value['state_revision'] += 1
            try:
                self.snapshot.write(value, etag)
                return result
            except Conflict:
                continue
        raise Conflict('Concurrent request update; no external effect was started by this mutation')

    def begin(self, request_id, question, *, started_at=None):
        if not isinstance(request_id,str) or not 1 <= len(request_id) <= 128:
            raise ValueError('A stable authenticated request ID is required')
        if not isinstance(question,str) or not 1 <= len(question.strip()) <= 500:
            raise ValueError('A question of 1–500 characters is required')
        current, etag = self.read()
        binding = digest([request_id,question])
        if current is not None:
            if current['request_binding'] != binding:
                raise ValueError('A request ID cannot be reused for a different question')
            return current
        value = {'version':2,'scope':self.scope.__dict__,'candidate_revision':self.revision,
                 'state_revision':0,'request_id':request_id,'request_binding':binding,
                 'question':question,'run_id':digest([self.scope.__dict__,getattr(self.snapshot,'session_binding',None),request_id]),'started_at':started_at or utc_now(),
                 'session_binding':getattr(self.snapshot,'session_binding',None),
                 'status':'running','brief':None,'plan':None,'task_index':0,
                 'collections':{},'evidence':{},'blocks':{},'artifacts':{},'events':[],
                 'next_sequence':1,'response':None,'cancelled':False,
                 'refinements':{},'read_batches':{},'active_query':None,'publications':{},'domain_outcomes':{},'financial_facts':{},'report_selection':None}
        try:
            self.snapshot.write(value,etag)
        except Conflict:
            return self.begin(request_id,question,started_at=started_at)
        return copy.deepcopy(value)

    def save(self, **updates):
        allowed={'status','brief','plan','task_index','collections','evidence','response','cancelled','refinements','read_batches','active_query','domain_outcomes','financial_facts','report_selection'}
        if set(updates)-allowed:
            raise ValueError('Only reviewed request fields can be updated')
        def update(value):
            value.update(copy.deepcopy(updates))
        self.transact(update)

    def reserve_draft_repair(self,source,input_sha256,*,evidence_revision=None):
        key=source if evidence_revision is None else source+':'+evidence_revision
        def reserve(value):
            repairs=value.setdefault('draft_repairs',{})
            if key in repairs:
                return repairs[key]==input_sha256
            if source in repairs or sum(name.startswith(source+':') for name in repairs)>=2:
                return False
            repairs[key]=input_sha256
            return True
        return self.transact(reserve)

    def append(self, event_type, *, role='coordinator', text='', evidence_ids=(), artifact_ids=(),
               block_id=None, supersedes=(), status='provisional', coverage=None, limitations=(),final_response=None):
        if final_response is not None and event_type not in {'run.completed','run.cancelled'}:
            raise ValueError('Only a terminal event can commit a final response')
        def record(value):
            if value['cancelled'] and event_type!='run.cancelled':
                raise MarketCancelled('The request was cancelled before publication')
            publication_key=digest([event_type,role,text,list(evidence_ids),list(artifact_ids),block_id,
                                    list(supersedes),status,coverage,list(limitations)])
            if publication_key in value['publications']:
                if final_response is not None and value['response']!=final_response:
                    raise ValueError('An existing terminal publication cannot change its response')
                return copy.deepcopy(value['publications'][publication_key])
            market_commit_terminal(value,event_type,final_response)
            attribution=market_attribution(value['evidence'],evidence_ids)
            previous=value['blocks'].get(block_id) if block_id else None
            block_revision=previous['block_revision']+1 if previous else int(block_id is not None)
            event=MarketEvent.model_validate({'event_id':digest([value['run_id'],value['next_sequence']]),
                'sequence':value['next_sequence'],'emitted_at':utc_now(),'run_id':value['run_id'],
                'turn_id':value['request_id'],'research_id':value['run_id'],'revision':self.revision,
                'type':event_type,'block_id':block_id,'block_revision':block_revision,
                'supersedes':list(supersedes),**attribution,
                'role_key':role,'text':text,'evidence_ids':list(evidence_ids),'artifact_ids':list(artifact_ids),
                'status':status,'coverage':coverage or {},'limitations':list(limitations)}).model_dump(mode='json')
            value['events'].append(event)
            value['events']=value['events'][-self.event_limit:]
            value['next_sequence']+=1
            if block_id:
                value['blocks'][block_id]=event
            value['publications'][publication_key]=event
            if len(encoded(value)) > self.snapshot.maximum_bytes:
                raise ValueError('Request snapshot capacity exceeded before publication')
            return copy.deepcopy(event)
        return self.transact(record)

    def cancel(self):
        value,_=self.read()
        if value is None:
            raise ValueError('The request must be initialized before cancellation')
        if value['response'] is not None:
            return value['response'],None
        response={'status':'cancelled','markdown':'La investigación fue cancelada. Las capturas enviadas conservan sus recibos.',
                  'references':[],'artifacts':[]}
        try:
            event=self.append('run.cancelled',text=response['markdown'],status='cancelled',final_response=response)
        except Conflict:
            current,_=self.read()
            if current['response'] is None:
                raise
            return current['response'],None
        return response,event

    def artifact(self, artifact):
        def save_artifact(value):
            if any(identity not in value['evidence'] for identity in artifact['evidence_ids']):
                raise ValueError('Artifact evidence belongs to another request')
            value['artifacts'][artifact['artifact_id']]=copy.deepcopy(artifact)
        self.transact(save_artifact)

    def replay(self, after_sequence=0):
        return self.replay_snapshot(after_sequence)[1]

    def replay_snapshot(self,after_sequence=0):
        if type(after_sequence) is not int or after_sequence < 0:
            raise ValueError('A nonnegative replay sequence is required')
        value,_=self.read()
        if value is None:
            return None,{'events':[],'snapshot':None,'cursor':0}
        if after_sequence >= value['next_sequence']:
            raise ValueError('The cursor is ahead of this research request')
        events=value['events']
        expired=bool(events and after_sequence < events[0]['sequence']-1)
        return value,{'events':[] if expired else [copy.deepcopy(event) for event in events if event['sequence']>after_sequence],
                'snapshot':{'blocks':copy.deepcopy(value['blocks']),'artifacts':copy.deepcopy(value['artifacts']),
                            'response':copy.deepcopy(value['response']),'status':value['status']} if expired else None,
                'cursor':value['next_sequence']-1}
