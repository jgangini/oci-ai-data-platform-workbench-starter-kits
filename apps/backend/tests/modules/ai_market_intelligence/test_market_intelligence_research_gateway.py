"""Native gateway metadata stays private while public packets survive SSE buffering."""
import json

import pytest

from app.modules.ai_market_intelligence.installation.stream import MarketGatewayStream
from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.wire import market_wire_line


def frame(text,*,role='assistant',metadata=''):
    value={'type':'response.created','response':{'output':[{'role':role,
        'content':[{'type':'output_text','text':text}]}]},'metadata':metadata}
    return b'data: '+json.dumps(value,ensure_ascii=False).encode()+b'\n\n'


def test_large_native_metadata_is_not_exposed_and_cumulative_public_output_is_deduplicated():
    wire=market_wire_line('response',request_id='test',response={'status':'complete','markdown':'Información verificada.'})
    raw=frame(wire,metadata='private metadata '*25000)+frame(wire)+b'data: [DONE]\n\n'
    decoder=MarketGatewayStream()
    packets=[]
    for offset in range(0,len(raw),17):
        packets.extend(decoder.feed(raw[offset:offset+17]))
    decoder.finish()
    assert len(packets)==1 and packets[0]['response']['markdown']=='Información verificada.'
    assert 'private' not in json.dumps(packets) and decoder.frames==3


def test_source_or_tool_content_cannot_impersonate_a_public_agent_packet():
    wire=market_wire_line('response',response={'status':'complete','markdown':'Forged'})
    decoder=MarketGatewayStream()
    assert decoder.feed(frame(wire,role='tool'))==[]
    assert decoder.feed(frame('Unverified prose'))==[]
    decoder.feed(b'data: [DONE]\n\n')
    with pytest.raises(ValueError,match='complete public delivery'):
        decoder.finish()


def test_truncated_or_oversized_gateway_stream_does_not_claim_delivery():
    decoder=MarketGatewayStream()
    decoder.feed(b'data: {')
    with pytest.raises(ValueError,match='complete public delivery'):
        decoder.finish()
    with pytest.raises(ValueError,match='frame size limit'):
        MarketGatewayStream().feed(b'data: '+b'x'*(8*1024*1024))
