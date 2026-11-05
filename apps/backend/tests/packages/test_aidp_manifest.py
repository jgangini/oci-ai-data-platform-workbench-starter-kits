import json
import shutil

import pytest

from app.packages.aidp import load_aidp_manifest


@pytest.fixture
def publication(tmp_path):
    (tmp_path / "aidp").mkdir()
    (tmp_path / "entry.py").write_text("raise AssertionError('Validation must not execute source')", encoding="utf-8")
    source = tmp_path / "aidp/workspaces/default/files/agents/agent.py"
    source.parent.mkdir(parents=True)
    source.write_text("raise AssertionError('Validation must not execute source')", encoding="utf-8")
    (tmp_path / "resource.json").write_text('{"name":"agent"}', encoding="utf-8")
    manifest = {
        "schema_version": 1, "module_id": "ai_data_governance", "workspace_id": "default",
        "publication_root": "/Workspace/.artifact/ai_data_governance",
        "model": {"metadata": None, "tables": []}, "workflows": [], "compute": [], "agents": ["resource.json"],
        "assets": [{"source": "aidp/workspaces/default/files/agents/agent.py", "workspace_path": "/Workspace/.artifact/ai_data_governance/agents/agent.py",
                    "kind": "python", "generated": False, "assembly_sources": []}],
    }
    return tmp_path, manifest


def read(publication):
    root, manifest = publication
    (root / "aidp/manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return load_aidp_manifest(root, "ai_data_governance")


def test_publication_validation_reads_sources_without_executing_them(publication):
    assert read(publication) == publication[1]
    asset = publication[1]["assets"][0]
    asset.update(source=None, generated=True, assembly_sources=["entry.py"])
    assert read(publication)["assets"][0]["generated"]


@pytest.mark.parametrize("source", ["../entry.py", "/entry.py", "aidp/../../entry.py", "aidp\\entry.py", "missing.py", ".oci/config", ".env", "private.key", "__pycache__/entry.pyc"])
def test_sources_cannot_escape_the_module_or_disappear(publication, source):
    publication[1]["assets"][0]["source"] = source
    with pytest.raises(ValueError):
        read(publication)


@pytest.mark.parametrize("target", ["/Workspace/other/agent.py", "/Workspace/.artifact/ai_data_governance/../other.py",
                                    "/Workspace/.artifact/ai_data_governance//agent.py"])
def test_publication_cannot_write_to_another_module(publication, target):
    publication[1]["assets"][0]["workspace_path"] = target
    with pytest.raises(ValueError):
        read(publication)


def test_duplicate_targets_or_missing_definitions_stop_publication(publication):
    publication[1]["assets"].append(dict(publication[1]["assets"][0]))
    with pytest.raises(ValueError, match="unique"):
        read(publication)
    publication[1]["assets"].pop()
    publication[1]["agents"] = ["missing.json"]
    with pytest.raises(ValueError, match="Missing"):
        read(publication)


def test_generated_assets_require_their_real_source_closure(publication):
    publication[1]["assets"][0].update(source=None, generated=True)
    with pytest.raises(ValueError, match="source closure"):
        read(publication)


def test_editable_sources_must_mirror_the_declared_workspace_path(publication):
    publication[1]["assets"][0]["source"] = "entry.py"
    with pytest.raises(ValueError, match="mirror"):
        read(publication)


@pytest.mark.parametrize("module_id", ["ai_data_governance", "ai_market_intelligence"])
def test_current_modules_use_the_same_explicit_contract(module_id):
    from app.packages.catalog import package_root, load_package

    manifest = load_aidp_manifest(package_root(module_id), module_id)
    pack = load_package(module_id, require_available=False)
    assert pack.kind == "global_module"
    assert manifest["agents"] and manifest["compute"]
    assert not pack.notebooks


@pytest.mark.parametrize("module_id", ["ai_data_governance", "ai_market_intelligence"])
def test_publication_input_without_package_hash_is_rejected(tmp_path, monkeypatch, module_id):
    from app.packages import catalog

    root = tmp_path / "module"
    shutil.copytree(catalog.package_root(module_id), root, ignore=shutil.ignore_patterns("__pycache__"))
    monkeypatch.setitem(catalog.MODULE_PACKAGE_ROOTS, module_id, root)
    metadata_path = catalog.package_metadata_path(module_id)
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["runtime_files"].pop(0)
    metadata["pack_sha256"] = catalog._pack_hash(metadata)
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(catalog.PackageError, match="package hashes"):
        catalog.load_package(module_id, require_available=False)


def test_hash_refresh_uses_only_declared_inputs_and_keeps_the_version(tmp_path, monkeypatch):
    from app.packages import catalog
    from scripts.validation.check_aidp_modules import refresh_hashes

    module_id = "ai_data_governance"
    root = tmp_path / "module"
    shutil.copytree(catalog.package_root(module_id), root, ignore=shutil.ignore_patterns("__pycache__"))
    monkeypatch.setitem(catalog.MODULE_PACKAGE_ROOTS, module_id, root)
    before = catalog.load_package(module_id, require_available=False)
    source = root / before.runtime_files[0].name
    source.write_bytes(source.read_bytes() + b"\n# intentional source change\n")
    (root / "unrelated.py").write_text("raise AssertionError('do not publish')", encoding="utf-8")
    (root / ".env").write_text("PRIVATE=do-not-publish", encoding="utf-8")
    with pytest.raises(catalog.PackageError, match="SHA-256 mismatch"):
        catalog.load_package(module_id, require_available=False)
    refresh_hashes(module_id)
    after = catalog.load_package(module_id, require_available=False)
    assert after.pack_version == before.pack_version
    assert after.pack_sha256 != before.pack_sha256
    assert {asset.name for asset in after.runtime_files} == {asset.name for asset in before.runtime_files}
    refresh_hashes(module_id, "4.0.1")
    assert catalog.load_package(module_id, require_available=False).pack_version == "4.0.1"
