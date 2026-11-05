"""Native research requires the exact reconciled package and preserves public-only receipts."""
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType

import pytest

from tests.modules.ai_market_intelligence.test_market_intelligence_research_gateway import frame
from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.wire import market_wire_line


def native_invoker(monkeypatch,raw):
    plugin=ModuleType('aidp_agent_flow')
    native=ModuleType('aidp_agent_flow.native_invoke')
    native._endpoint=lambda *args:('https://test.example/invoke',{'key':'test'})
    monkeypatch.setitem(sys.modules,'aidp_agent_flow',plugin)
    monkeypatch.setitem(sys.modules,'aidp_agent_flow.native_invoke',native)
    path=Path(__file__).resolve().parents[5]/'scripts/modules/ai_market_intelligence/market_invoke.py'
    spec=importlib.util.spec_from_file_location('market_invoke_script',path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    class Response:
        status_code=200
        headers={'Content-Type':'text/event-stream'}
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def iter_content(self,**kwargs): yield raw
    class Lifecycle:
        def __init__(self):
            self.state={'test_receipt':{'package_sha256':'exact','deployments':['active']},'test_session_key':'session'}
            self.calls=[]
        def status(self): pass
        def _package(self): return {'package_sha256':'exact'},None
        def _deployments(self,surface):
            assert surface=='TEST'
            return ['active']
        def _snapshot(self,rows): return rows
        def _session_info(self,key,rows): assert key=='session'
        def _session(self): return self
        def request(self,*args,**kwargs):
            self.calls.append(kwargs)
            return Response()
    return module,Lifecycle()


def test_native_research_refuses_stale_package_before_post_or_intent(monkeypatch,tmp_path):
    script,lifecycle=native_invoker(monkeypatch,b'')
    lifecycle.state['test_receipt']['package_sha256']='old'
    output=tmp_path/'study'
    with pytest.raises(ValueError,match='exact TEST'):
        script.invoke_market(lifecycle,{'request_id':'one','question':'Research'},output)
    assert lifecycle.calls==[] and not output.exists()


def test_native_replay_receipt_records_the_persisted_response_status(monkeypatch,tmp_path):
    wire=market_wire_line('snapshot',request_id='one',replay={'events':[],'cursor':1},
                      response={'status':'partial','markdown':'Persisted finding.'},artifacts=[])
    script,lifecycle=native_invoker(monkeypatch,frame(wire)+b'data: [DONE]\n\n')
    result=script.invoke_market(lifecycle,{'action':'replay','request_id':'one'},tmp_path/'replay')
    assert result['complete'] is True and result['response_status']=='partial'
    assert len(lifecycle.calls)==1 and result['semantic_accepted'] is False


@pytest.mark.parametrize('complete',[True,False])
def test_native_receipt_retains_public_packets_and_distinguishes_truncated_delivery(monkeypatch,tmp_path,complete):
    wire=market_wire_line('response',request_id='one',response={'status':'partial','markdown':'Verified finding.'})
    raw=frame(wire,metadata='PRIVATE')+(b'data: [DONE]\n\n' if complete else b'')
    script,lifecycle=native_invoker(monkeypatch,raw)
    result=script.invoke_market(lifecycle,{'request_id':'one','question':'Research'},tmp_path/'study')
    assert result['complete'] is complete and result['semantic_accepted'] is False
    assert result['response_status']=='partial' and len(lifecycle.calls)==1
    assert 'PRIVATE' not in (tmp_path/'study/packets.jsonl').read_text(encoding='utf-8')
    arrivals=[json.loads(line) for line in (tmp_path/'study/arrivals.jsonl').read_text().splitlines()]
    assert len(arrivals)==result['packet_count']==1
    assert set(arrivals[0])=={'packet_index','elapsed_seconds'} and arrivals[0]['packet_index']==0
    assert 0<=arrivals[0]['elapsed_seconds']<=300
    assert json.loads((tmp_path/'study/result.json').read_text())['failure']==(None if complete else 'ValueError')
    with pytest.raises(FileExistsError):
        script.invoke_market(lifecycle,{'request_id':'one','question':'Research'},tmp_path/'study')
    assert len(lifecycle.calls)==1


def test_native_execution_error_preserves_safe_reason_without_gateway_payload_or_retry(monkeypatch,tmp_path):
    private='PRIVATE_PROVIDER_DETAIL'
    raw=('data: '+json.dumps({'type':'error','error':{'message':private}})+'\n\n').encode()
    script,lifecycle=native_invoker(monkeypatch,raw)
    result=script.invoke_market(lifecycle,{'request_id':'one','question':'Research'},tmp_path/'failure')
    assert result['complete'] is False and result['failure']=='ValueError'
    assert result['failure_detail']=='native_execution_error' and len(lifecycle.calls)==1
    assert private not in (tmp_path/'failure/result.json').read_text(encoding='utf-8')
    assert not (tmp_path/'failure/packets.jsonl').exists()


def test_unknown_provider_exception_retains_only_type_and_unclassified_reason(monkeypatch,tmp_path):
    script,lifecycle=native_invoker(monkeypatch,b'')
    def fail(*args,**kwargs):
        lifecycle.calls.append(kwargs)
        raise ValueError('PRIVATE_PROVIDER_DETAIL')
    lifecycle.request=fail
    result=script.invoke_market(lifecycle,{'request_id':'one','question':'Research'},tmp_path/'unknown')
    assert result['failure_detail']=='unclassified_error' and len(lifecycle.calls)==1
    assert 'PRIVATE_PROVIDER_DETAIL' not in (tmp_path/'unknown/result.json').read_text(encoding='utf-8')
