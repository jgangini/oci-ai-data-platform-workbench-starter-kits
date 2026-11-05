"""Validate the explicit source-to-AIDP publication contract without cloud access."""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath


STANDARD_MODULES = frozenset({"ai_data_governance", "ai_market_intelligence"})


def source_file(root: Path, name: str) -> Path:
    if not isinstance(name, str) or not name or "\\" in name:
        raise ValueError("Invalid AIDP source path")
    relative = PurePosixPath(name)
    if relative.is_absolute() or relative.as_posix() != name or any(part in {".", ".."} for part in relative.parts):
        raise ValueError("AIDP source path must stay inside its module")
    if any(part in {".oci", ".aws", ".local", ".aidp-local", ".codex", "__pycache__"} for part in relative.parts) or relative.name.startswith(".env") or relative.suffix.lower() in {".pem", ".key", ".p12", ".tfstate", ".pyc", ".pyo"}:
        raise ValueError("Private configuration cannot be an AIDP publication source")
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError(f"Missing or external AIDP source: {name}")
    return path


def load_aidp_manifest(root: Path, module_id: str) -> dict:
    manifest = json.loads(source_file(root, "aidp/manifest.json").read_text(encoding="utf-8"))
    publication_root = "/Workspace/.artifact/" + module_id
    if (not isinstance(manifest, dict) or manifest.get("schema_version") != 1
            or manifest.get("module_id") != module_id or module_id not in STANDARD_MODULES
            or manifest.get("workspace_id") != "default" or manifest.get("publication_root") != publication_root):
        raise ValueError("Invalid AIDP module identity or publication root")
    assets = manifest.get("assets")
    if not isinstance(assets, list) or not assets:
        raise ValueError("An AIDP module must declare its publication assets")
    targets = set()
    for asset in assets:
        if not isinstance(asset, dict):
            raise ValueError("Invalid AIDP publication asset")
        target = asset.get("workspace_path")
        if (not isinstance(target, str) or not target.startswith(publication_root + "/")
                or "\\" in target or PurePosixPath(target).as_posix() != target
                or ".." in PurePosixPath(target).parts or target in targets):
            raise ValueError("AIDP publication targets must be unique and inside their module root")
        targets.add(target)
        if asset.get("kind") not in {"python", "notebook", "text", "json", "data"} or type(asset.get("generated")) is not bool:
            raise ValueError("Invalid AIDP asset kind or generation contract")
        assembly = asset.get("assembly_sources")
        if not isinstance(assembly, list) or any(not isinstance(name, str) for name in assembly) or len(assembly) != len(set(assembly)):
            raise ValueError("Invalid AIDP assembly source closure")
        if asset["generated"]:
            if asset.get("source") is not None or not assembly:
                raise ValueError("Generated AIDP assets need an explicit source closure")
        else:
            source = asset.get("source")
            source_file(root, source)
            files_root = "aidp/workspaces/default/files/"
            if not source.startswith(files_root) or target != publication_root + "/" + source.removeprefix(files_root):
                raise ValueError("Editable AIDP assets must mirror their workspace files path")
            if assembly:
                raise ValueError("Editable AIDP assets cannot have another canonical source")
        for name in assembly:
            source_file(root, name)
    model = manifest.get("model")
    if not isinstance(model, dict) or not isinstance(model.get("tables"), list):
        raise ValueError("Invalid AIDP data model contract")
    definitions = ([model["metadata"]] if model.get("metadata") is not None else []) + model["tables"]
    for key in ("workflows", "compute", "agents"):
        values = manifest.get(key)
        if not isinstance(values, list):
            raise ValueError(f"AIDP module must declare {key}")
        definitions.extend(values)
    if any(not isinstance(name, str) for name in definitions) or len(definitions) != len(set(definitions)):
        raise ValueError("Duplicate AIDP resource definitions")
    for name in definitions:
        if not isinstance(json.loads(source_file(root, name).read_text(encoding="utf-8")), dict):
            raise ValueError("AIDP resource definitions must be JSON objects")
    return manifest


def manifest_sources(manifest: dict) -> set[str]:
    """Return every input whose bytes must be covered by package hashes."""
    sources = {"aidp/manifest.json", *manifest["model"]["tables"]}
    if manifest["model"].get("metadata"):
        sources.add(manifest["model"]["metadata"])
    for key in ("workflows", "compute", "agents"):
        sources.update(manifest[key])
    for asset in manifest["assets"]:
        if asset["source"]:
            sources.add(asset["source"])
        sources.update(asset["assembly_sources"])
    return sources
