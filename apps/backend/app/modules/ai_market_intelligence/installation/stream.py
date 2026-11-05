"""Read native SSE envelopes, exposing only the agent's versioned public packets."""
import hashlib
import json

from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.wire import MarketWireDecoder


class MarketGatewayStream:
    def __init__(self,*,require_response=True):
        self.buffer=bytearray()
        self.lines=[]
        self.frame_bytes=0
        self.total_bytes=0
        self.frames=0
        self.done=False
        self.has_response=False
        self.require_response=require_response
        self.seen={}

    def feed(self,chunk):
        if not isinstance(chunk,bytes):
            raise ValueError('Native SSE requires byte fragments')
        self.total_bytes+=len(chunk)
        if self.total_bytes>32*1024*1024:
            raise ValueError('Native SSE exceeds its total size limit')
        self.buffer.extend(chunk)
        packets=[]
        while b'\n' in self.buffer:
            line,_,remaining=self.buffer.partition(b'\n')
            self.buffer=bytearray(remaining)
            line=line.rstrip(b'\r')
            self.frame_bytes+=len(line)
            if self.frame_bytes>8*1024*1024:
                raise ValueError('Native SSE exceeds its frame size limit')
            if not line:
                packets.extend(self.event())
                self.lines=[]
                self.frame_bytes=0
            elif line.startswith(b'data:'):
                self.lines.append(bytes(line[5:]).removeprefix(b' '))
        if len(self.buffer)+self.frame_bytes>8*1024*1024:
            raise ValueError('Native SSE exceeds its frame size limit')
        return packets

    def event(self):
        if not self.lines:
            return []
        self.frames+=1
        if self.frames>2048:
            raise ValueError('Native SSE exceeds its event limit')
        raw=b'\n'.join(self.lines)
        if raw.strip()==b'[DONE]':
            self.done=True
            return []
        event=json.loads(raw)
        if not isinstance(event,dict):
            raise ValueError('Native SSE event must be an object')
        response=event.get('response') or {}
        if not isinstance(response,dict):
            raise ValueError('Native SSE response must be an object')
        if (event.get('error') or event.get('type')=='error'
                or str(response.get('status', '')).lower() in {'failed','error','incomplete','cancelled','canceled'}):
            raise ValueError('The native gateway reported an execution error')
        packets=self.public_output(response.get('output',[]))
        if event.get('type')=='response.completed' or response.get('status')=='completed':
            self.done=True
        return packets

    def public_output(self,output):
        if not isinstance(output,list) or len(output)>256:
            raise ValueError('Native output exceeds its message bound')
        packets=[]
        for message in output:
            for text in assistant_texts(message):
                packets.extend(self.public_text(text))
        return packets

    def public_text(self,text):
        if not isinstance(text,str):
            raise ValueError('Native public text must be a string')
        if not text.lstrip().startswith('{"protocol":"ai-market-intelligence"'):
            return []
        decoder=MarketWireDecoder()
        packets=decoder.feed(text)
        decoder.finish()
        fresh=[]
        for packet in packets:
            signature=hashlib.sha256(json.dumps(packet,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            identity=packet.get('event',{}).get('event_id') or signature
            if identity in self.seen:
                if self.seen[identity]!=signature:
                    raise ValueError('An immutable public event changed during native replay')
                continue
            self.seen[identity]=signature
            self.has_response=self.has_response or packet['kind']=='response'
            fresh.append(packet)
        return fresh

    def finish(self):
        if self.buffer or self.lines or not self.done or self.require_response and not self.has_response:
            raise ValueError('Native SSE ended without a complete public delivery')


def assistant_texts(message):
    if not isinstance(message,dict) or message.get('role')!='assistant':
        return []
    if str(message.get('status', '')).lower() in {'failed','error','incomplete','cancelled','canceled'}:
        raise ValueError('The native assistant message did not complete successfully')
    content=message.get('content',[])
    if not isinstance(content,list) or len(content)>256:
        raise ValueError('Native message exceeds its content bound')
    return [part.get('text') for part in content if isinstance(part,dict) and part.get('type')=='output_text']
