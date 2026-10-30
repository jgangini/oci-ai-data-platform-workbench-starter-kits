"""Protected opt-in and SDK metadata correlation; no native SDK/network calls."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
from types import ModuleType, SimpleNamespace
import sys
from uuid import UUID

import pytest

from app import sonda_activity_entry as entry


def uid(number):
    return str(UUID(int=number))


@pytest.mark.parametrize('policy',[None,{'enabled':False},
    {'enabled':False,'manifest_sha256':entry.ACTIVITY_MANIFEST_SHA256}])
def test_disabled_policy_preserves_legacy_without_sdk_or_admission(monkeypatch,policy):
    monkeypatch.setattr(entry,'generation_control',lambda *args:{'activity':policy})
    def forbidden(*args,**kwargs):
        raise AssertionError('Disabled activity must not touch SDK or admission')
    monkeypatch.setattr(entry,'generation_inference',forbidden)
    monkeypatch.setattr(entry,'activity_invocation',forbidden)
    async def run():
        async with entry.native_activity(None,{},None,None) as activity:
            assert activity is None
    asyncio.run(run())


@pytest.mark.parametrize('policy',[True,0,{'enabled':0},{'enabled':True},
    {'enabled':True,'manifest_sha256':'a'*64},
    {'enabled':True,'manifest_sha256':entry.ACTIVITY_MANIFEST_SHA256,'callback_url':'https://example.invalid'}])
def test_invalid_policy_cannot_silently_fall_back(monkeypatch,policy):
    monkeypatch.setattr(entry,'generation_control',lambda *args:{'activity':policy})
    with pytest.raises(ValueError):
        entry.activity_enabled(None,{})


@pytest.mark.parametrize('variables',[None,{}, {'sondaActivityRef':'bad'},
    {'sondaActivityRef':uid(1).replace('-','')}, {'sondaActivityRef':False},
    {'sondaActivityRef':uid(1),'sessionvariables.sondaActivityRef':uid(2)}])
def test_missing_invalid_or_conflicting_reference_rejected(variables):
    with pytest.raises(ValueError):
        entry.activity_reference(variables)


@pytest.mark.parametrize('variables',[{'sondaActivityRef':uid(1)},
    {'sessionvariables.sondaActivityRef':{'value':uid(1)}},
    {'sondaActivityRef':uid(1),'sessionvariables.sondaActivityRef':{'value':uid(1)}}])
def test_native_sdk_aliases_must_resolve_one_exact_reference(variables):
    assert entry.activity_reference(variables)==uid(1)


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(entry,'generation_control',lambda *args:{'activity':{
        'enabled':True,'manifest_sha256':entry.ACTIVITY_MANIFEST_SHA256}})
    monkeypatch.setattr(entry,'generation_inference',lambda *args:True)
    for name in ('aidputils','aidputils.agents','aidputils.agents.toolkit'):
        monkeypatch.setitem(sys.modules,name,ModuleType(name))
    sys.modules['aidputils.agents.toolkit'].chat_context=SimpleNamespace(
        session_context_var=SimpleNamespace(get=lambda:{'sondaActivityRef':uid(9)}))
    calls=[]
    @asynccontextmanager
    async def admitted(*args,**kwargs):
        calls.append((args,kwargs))
        yield 'admitted'
    monkeypatch.setattr(entry,'activity_invocation',admitted)
    def objects(storage,namespace,buckets):
        assert namespace=='known' and buckets=={'artifacts':'fixed-private-bucket'}
        return 'objects'
    monkeypatch.setattr(entry,'OciObjects',objects)
    binding=dict(registry_id=uid(1),generation_id=uid(2),activity_agent_id=uid(3),
        namespace='known',artifacts_bucket='fixed-private-bucket')
    return binding,calls


def test_actual_session_hash_is_taken_before_graph_thread_rewrite(enabled):
    binding,calls=enabled
    protected={'work_id':uid(4),'work_input_sha256':'b'*64,'investigation_id':'c'*64}
    async def run():
        async with entry.native_activity(None,binding,{'configurable':{'thread_id':uid(5)}},
                'a'*64,protected_work=protected) as activity:
            assert activity=='admitted'
    asyncio.run(run())
    args,kwargs=calls[0]
    assert args[:6]==('objects',{'registry_id':uid(1),'generation_id':uid(2)},uid(3),
        hashlib.sha256(uid(5).encode()).hexdigest(),'a'*64,uid(9))
    assert kwargs['protected_work']==protected and len(calls)==1


@pytest.mark.parametrize('agent,native',[(None,uid(5)),(uid(0),uid(5)),('bad',uid(5)),
    (uid(3),None),(uid(3),''),(uid(3),'x'*513)])
def test_invalid_installed_identity_denied_before_admission(enabled,agent,native):
    binding,calls=enabled
    binding['activity_agent_id']=agent
    async def run():
        async with entry.native_activity(None,binding,{'configurable':{'thread_id':native}},'a'*64):
            raise AssertionError('Must not enter business work')
    with pytest.raises(ValueError):
        asyncio.run(run())
    assert calls==[]


def test_pause_denies_admission_without_legacy_fallback(enabled,monkeypatch):
    binding,calls=enabled
    monkeypatch.setattr(entry,'generation_inference',lambda *args:False)
    async def run():
        async with entry.native_activity(None,binding,{},'a'*64):
            raise AssertionError('Must not enter business work')
    with pytest.raises(RuntimeError):
        asyncio.run(run())
    assert calls==[]
