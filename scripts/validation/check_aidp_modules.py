"""Check module publication sources, hashes and resource definitions offline."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps/backend"))

from app.packages.aidp import STANDARD_MODULES, load_aidp_manifest, manifest_sources, source_file
from app.packages.catalog import PACK_VERSION_PATTERN, load_package, package_root, package_metadata_path, _pack_contract, _pack_hash


def refresh_hashes(module_id, pack_version=None):
    """Hash only declared publication inputs, retaining the explicit package version."""
    root = package_root(module_id)
    manifest = load_aidp_manifest(root, module_id)
    path = package_metadata_path(module_id)
    metadata = json.loads(path.read_text(encoding="utf-8"))
    _pack_contract(metadata, module_id, False)
    if pack_version is not None:
        if not PACK_VERSION_PATTERN.fullmatch(pack_version):
            raise ValueError("Package version must be major.minor.patch")
        metadata["pack_version"] = pack_version
    definitions = {"aidp/manifest.json", *manifest["model"]["tables"]}
    if manifest["model"].get("metadata"):
        definitions.add(manifest["model"]["metadata"])
    for key in ("workflows", "compute", "agents"):
        definitions.update(manifest[key])
    previous = {asset["file"]: asset for key in ("runtime_files", "source_manifests") for asset in metadata[key]}
    assets = {"runtime_files": [], "source_manifests": []}
    for name in sorted(manifest_sources(manifest)):
        value = {**previous.get(name, {}), "file": name,
                 "sha256": hashlib.sha256(source_file(root, name).read_bytes()).hexdigest()}
        assets["source_manifests" if name in definitions else "runtime_files"].append(value)
    metadata.update(assets)
    metadata["pack_sha256"] = _pack_hash(metadata)
    path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def check_modules():
    results = []
    for module_id in sorted(STANDARD_MODULES):
        manifest = load_aidp_manifest(package_root(module_id), module_id)
        pack = load_package(module_id, require_available=False)
        for asset in pack.runtime_files:
            if asset.path.suffix == ".py":
                compile(asset.read_bytes(), asset.name, "exec")
        results.append({"module_id": module_id, "package_version": pack.pack_version,
                        "agents": len(manifest["agents"]), "tables": len(manifest["model"]["tables"]),
                        "workflows": len(manifest["workflows"]), "compute": len(manifest["compute"]),
                        "publication_assets": len(manifest["assets"]),
                        "generated_assets": sum(asset["generated"] for asset in manifest["assets"]),
                        "publication_root": manifest["publication_root"], "native_acceptance": "not_checked"})
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh-hashes", choices=sorted(STANDARD_MODULES),
                        help="Refresh declared source hashes after an intentional source change; keep the chosen version.")
    parser.add_argument("--pack-version", help="Explicit new package version; requires --refresh-hashes.")
    args = parser.parse_args()
    if args.pack_version and not args.refresh_hashes:
        parser.error("--pack-version requires --refresh-hashes")
    if args.refresh_hashes:
        refresh_hashes(args.refresh_hashes, args.pack_version)
    print(json.dumps(check_modules(), ensure_ascii=False, indent=2))
