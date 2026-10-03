#!/usr/bin/env python3
"""Load one frozen image from the selected immutable Starter Kits release."""
from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import urllib.request
from pathlib import Path

from vm_release_updater import REPOSITORY, SHA, SHA256, _download, semantic_version


COMPONENTS = {
    "aidp-lab": ("aidp-lab-image-amd64.tar.gz", "aidp-release.json"),
    "prisma-viewer": ("prisma-viewer-image-amd64.tar.gz", "prisma-release.json"),
}


def selected_assets(release: dict, tag: str, component: str) -> dict:
    if (not semantic_version(tag) or release.get("tag_name") != tag
            or release.get("immutable") is not True or release.get("draft")
            or release.get("prerelease")):
        raise ValueError("Selected release is not immutable and stable")
    selected = {}
    for name in COMPONENTS[component]:
        matches = [item for item in release.get("assets", []) if item.get("name") == name]
        if len(matches) != 1:
            raise ValueError("Release asset is missing or ambiguous")
        asset = matches[0]
        raw_digest = str(asset.get("digest", ""))
        digest = raw_digest.removeprefix("sha256:")
        expected_url = f"{REPOSITORY.removesuffix('.git')}/releases/download/{tag}/{name}"
        if (not raw_digest.startswith("sha256:") or not SHA256.fullmatch(digest)
                or asset.get("browser_download_url") != expected_url):
            raise ValueError("Release asset identity or digest is invalid")
        selected[name] = (expected_url, digest)
    return selected


def validate_manifest(manifest: dict, *, tag: str, commit: str, component: str, digest: str) -> str:
    image = manifest.get("image", {})
    expected_tag = f"{component}:{commit}"
    if (not SHA.fullmatch(commit) or manifest.get("schema_version") != 1
            or manifest.get("updater_protocol") != 1 or manifest.get("release") != tag
            or manifest.get("commit_sha") != commit
            or manifest.get("repository") != REPOSITORY.removesuffix(".git")
            or image.get("asset_name") != COMPONENTS[component][0]
            or image.get("sha256") != digest or image.get("platform") != "linux/amd64"
            or image.get("image_tag") != expected_tag):
        raise ValueError("Release manifest does not match the deployment SHA")
    return expected_tag


def load(tag: str, commit: str, component: str) -> str:
    if not semantic_version(tag) or not SHA.fullmatch(commit):
        raise ValueError("Invalid deployment release or SHA")
    repository = REPOSITORY.removesuffix(".git").removeprefix("https://github.com/")
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/releases/tags/{tag}",
        headers={"User-Agent": "starter-kits-image-bootstrap", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        document = response.read(1_048_577)
    if len(document) > 1_048_576:
        raise ValueError("Release document exceeds the size limit")
    assets = selected_assets(json.loads(document), tag, component)
    image_name, manifest_name = COMPONENTS[component]
    with tempfile.TemporaryDirectory(prefix="starter-image-") as directory:
        root = Path(directory)
        _download(assets[manifest_name][0], root / manifest_name, assets[manifest_name][1], 65536)
        image_tag = validate_manifest(json.loads((root / manifest_name).read_text()), tag=tag,
                                      commit=commit, component=component, digest=assets[image_name][1])
        _download(assets[image_name][0], root / image_name, assets[image_name][1], 4 * 1024 ** 3)
        subprocess.run(["docker", "load", "--input", str(root / image_name)], check=True)
        platform = subprocess.check_output(
            ["docker", "image", "inspect", image_tag, "--format", "{{.Os}}/{{.Architecture}}"], text=True,
        ).strip()
        if platform != "linux/amd64":
            raise ValueError("Release image has an unexpected platform")
    return image_tag


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--component", choices=COMPONENTS, required=True)
    arguments = parser.parse_args()
    print(load(arguments.release, arguments.commit, arguments.component))
