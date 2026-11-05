"""Regression checks for Governance's single editable AIDP resource tree."""
from pathlib import Path
import json
from unittest.mock import Mock

import pytest

from app.modules.ai_data_governance import schema
from app.modules.ai_data_governance.constants import RUNTIME_FAMILY, governance_compute_payload
from app.modules.ai_data_governance.deployment_bundle import build_deployment_bundle
from app.packages.aidp import load_aidp_manifest
from app.services.oci.aidp import AidpClient, AidpProvisionConflict, AidpProvisionError
from tests.modules.ai_data_governance.test_ai_dg_deployment_bundle import binding


ROOT = Path(schema.__file__).resolve().parent


def test_master_catalog_has_one_canonical_definition_for_each_of_eighteen_tables():
    contract = load_aidp_manifest(ROOT, 'ai_data_governance')
    tables = contract['model']['tables']
    assert len(tables) == 18 and len(set(tables)) == 18
    assert {p.relative_to(ROOT).as_posix() for p in schema.MODEL_ROOT.joinpath('tables').glob('*.json')} == set(tables)
    assert [json.loads((ROOT / name).read_text(encoding='utf-8')) for name in tables] == schema.load_model()['tables']
    assert not (ROOT / 'package/source/ai_dg_model.json').exists()
    assert not (ROOT / 'legacy.py').exists()
    assert not any((ROOT / 'package/notebooks').rglob('*.py'))
    assert not any((ROOT / 'package/notebooks').rglob('*.ipynb'))


def test_published_files_match_exact_declared_sources_and_generated_assets():
    contract = load_aidp_manifest(ROOT, 'ai_data_governance')
    bundle = build_deployment_bundle(binding())
    assert set(bundle['files']) == {asset['workspace_path'] for asset in contract['assets']}
    for asset in contract['assets']:
        if not asset['generated']:
            assert bundle['files'][asset['workspace_path']] == (ROOT / asset['source']).read_bytes()
    assert bundle['manifest']['deployment_stage'] == 'TEST'


def test_compute_payload_uses_the_native_descriptor():
    contract = load_aidp_manifest(ROOT, 'ai_data_governance')
    definitions = [json.loads((ROOT / path).read_text(encoding='utf-8')) for path in contract['compute']]
    owned = next(item for item in definitions if item['type'] == 'AI_COMPUTE')
    assert governance_compute_payload() == owned['payload']
    assert owned['payload']['displayName'] == 'aidp_data_governance_agent_compute'


@pytest.mark.parametrize('family', [None, 'legacy', 'AI_DG_V2_TEST'])
def test_retired_receipts_are_rejected_before_any_native_mutation(family):
    receipt = {'schema_version': 1, 'module_id': 'ai_data_governance', 'runtime_family': family,
        'status': 'active', 'operation': {'operation_id': '77777777-7777-4777-8777-777777777777', 'type': 'install'}}
    assert family != RUNTIME_FAMILY
    client = object.__new__(AidpClient)
    client._workspace_json = Mock(return_value=receipt)
    client._request = Mock()
    with pytest.raises(AidpProvisionError, match='invalid'):
        client._module_manifest('workspace')
    client._request.assert_not_called()


@pytest.mark.parametrize('operation', ['redeploy', 'delete'])
def test_unsupported_lifecycle_does_not_read_or_mutate_native_resources(operation):
    client = object.__new__(AidpClient)
    client._governance_workspace = Mock()
    client._request = Mock()
    with pytest.raises(AidpProvisionConflict, match='reviewed'):
        client._reconcile_governance_module('user', 'operation', operation)
    client._governance_workspace.assert_not_called()
    client._request.assert_not_called()


@pytest.mark.parametrize('metadata', [None, '../private.json', 'aidp/master_catalog/oci_medallion/schemas/oci_artifacts/other.json'])
def test_model_rejects_metadata_that_disagrees_with_its_canonical_location(metadata, tmp_path, monkeypatch):
    manifest = json.loads((ROOT / 'aidp/manifest.json').read_text(encoding='utf-8'))
    manifest['model']['metadata'] = metadata
    target = tmp_path / 'aidp'
    target.mkdir()
    (target / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    canonical_root = target / 'master_catalog/oci_medallion/schemas/oci_artifacts'
    canonical_root.mkdir(parents=True)
    if isinstance(metadata, str) and metadata.endswith('/other.json'):
        # Even an existing, otherwise valid metadata JSON cannot be silently ignored.
        (tmp_path / metadata).write_text((ROOT / manifest['model']['metadata'].replace('other.json', 'schema.json')).read_text(encoding='utf-8'), encoding='utf-8')
    monkeypatch.setattr(schema, 'AIDP_ROOT', target)
    monkeypatch.setattr(schema, 'MODEL_ROOT', canonical_root)
    with pytest.raises(ValueError, match='canonical Master Catalog path'):
        schema.load_model()


def test_assembly_closures_declare_actual_model_prompt_entry_and_workflow_resources():
    contract = load_aidp_manifest(ROOT, 'ai_data_governance')
    model_resources = {'aidp/manifest.json', contract['model']['metadata'], *contract['model']['tables']}
    generated = {asset['workspace_path']: set(asset['assembly_sources']) for asset in contract['assets'] if asset['generated']}
    publication = contract['publication_root']
    assert model_resources <= generated[publication + '/bootstrap18tables.py']
    for task in ('sync_inventory', 'publish_metadata', 'replicate_model'):
        assert model_resources <= generated[publication + '/tasks/' + task + '.py']
    for folder, entry, runtime in (
        ('governance', 'governance_agent.py', 'governance_runtime.py'),
        ('documentation', 'documentation_agent.py', 'documentary_runtime.py'),
    ):
        resources = {f'aidp/workspaces/default/files/agents/{folder}/{entry}', f'aidp/workspaces/default/files/agents/{folder}/prompts.md'}
        assert resources | model_resources <= generated[f'{publication}/agents/{folder}/{runtime}']
    assert set(contract['workflows']) <= generated[publication + '/workflow.json']
    for sources in generated.values():
        assert sources <= generated[publication + '/manifest.json']
