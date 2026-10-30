import ast
import json
from uuid import UUID

import pytest

from app.sonda_runtime import chat_source, investigator_source, readable_source
from app.lab_packs import LabAsset, LabPackError, _shared_module_assets
from app.labs.ai_social_network.notebooks.agent.sonda_worker import worker_object, worker_request


def test_native_investigator_contains_readable_sources_without_runtime_loader():
    raw=investigator_source()
    tree=ast.parse(raw)
    assert b'class SondaInvestigator' in raw
    assert b'async def study_case' in raw
    assert b'async def study(' not in raw  # Ship one production case pipeline.
    for node in ast.walk(tree):
        assert not (isinstance(node,ast.ImportFrom) and (node.module or '').startswith('app.'))
        assert not (isinstance(node,ast.Call) and isinstance(node.func,ast.Name)
            and node.func.id in ('exec','eval','compile'))


def test_builder_rejects_source_escape_and_name_collisions():
    with pytest.raises(ValueError):
        readable_source(('../config.py',))
    with pytest.raises(ValueError,match='conflicting'):
        readable_source(('sonda_contract.py','sonda_contract.py'))


def test_native_chat_contains_one_entry_and_reuses_visualizations_with_generation_memory():
    raw=chat_source()
    assert b'class SondaChatAgent' in raw
    assert b'class SocialNetworkAgent' not in raw
    assert b'def build_visualization' in raw and b'class GenerationMemory' in raw
    assert b'from app.' not in raw


def test_native_request_has_only_fixed_work_identity_not_arbitrary_urls():
    valid={'version':1,'work_id':str(UUID(int=1)),'sha256':'a'*64}
    assert worker_request(json.dumps(valid))==valid
    binding={'registry_id':str(UUID(int=2)),'generation_id':str(UUID(int=3))}
    assert worker_object(binding,valid,'input').endswith('/work/'+str(UUID(int=1))+'/input.json')
    with pytest.raises(ValueError):
        worker_request(json.dumps({**valid,'url':'https://example.com'}))
    with pytest.raises(ValueError):
        worker_request(json.dumps({**valid,'work_id':'../../other'}))


def test_shared_source_cannot_escape_its_fixed_allowlist():
    for lab,name in [('ai_social_network','../config.py'),('ai_data_governance','sonda_contract.py')]:
        with pytest.raises(LabPackError):
            _shared_module_assets(lab,[{'file':name,'sha256':'a'*64}])


def test_asset_detects_change_after_pack_validation(tmp_path):
    import hashlib
    path=tmp_path/'source.py'
    path.write_bytes(b'reviewed')
    asset=LabAsset('source.py',path,hashlib.sha256(b'reviewed').hexdigest())
    assert asset.read_bytes()==b'reviewed'
    path.write_bytes(b'changed')
    with pytest.raises(LabPackError,match='changed'):
        asset.read_bytes()
