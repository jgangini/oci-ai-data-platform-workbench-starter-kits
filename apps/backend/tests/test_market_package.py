"""Candidate packaging must not activate the legacy ingestion module."""
import hashlib
import json

import pytest

from app import lab_packs
from app.market_installation.agent_package import FILES


def test_market_candidate_has_documented_empty_framework_and_own_runtime():
    pack = lab_packs.load_lab_pack('ai_market_intelligence', require_available=False)
    assert pack.status == 'planned'
    with pytest.raises(lab_packs.LabPackError, match='not available yet'):
        lab_packs.load_lab_pack(pack.lab_id)
    assert pack.agent['name'] == 'ai_market_intelligence'
    assert not pack.datasets and not pack.notebooks
    expected = {'notebooks/agent/ai_market_intelligence/' + name for name in FILES}
    expected.update({'notebooks/agent/market_intelligence.py', 'notebooks/agent/requirements.txt'})
    assert {asset.name for asset in pack.runtime_files} == expected
    assert 'memory.py' in FILES
    schema = json.loads(lab_packs.module_runtime_source(pack.lab_id, 'notebooks/agent/ai_market_intelligence/schema.json', require_available=False))
    assert set(pack.tables['framework']) == {table['name'] for table in schema['tables']}
    assert len(schema['tables']) == 14
    columns = [column for table in schema['tables'] for column in table['columns']]
    assert len(columns) == 145
    assert all(column['description'].strip() for column in columns)
    assert all(table['layer'] == 'framework' for table in schema['tables'])
    assert pack.expected_results['installation'] == {
        'rows_per_table': 0, 'documented_columns': 145,
        'research_enabled': False, 'scheduled': False,
    }
    for asset in pack.runtime_files:
        assert hashlib.sha256(asset.read_bytes()).hexdigest() == asset.sha256


def test_retired_social_package_cannot_be_loaded_or_installed_as_market():
    with pytest.raises(lab_packs.LabPackError, match='Invalid lab metadata'):
        lab_packs.load_lab_pack('ai_social_network')
    assert 'ai_social_network' not in {pack.lab_id for pack in lab_packs.lab_catalog()}
    assert not (lab_packs.LABS_ROOT.parent / 'legacy_labs' / 'ai_social_network').exists()


@pytest.mark.parametrize('filename', ['../outside.py', '/outside.py', 'nested/file.py', 'C:/outside.py', 'nested\\file.py', '..'])
def test_market_uses_the_common_notebooks_asset_boundary(tmp_path, filename):
    with pytest.raises(lab_packs.LabPackError, match='asset path'):
        lab_packs._module_assets(tmp_path, [{'file': filename, 'sha256': '0' * 64}], 'notebooks')


def test_loaded_runtime_rejects_later_content_drift(tmp_path):
    (tmp_path / 'notebooks').mkdir()
    source = tmp_path / 'notebooks/runtime.py'
    source.write_bytes(b'initial')
    asset = lab_packs._module_assets(tmp_path, [{'file': 'notebooks/runtime.py', 'sha256': hashlib.sha256(b'initial').hexdigest()}], 'notebooks')[0]
    source.write_bytes(b'changed')
    with pytest.raises(lab_packs.LabPackError, match='SHA-256 changed'):
        asset.read_bytes()
