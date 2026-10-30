import io
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.labs.ai_social_network.notebooks.sonda_io import (
    generation_enabled, generation_bootstrap, generation_inference, operational_prefix, verified_control)
from app.labs.ai_social_network.notebooks import sonda_io

BINDING={'registry_id':'00000000-0000-0000-0000-000000000001',
    'generation_id':'00000000-0000-0000-0000-000000000002',
    'contract_sha256':'a'*64,'namespace':'test','artifacts_bucket':'artifacts'}


class Storage:
    def __init__(self,value):
        self.value=value

    def get_object(self,*args):
        raw=io.BytesIO(json.dumps(self.value).encode())
        return SimpleNamespace(data=SimpleNamespace(raw=raw,close=raw.close))


@pytest.mark.parametrize('fields',[{}, {'enabled':True},
    {'enabled':True,'reset_verified':True},
    {'enabled':True,'reset_verified':True,'integrated_acceptance':True,'generation_id':'wrong'}])
def test_partial_or_wrong_generation_never_enables(fields):
    assert not generation_enabled(Storage({**BINDING,**fields}),BINDING)


def test_only_verified_accepted_generation_is_enabled():
    assert generation_enabled(Storage({**BINDING,'enabled':True,'reset_verified':True,
        'integrated_acceptance':True}),BINDING)


def test_control_reads_cannot_escape_generation_or_exceed_bound():
    with pytest.raises(ValueError):
        verified_control(Storage({}),BINDING,'other/key')
    with pytest.raises(ValueError):
        verified_control(Storage({'text':'long'}),BINDING,operational_prefix(BINDING)+'work.json',maximum=5)


def test_bootstrap_can_prepare_without_enabling_inference_or_claiming_acceptance():
    storage=Storage({**BINDING,'reset_verified':True,'enabled':False,'bootstrap':True})
    assert generation_bootstrap(storage,BINDING)
    assert not generation_enabled(storage,BINDING)
    assert not generation_inference(storage,BINDING)


@pytest.mark.parametrize('start_offset,end_offset,valid',[(-10,600,True),(-10,-1,False),
    (10,600,False),(-10,3601,False)])
def test_validation_window_expires_and_never_claims_production_acceptance(start_offset,end_offset,valid):
    now=datetime.now(timezone.utc)
    value={**BINDING,'reset_verified':True,'enabled':False,'validation':{
        'operation_id':BINDING['generation_id'],
        'started_at':(now+timedelta(seconds=start_offset)).isoformat(),
        'expires_at':(now+timedelta(seconds=end_offset)).isoformat()}}
    assert generation_inference(Storage(value),BINDING) is valid
    assert not generation_enabled(Storage(value),BINDING)


def test_native_spark_uses_injected_secret_api_and_operator_bound_region(monkeypatch):
    values={'tenancy':'tenancy','user':'user','fingerprint':'fingerprint','private_key':'test-only',
        'region':'other-region'}
    identity=hashlib.sha256(json.dumps([values[k] for k in ('tenancy','user','fingerprint')],separators=(',',':')).encode()).hexdigest()
    calls=[]
    monkeypatch.setattr(sonda_io,'aidputils',SimpleNamespace(secrets=SimpleNamespace(
        get=lambda **kwargs:values[kwargs['key']])),raising=False)
    monkeypatch.setitem(sys.modules,'aidputils',None)  # No importable distribution on Spark.
    def storage(config,**kwargs):
        calls.append(config)
        return 'storage'
    monkeypatch.setitem(sys.modules,'oci',SimpleNamespace(signer=SimpleNamespace(Signer=lambda *args,**kwargs:'signer'),
        object_storage=SimpleNamespace(ObjectStorageClient=storage)))
    assert sonda_io.runtime_storage({'credential_name':'existing','identity_sha256':identity,
        'region':'us-chicago-1'})==('storage','signer')
    assert calls[0]['region']=='us-chicago-1'
