"""One owner/session pointer and compact conversational scope; no evidence copied to history."""
import copy

from .runtime_core import Conflict, ObjectSnapshot, digest


class V2SessionPending(RuntimeError):
    pass


class V2Session:
    def __init__(self,storage,namespace,bucket,scope,session_id):
        self.snapshot=ObjectSnapshot(storage,namespace,bucket,scope)
        self.binding=digest([scope.__dict__,session_id])
        self.snapshot.key=scope.prefix+'v2/sessions/'+digest(session_id)+'.json'

    def change(self,mutation):
        for _ in range(5):
            value,etag=self.snapshot.read()
            if value is None:
                value={'version':2,'binding':self.binding,'active':None,'requests':{},'context':None}
            if value.get('version')!=2 or value.get('binding')!=self.binding:
                raise ValueError('Conversation belongs to another authenticated owner/session')
            result=mutation(value)
            try:
                self.snapshot.write(value,etag)
                return copy.deepcopy(result)
            except Conflict:
                continue
        raise Conflict('Concurrent conversation change; no research was started by this mutation')

    def reserve(self,request_id,question,revision,*,selections=None):
        def bind(value):
            existing=value['requests'].get(request_id)
            active=value['active']
            if existing and (existing['question']!=question or existing['revision']!=revision or existing.get('selections')!=selections):
                raise ValueError('A request ID cannot change its question or candidate')
            if existing and existing.get('finished'):
                return {**existing,'context':value['context']}
            if active:
                if active['question']!=question or active['revision']!=revision or active.get('selections')!=selections:
                    raise V2SessionPending('Retoma o cancela el encargo pendiente antes de iniciar otro o cambiar su revisión.')
                return {**active,'context':value['context']}
            if existing:
                return {**existing,'context':value['context']}
            request={'request_id':request_id,'question':question,'revision':revision,'selections':copy.deepcopy(selections),
                     'session_binding':self.binding}
            if len(value['requests'])>=1000:
                raise ValueError('Conversation request capacity reached; start a new native session')
            value['requests'][request_id]=request
            value['active']=request
            return {**request,'context':value['context']}
        return self.change(bind)

    def lookup(self,request_id):
        def find(value):
            request=value['requests'].get(request_id)
            if request is None:
                raise ValueError('The request does not belong to this authenticated conversation')
            return request
        return self.change(find)

    def previous_request(self,question,exclude):
        def find(value):
            return next((request for request in reversed(list(value['requests'].values()))
                if request['request_id']!=exclude and request['question']==question and request.get('finished')),None)
        return self.change(find)

    def finished_requests(self,exclude):
        def find(value):
            return [request for request in reversed(list(value['requests'].values()))
                    if request['request_id']!=exclude and request.get('finished')]
        return self.change(find)

    def remember_report(self,request_id):
        def remember(value):
            request=value['requests'][request_id]
            if not request.get('finished'):
                raise ValueError('Only a finished owned report can become the canonical report')
            context=value['context'] or {}
            if context.get('report') is None:
                value['context']={**context,'report':copy.deepcopy(request)}
        self.change(remember)

    def finish(self,request_id,brief,*,report_available=False,reused_report_id=None):
        def finish(value):
            if value['active'] and value['active']['request_id']==request_id:
                question=value['active']['question']
                prior=(value['context'] or {}).get('report')
                if reused_report_id is not None:
                    prior=value['requests'].get(reused_report_id)
                    if prior is None or not prior.get('finished'):
                        raise ValueError('The reused report must be a finished request in this conversation')
                context={'question':question,'brief':copy.deepcopy(brief),'report':prior}
                if report_available:
                    context['report']=copy.deepcopy(value['active'])
                value['context']=context if brief else value['context']
                value['active']=None
                value['requests'][request_id]['finished']=True
        self.change(finish)
