#!/usr/bin/env python3
"""Root-only pinned viewer update; published data stays in Object Storage/Autonomous."""
from __future__ import annotations

import argparse
from pathlib import Path

from load_release_image import load
from vm_release_updater import (
    _atomic_json, _container_exists, _healthy, _remove_container, _run, _singleton_lock,
)

APP, CANDIDATE, PREVIOUS = "prisma-viewer", "prisma-viewer-candidate", "prisma-viewer-previous"


def run_container(root: Path, name: str, image: str, candidate: bool) -> None:
    # Native feed caches include provider rate budgets; updates and rollback keep them.
    cache = root / "native-cache"
    _run(["install", "-d", "-m", "0700", "-o", "65534", "-g", "65534", str(cache)])
    _run([
        "docker", "run", "-d", "--name", name, "--restart", "no" if candidate else "unless-stopped",
        "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m",
        "--security-opt", "no-new-privileges:true", "--cap-drop", "ALL",
        "-v", f"{cache}:/app/.upstream/.gev-cache:rw,z",
        "--env-file", str(root / "runtime.env"), "-p",
        "127.0.0.1:18081:8081" if candidate else "8081:8081", image,
    ])


def update(root: Path, release: str, commit: str) -> None:
    # Recover an interrupted swap before loading anything or deleting a previous image.
    if not _container_exists(APP) and _container_exists(PREVIOUS):
        _run(["docker", "rename", PREVIOUS, APP])
        _run(["docker", "start", APP])
    if not _container_exists(APP) or not _healthy("http://127.0.0.1:8081/health", attempts=3):
        raise RuntimeError("viewer_current_unhealthy; restore the prior container before updating")
    image = load(release, commit, "territorial-viewer")
    _remove_container(CANDIDATE)
    try:
        run_container(root, CANDIDATE, image, True)
        if not _healthy("http://127.0.0.1:18081/ready"):
            raise RuntimeError("viewer_candidate_not_ready")
        _remove_container(CANDIDATE)
        _remove_container(PREVIOUS)
        _run(["docker", "stop", "--time", "30", APP])
        _run(["docker", "rename", APP, PREVIOUS])
        try:
            run_container(root, APP, image, False)
            if not _healthy("http://127.0.0.1:8081/ready"):
                raise RuntimeError("viewer_activation_not_ready")
        except Exception:
            _remove_container(APP)
            _run(["docker", "rename", PREVIOUS, APP])
            _run(["docker", "start", APP])
            if not _healthy("http://127.0.0.1:8081/health"):
                raise RuntimeError("viewer_rollback_unhealthy") from None
            raise
        _atomic_json(root / "release.json", {"release": release, "commit_sha": commit, "image": image})
    finally:
        _remove_container(CANDIDATE)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True)
    parser.add_argument("--commit", required=True)
    arguments = parser.parse_args()
    with _singleton_lock(Path("/run/prisma-release-update.lock")):
        update(Path("/opt/prisma"), arguments.release, arguments.commit)
