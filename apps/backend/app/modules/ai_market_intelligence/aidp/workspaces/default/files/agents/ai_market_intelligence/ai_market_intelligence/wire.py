"""Content-carried NDJSON envelopes survive gateways which omit message metadata."""
import asyncio
import json

from .research_contracts import MarketEvent


Market_WIRE_LIMIT=2*1024*1024


def market_wire_line(kind,**values):
    if kind not in {'event','response','snapshot','heartbeat'}:
        raise ValueError('Unknown public wire envelope')
    packet={'protocol':'ai-market-intelligence','version':'2.0','kind':kind,**values}
    text=json.dumps(packet,ensure_ascii=False,separators=(',',':'),allow_nan=False)+'\n'
    if len(text.encode('utf-8'))>Market_WIRE_LIMIT:
        raise ValueError('Public wire packet exceeds its size bound')
    return text


def market_reference_manifest(evidence,identities):
    fields=('evidence_id','source','discovery_channel','url','requested_url','author_url','author_name','source_title','source_relation',
            'media','media_omitted_count','media_content_analyzed',
            'published_at','data_last_updated','observed_at','content_basis','locator','document_sha256')
    return [{key:evidence[identity][key] for key in fields if key in evidence[identity]} for identity in identities]


def market_event_packet(store,event):
    event=MarketEvent.model_validate(event).model_dump(mode='json')
    value,_=store.read()
    references=market_reference_manifest(value['evidence'],event['evidence_ids'])
    artifacts=[value['artifacts'][identity] for identity in event['artifact_ids']]
    return market_wire_line('event',event=event,references=references,artifacts=artifacts)


def market_visible_artifacts(value):
    if value is None:
        return []
    response=value['response']
    if response and response['status']!='pending' and not response.get('resumable'):
        return response.get('artifacts',[])
    identities={identity for block in value['blocks'].values()
                if block['type']!='draft.compacted' and block['status']!='failed' for identity in block['evidence_ids']}
    return [artifact for artifact in value['artifacts'].values() if set(artifact['evidence_ids']).issubset(identities)]


def market_replay_packet(store,request_id,after_sequence):
    value,replay=store.replay_snapshot(after_sequence)
    blocks=(replay.get('snapshot') or {}).get('blocks',{}).values()
    artifacts=market_visible_artifacts(value)
    if replay['snapshot'] is not None:
        replay['snapshot']['artifacts']={artifact['artifact_id']:artifact for artifact in artifacts}
    response=value['response'] if value else None
    identities={identity for event in [*replay['events'],*blocks] for identity in event['evidence_ids']}
    identities.update(identity for artifact in artifacts for identity in artifact['evidence_ids'])
    identities.update(row['evidence_id'] for row in (response or {}).get('references',[]))
    references=market_reference_manifest(value['evidence'],sorted(identities)) if value else []
    return market_wire_line('snapshot',request_id=request_id,replay=replay,response=response,
                        artifacts=artifacts,references=references)


class MarketWireDecoder:
    def __init__(self):
        self.buffer=''

    def feed(self,text):
        if not isinstance(text,str):
            raise ValueError('Wire decoding requires decoded text fragments')
        self.buffer+=text
        packets=[]
        while '\n' in self.buffer:
            line,self.buffer=self.buffer.split('\n',1)
            if not line:
                continue
            if len(line.encode('utf-8'))>Market_WIRE_LIMIT:
                raise ValueError('Public wire packet exceeds its size bound')
            packet=json.loads(line)
            if (not isinstance(packet,dict) or packet.get('protocol')!='ai-market-intelligence'
                    or packet.get('version')!='2.0'
                    or packet.get('kind') not in {'event','response','snapshot','heartbeat'}):
                raise ValueError('Unexpected native wire protocol')
            packets.append(packet)
        if len(self.buffer.encode('utf-8'))>Market_WIRE_LIMIT:
            raise ValueError('Incomplete wire packet exceeds its size bound')
        return packets

    def finish(self):
        if self.buffer.strip():
            raise ValueError('The native stream ended inside a public packet')


async def market_stream_graph(graph,store,request,config,*,heartbeat_seconds=10):
    stream=graph.astream(request,config=config,stream_mode=['custom','values'])
    pending=None
    response=None
    try:
        while True:
            if pending is None:
                pending=asyncio.create_task(stream.__anext__())
            done,_=await asyncio.wait({pending},timeout=heartbeat_seconds)
            if not done:
                yield market_wire_line('heartbeat',request_id=request['request_id'])
                continue
            try:
                mode,data=pending.result()
            except StopAsyncIteration:
                break
            pending=None
            if mode=='custom':
                yield market_event_packet(store,data)
            elif mode=='values' and data.get('response'):
                response=data['response']
        if response is None:
            raise RuntimeError('Research ended without a response or an explicit pending outcome')
        yield market_wire_line('response',request_id=request['request_id'],response=response)
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending,return_exceptions=True)
        await stream.aclose()
