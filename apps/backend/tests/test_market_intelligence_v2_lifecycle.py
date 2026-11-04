"""The maintained v2 TEST path cannot select legacy tables or a production source root."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def lifecycle_script():
    path=Path(__file__).resolve().parents[3]/'scripts/market_v2.py'
    spec=importlib.util.spec_from_file_location('market_v2_lifecycle_script',path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_v2_prepare_emits_a_readable_package_without_business_schema(tmp_path):
    script=lifecycle_script()
    config=tmp_path/'runtime.json'
    config.write_text(json.dumps({'runtime_version':'v2','research_enabled':False}),encoding='utf-8')
    result=script.prepare_v2(tmp_path,config,'candidate')
    source=(tmp_path/'candidate/market_intelligence.py').read_text(encoding='utf-8')
    assert result['runtime_version']=='v2' and result['business_tables_required'] is False
    assert 'class Records' not in source and 'SCHEMA_DEFINITION' not in source
    assert 'class MarketIntelligenceAgent' in source


def test_v2_lifecycle_rejects_legacy_or_foreign_native_source_root(tmp_path):
    script=lifecycle_script()
    for root in ('/Workspace/.control/modules/ai_market_intelligence/agent','/Workspace/another-agent'):
        with pytest.raises(ValueError,match='legacy or another module'):
            script.verify_v2_target(tmp_path,{'native':{'remote_root':root}},candidate_required=False)


def test_v2_artifacts_cannot_escape_the_project(tmp_path):
    with pytest.raises(ValueError,match='owning project'):
        lifecycle_script().v2_project_path(tmp_path,'../outside')


def test_native_v2_mutations_refuse_source_shared_with_production():
    class Lifecycle:
        agent_key='existing-v2'
        def _deployments(self,surface):
            assert surface=='PROD'
            return [{'lifecycleState':'ACTIVE'}]
    with pytest.raises(ValueError,match='production deployment'):
        lifecycle_script().native_v2_action(Lifecycle(),'sync',apply=True)


@pytest.mark.parametrize('options',[
    {'apply':False}, {'label':'../escape'}, {'command':None},
])
def test_v2_command_dispatch_rejects_unadmitted_invocation_before_effects(tmp_path,options):
    args=SimpleNamespace(action='invoke',apply=True,command=Path('command.json'),label='one',timeout=300)
    for key,value in options.items():
        setattr(args,key,value)
    with pytest.raises(ValueError,match='invoke requires'):
        lifecycle_script().execute_v2_command(object(),args,tmp_path)
    assert list(tmp_path.iterdir())==[]
