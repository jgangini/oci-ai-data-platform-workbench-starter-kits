"""Candidate packaging must not activate the legacy ingestion module."""
import hashlib
import json

import pytest

from app import lab_packs
from app.labs.ai_market_intelligence.agent_package import FILES


def test_market_candidate_has_documented_empty_framework_and_own_runtime():
    pack = lab_packs.load_lab_pack('ai_market_intelligence', require_available=False)
    assert pack.status == 'planned'
    with pytest.raises(lab_packs.LabPackError, match='not available yet'):
        lab_packs.load_lab_pack(pack.lab_id)
    assert pack.agent['name'] == 'ai_market_intelligence'
    assert not pack.datasets and not pack.notebooks
    assert {asset.name for asset in pack.runtime_files} == set(FILES)
    assert 'memory.py' in FILES
    schema = json.loads(lab_packs.module_runtime_source(pack.lab_id, 'schema.json'))
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


def test_social_compatibility_does_not_alias_market_or_appear_in_catalog():
    legacy = lab_packs.load_lab_pack('ai_social_network')
    assert legacy.pack_version == '1.1.1'
    assert legacy.lab_id not in {pack.lab_id for pack in lab_packs.lab_catalog()}
    assert not (lab_packs.LABS_ROOT / legacy.lab_id).exists()
    assert all(asset.path.is_relative_to(lab_packs.LEGACY_LABS_ROOT) for asset in legacy.runtime_files)


@pytest.mark.parametrize('filename', ['../outside.py', '/outside.py', 'nested/file.py', 'C:/outside.py', 'nested\\file.py', '..'])
def test_flat_market_asset_boundary_rejects_paths(tmp_path, filename):
    with pytest.raises(lab_packs.LabPackError, match='asset path'):
        lab_packs._module_assets(tmp_path, [{'file': filename, 'sha256': '0' * 64}], None)


def test_loaded_runtime_rejects_later_content_drift(tmp_path):
    source = tmp_path / 'runtime.py'
    source.write_bytes(b'initial')
    asset = lab_packs._module_assets(tmp_path, [{'file': source.name, 'sha256': hashlib.sha256(b'initial').hexdigest()}], None)[0]
    source.write_bytes(b'changed')
    with pytest.raises(lab_packs.LabPackError, match='SHA-256 changed'):
        asset.read_bytes()
