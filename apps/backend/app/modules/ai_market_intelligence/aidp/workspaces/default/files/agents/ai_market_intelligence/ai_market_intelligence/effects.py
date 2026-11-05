"""Conditional receipts for one current request, separate from the business tables."""
import copy
import asyncio
from datetime import datetime,timedelta,timezone
from uuid import uuid4

from .runtime_core import Conflict, ConnectorUnavailable, ObjectSnapshot, digest, public_url, utc_now

Market_MAX_READ_DESTINATIONS=10
Market_READ_BATCH_SIZE=2

class MarketEffectPending(RuntimeError):
    pass


class MarketReadBudgetExceeded(ConnectorUnavailable):
    pass


class MarketObjectSnapshot(ObjectSnapshot):
    def __init__(self, storage, namespace, bucket, scope, revision, request_id, kind, *, session_binding=None):
        if kind not in {'request','effects'}:
            raise ValueError('Unsupported current operational object')
        super().__init__(storage,namespace,bucket,scope)
        self.session_binding=session_binding
        identity=[revision,request_id] if session_binding is None else [revision,request_id,session_binding]
        self.key=scope.prefix+'research/'+digest(identity)+'/'+kind+'.json'


class MarketEffects:
    def __init__(self, snapshot, revision, request_id, *, prior_operations=None):
        self.snapshot=snapshot
        self.binding=digest([snapshot.scope.__dict__,revision,request_id])
        if getattr(snapshot,'session_binding',None) is not None:
            self.binding=digest([self.binding,snapshot.session_binding])
        self.prior_operations=copy.deepcopy(prior_operations or {})

    def read(self):
        value,etag=self.snapshot.read()
        if value is not None and (value.get('version')!=2 or value.get('binding')!=self.binding):
            raise ValueError('Effect receipts belong to another request, owner or candidate')
        return value,etag

    def operation(self,key):
        value,_=self.read()
        return copy.deepcopy((value or {}).get('operations',{}).get(key))

    def reserved_read_urls(self):
        value,_=self.read()
        return [url for operation in (value or {}).get('operations',{}).values() for url in operation.get('read_urls',[])]

    @staticmethod
    def check_read_budget(operations,urls):
        reserved=[url for operation in operations.values() for url in operation.get('read_urls',[])]
        if set(reserved)&set(urls) or len(reserved)+len(urls)>Market_MAX_READ_DESTINATIONS:
            raise MarketReadBudgetExceeded('Se alcanzó el límite de diez páginas o documentos de esta consulta; se conservan las lecturas anteriores.')

    def change(self, mutation):
        for _ in range(5):
            value,etag=self.read()
            if value is None:
                value={'version':2,'binding':self.binding,'operations':{}}
            if value.get('version')!=2 or value.get('binding')!=self.binding:
                raise ValueError('Effect receipts belong to another request, owner or candidate')
            result=mutation(value['operations'])
            try:
                self.snapshot.write(value,etag)
                return copy.deepcopy(result)
            except Conflict:
                continue
        raise Conflict('Concurrent effect admission; no external call was made by this mutation')

    def claim(self, key, kind, arguments, *, read_urls=()):
        urls=[public_url(url) for url in read_urls]
        if len(urls)!=len(set(urls)):
            raise ValueError('Read destinations must be unique')
        fingerprint=digest([kind,arguments])
        token=uuid4().hex
        def admit(operations):
            previous=operations.get(key)
            if previous is not None:
                if previous['fingerprint']!=fingerprint or previous.get('read_urls',[])!=urls:
                    raise ValueError('An operation key cannot be reused with changed arguments')
                return {'claimed':False,**previous}
            self.check_read_budget(operations,urls)
            if kind=='model' and any(row['kind']=='model' and row['state'] in {'submitted','uncertain'}
                                     for row in operations.values()):
                raise MarketEffectPending('A prior model request is unresolved; changed context does not authorize another call')
            operation={'kind':kind,'fingerprint':fingerprint,'state':'submitted',
                       'claim':token,'submitted_at':utc_now(),'receipt':None,'result':None}
            if urls:
                operation['read_urls']=urls
            reused=self.reusable_receipt(kind,fingerprint)
            if reused is not None:
                operation.update(state='running',receipt=reused['receipt'],
                    capture_submitted_at=reused.get('capture_submitted_at',reused['submitted_at']),
                    reused_from=reused['submitted_at'])
            operations[key]=operation
            return {'claimed':reused is None,**operation}
        return self.change(admit)

    def reusable_receipt(self,kind,fingerprint):
        if kind!='apify':
            return None
        for row in self.prior_operations.values():
            if (row['kind']!='apify' or row['fingerprint']!=fingerprint or not row['receipt']
                    or row['state'] not in {'running','completed'}):
                continue
            age=datetime.now(timezone.utc)-datetime.fromisoformat(row.get('capture_submitted_at',row['submitted_at']).replace('Z','+00:00'))
            if timedelta(0)<=age<=timedelta(hours=24):
                return row
        return None

    def update(self, key, token, *, state, receipt=None, result=None):
        if state not in {'running','completed','rejected','uncertain'}:
            raise ValueError('Unsupported effect receipt state')
        def save(operations):
            operation=operations[key]
            if operation['claim']!=token:
                raise ValueError('Only the admitted caller may attach its receipt')
            if operation['state'] in {'completed','rejected'}:
                if (operation['state'],operation['receipt'],operation['result'])!=(state,receipt,result):
                    raise ValueError('A terminal receipt is immutable')
                return operation
            operation.update(state=state,receipt=copy.deepcopy(receipt),result=copy.deepcopy(result))
            return operation
        return self.change(save)


class MarketRecordedModel:
    def __init__(self, model, effects):
        self.model,self.effects=model,effects

    async def ask(self, name, schema, payload):
        arguments={'name':name,'schema':schema,'payload':payload}
        key='model-'+digest(arguments)
        admitted=self.effects.claim(key,'model',arguments)
        if admitted['state']=='completed':
            return admitted['result']
        if not admitted['claimed']:
            raise MarketEffectPending('A prior model request has no reconciled response; it was not repeated')
        async def recorded():
            try:
                result=await self.model.ask(name,schema,payload)
            except BaseException:
                self.effects.update(key,admitted['claim'],state='uncertain')
                raise
            self.effects.update(key,admitted['claim'],state='completed',result=result)
            return result
        pending=asyncio.create_task(recorded())
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError:
            try:
                await pending
            except BaseException:
                # The effect records its own failure; preserve the caller's cancellation.
                pass
            raise
