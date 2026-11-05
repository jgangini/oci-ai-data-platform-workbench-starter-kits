"""The maintained current TEST path cannot select managed business tables or a production source root."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def lifecycle_script():
    path=Path(__file__).resolve().parents[5]/'scripts/modules/ai_market_intelligence/market.py'
    spec=importlib.util.spec_from_file_location('market_lifecycle_script',path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_market_prepare_emits_a_readable_package_without_business_schema(tmp_path):
    script=lifecycle_script()
    config=tmp_path/'runtime.json'
    config.write_text(json.dumps({'schema_version':1,'research_enabled':False}),encoding='utf-8')
    result=script.prepare_market(tmp_path,config,'candidate')
    source=(tmp_path/'candidate/market_intelligence.py').read_text(encoding='utf-8')
    assert result['schema_version']==1 and result['business_tables_required'] is False
    assert 'class Records' not in source and 'SCHEMA_DEFINITION' not in source
    assert 'class MarketIntelligenceAgent' in source


def test_market_lifecycle_rejects_legacy_or_foreign_native_source_root(tmp_path):
    script=lifecycle_script()
    for root in ('/Workspace/.control/modules/ai_market_intelligence/agent','/Workspace/another-agent'):
        with pytest.raises(ValueError,match='another module'):
            script.verify_market_target(tmp_path,{'native':{'remote_root':root}},candidate_required=False)


def test_market_artifacts_cannot_escape_the_project(tmp_path):
    with pytest.raises(ValueError,match='owning project'):
        lifecycle_script().market_project_path(tmp_path,'../outside')


def test_native_market_mutations_refuse_source_shared_with_production():
    class Lifecycle:
        agent_key='existing-current'
        def _deployments(self,surface):
            assert surface=='PROD'
            return [{'lifecycleState':'ACTIVE'}]
    with pytest.raises(ValueError,match='production deployment'):
        lifecycle_script().native_market_action(Lifecycle(),'sync',apply=True)


@pytest.mark.parametrize('options',[
    {'apply':False}, {'label':'../escape'}, {'command':None},
])
def test_market_command_dispatch_rejects_unadmitted_invocation_before_effects(tmp_path,options):
    args=SimpleNamespace(action='invoke',apply=True,command=Path('command.json'),label='one',timeout=300)
    for key,value in options.items():
        setattr(args,key,value)
    with pytest.raises(ValueError,match='invoke requires'):
        lifecycle_script().execute_market_command(object(),args,tmp_path)
    assert list(tmp_path.iterdir())==[]
