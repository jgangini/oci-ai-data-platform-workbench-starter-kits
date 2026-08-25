#!/usr/bin/env python3
"""Host-only updater for immutable Starter Kits releases.

The web container can enqueue only an opaque UUID in its persistent state volume.
This root-owned helper chooses the target release itself, validates it, probes a
candidate container, and retains the prior container until the replacement is healthy.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import UUID


REPOSITORY = "https://github.com/jgangini/oci-ai-data-platform-workbench-starter-kits.git"
RELEASE_API = (
    "https://api.github.com/repos/jgangini/"
    "oci-ai-data-platform-workbench-starter-kits/releases/latest"
)
SEMVER_TAG = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
SHA = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
MANIFEST_ASSET = "aidp-release.json"
IMAGE_ASSET = "aidp-lab-image-amd64.tar.gz"
MANIFEST_LIMIT = 64 * 1024
IMAGE_LIMIT = 4 * 1024 * 1024 * 1024
RUNNING = frozenset(
    {"queued", "checking", "downloading", "building", "validating", "activating"}
)
TERMINAL = frozenset({"succeeded", "up_to_date", "failed"})
APP_NAME = "aidp-lab"
ROLLBACK_NAME = "aidp-lab-rollback"
CANDIDATE_NAME = "aidp-lab-candidate"


def semantic_version(tag: str) -> tuple[int, int, int] | None:
    match = SEMVER_TAG.fullmatch(tag)
    return tuple(map(int, match.groups())) if match else None


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    os.replace(temporary, path)


def _run(arguments: list[str], *, capture: bool = False) -> str:
    result = subprocess.run(
        arguments,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    return result.stdout.strip() if capture else ""


def validated_release(payload: Any) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise RuntimeError("invalid_release_document")
    tag = str(payload.get("tag_name") or "")
    if semantic_version(tag) is None:
        raise RuntimeError("invalid_release_tag")
    if payload.get("draft") is not False or payload.get("prerelease") is not False:
        raise RuntimeError("release_not_stable")
    if payload.get("immutable") is not True:
        raise RuntimeError("release_not_immutable")
    raw_assets = payload.get("assets")
    if not isinstance(raw_assets, list):
        raise RuntimeError("release_assets_missing")
    result = {"tag": tag}
    for name, prefix in (
        (MANIFEST_ASSET, "manifest"),
        (IMAGE_ASSET, "image"),
    ):
        matches = [
            item
            for item in raw_assets
            if isinstance(item, dict) and item.get("name") == name
        ]
        expected_url = (
            f"{REPOSITORY.removesuffix('.git')}/releases/download/{tag}/{name}"
        )
        if len(matches) != 1:
            raise RuntimeError("release_asset_invalid")
        asset = matches[0]
        digest = str(asset.get("digest") or "")
        if (
            asset.get("browser_download_url") != expected_url
            or not digest.startswith("sha256:")
            or SHA256.fullmatch(digest.removeprefix("sha256:")) is None
        ):
            raise RuntimeError("release_asset_invalid")
        result[f"{prefix}_url"] = expected_url
        result[f"{prefix}_sha256"] = digest.removeprefix("sha256:")
    return result


def latest_release() -> dict[str, str]:
    request = urllib.request.Request(
        RELEASE_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "oci-aidp-starter-kits-updater",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        content = response.read(1_048_577)
    if len(content) > 1_048_576:
        raise RuntimeError("release_document_too_large")
    return validated_release(json.loads(content))


def _download(
    url: str,
    destination: Path,
    expected_sha256: str,
    maximum_bytes: int,
) -> None:
    if SHA256.fullmatch(expected_sha256) is None:
        raise RuntimeError("invalid_asset_digest")
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/octet-stream",
            "User-Agent": "oci-aidp-starter-kits-updater",
        },
    )
    digest = hashlib.sha256()
    size = 0
    with urllib.request.urlopen(request, timeout=30) as response, destination.open(
        "xb"
    ) as output:
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            if size > maximum_bytes:
                raise RuntimeError("release_asset_too_large")
            digest.update(chunk)
            output.write(chunk)
    destination.chmod(0o600)
    if digest.hexdigest() != expected_sha256:
        raise RuntimeError("release_asset_digest_mismatch")


def _validated_manifest(value: Any, release: dict[str, str]) -> dict[str, str]:
    if not isinstance(value, dict):
        raise RuntimeError("invalid_release_manifest")
    image = value.get("image")
    tag = release["tag"]
    commit_sha = str(value.get("commit_sha") or "")
    if (
        value.get("schema_version") != 1
        or value.get("updater_protocol") != 1
        or value.get("release") != tag
        or value.get("repository") != REPOSITORY.removesuffix(".git")
        or SHA.fullmatch(commit_sha) is None
        or not isinstance(image, dict)
        or image.get("asset_name") != IMAGE_ASSET
        or image.get("sha256") != release["image_sha256"]
        or image.get("platform") != "linux/amd64"
        or image.get("image_tag") != f"aidp-lab:{commit_sha}"
    ):
        raise RuntimeError("invalid_release_manifest")
    return {
        "release": tag,
        "commit_sha": commit_sha,
        "image_sha256": release["image_sha256"],
        "image_tag": f"aidp-lab:{commit_sha}",
    }


def _prepare_artifact(
    releases_dir: Path,
    operation_id: str,
    release: dict[str, str],
) -> tuple[Path, dict[str, str], Path]:
    stage = releases_dir / f"update-{operation_id}"
    _safe_rmtree(stage, releases_dir)
    stage.mkdir(mode=0o700)
    try:
        manifest_path = stage / MANIFEST_ASSET
        _download(
            release["manifest_url"],
            manifest_path,
            release["manifest_sha256"],
            MANIFEST_LIMIT,
        )
        manifest = _validated_manifest(_json(manifest_path), release)
        if resolved_tag_sha(manifest["release"]) != manifest["commit_sha"]:
            raise RuntimeError("release_commit_mismatch")
        image_path = stage / IMAGE_ASSET
        _download(
            release["image_url"],
            image_path,
            manifest["image_sha256"],
            IMAGE_LIMIT,
        )
        return stage, manifest, image_path
    except Exception:
        _safe_rmtree(stage, releases_dir)
        raise


def resolved_tag_sha(tag: str) -> str:
    if semantic_version(tag) is None:
        raise RuntimeError("invalid_release_tag")
    output = _run(
        [
            "git",
            "ls-remote",
            "--tags",
            REPOSITORY,
            f"refs/tags/{tag}",
            f"refs/tags/{tag}^{{}}",
        ],
        capture=True,
    )
    refs = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) == 2 and SHA.fullmatch(parts[0]):
            refs[parts[1]] = parts[0]
    commit_sha = refs.get(f"refs/tags/{tag}^{{}}") or refs.get(f"refs/tags/{tag}")
    if not commit_sha:
        raise RuntimeError("release_tag_not_found")
    return commit_sha


def _request(path: Path) -> str | None:
    try:
        if path.is_symlink():
            return None
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            content = handle.read(4097)
        if len(content) > 4096:
            return None
        value = json.loads(content)
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(value, dict):
        return None
    try:
        operation_id = str(UUID(str(value.get("operation_id") or "")))
    except ValueError:
        return None
    if value.get("schema_version") != 1 or value.get("action") != "update":
        return None
    return operation_id


def _release_state(path: Path) -> tuple[str, str]:
    value = _json(path)
    release = str(value.get("release") or "")
    commit_sha = str(value.get("commit_sha") or "")
    repository = str(value.get("repository") or "").removesuffix(".git")
    if (
        value.get("schema_version") != 1
        or semantic_version(release) is None
        or SHA.fullmatch(commit_sha) is None
        or repository != REPOSITORY.removesuffix(".git")
    ):
        raise RuntimeError("invalid_current_release_state")
    return release, commit_sha


def _status(
    path: Path,
    operation_id: str,
    state: str,
    message: str,
    current_release: str,
    current_sha: str,
    target_release: str = "",
    target_sha: str = "",
) -> None:
    value: dict[str, Any] = {
        "schema_version": 1,
        "operation_id": operation_id,
        "status": state,
        "message": message,
        "current_release": current_release,
        "current_commit_sha": current_sha,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if target_release:
        value["target_release"] = target_release
    if target_sha:
        value["target_commit_sha"] = target_sha
    _atomic_json(path, value)


def _safe_rmtree(path: Path, root: Path) -> None:
    resolved = path.resolve()
    resolved_root = root.resolve()
    if resolved == resolved_root or not resolved.is_relative_to(resolved_root):
        raise RuntimeError("unsafe_staging_path")
    if path.exists():
        shutil.rmtree(path)


def _snapshot_state(state_dir: Path, releases_dir: Path, operation_id: str) -> Path:
    snapshot = releases_dir / f"candidate-state-{operation_id}"
    _safe_rmtree(snapshot, releases_dir)
    for directory, names, files in os.walk(state_dir, followlinks=False):
        for name in [*names, *files]:
            if (Path(directory) / name).is_symlink():
                raise RuntimeError("unsafe_state_symlink")
    shutil.copytree(
        state_dir,
        snapshot,
        ignore=lambda directory, _names: (
            {"update"} if Path(directory) == state_dir else set()
        ),
    )
    (snapshot / "update/inbox").mkdir(parents=True, mode=0o700)
    (snapshot / "update/status").mkdir(mode=0o700)
    return snapshot


def _image_environment(image: str) -> dict[str, str]:
    output = _run(
        [
            "docker",
            "image",
            "inspect",
            "--format",
            "{{range .Config.Env}}{{println .}}{{end}}",
            image,
        ],
        capture=True,
    )
    return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)


def _load_image(archive: Path, tag: str, commit_sha: str) -> str:
    image = f"aidp-lab:{commit_sha}"
    _run(["docker", "image", "load", "--input", str(archive)])
    environment = _image_environment(image)
    if (
        environment.get("APP_RELEASE_TAG") != tag
        or environment.get("APP_RELEASE_SHA") != commit_sha
        or environment.get("APP_REPOSITORY") != REPOSITORY.removesuffix(".git")
    ):
        raise RuntimeError("candidate_release_metadata_mismatch")
    platform = _run(
        [
            "docker",
            "image",
            "inspect",
            "--format",
            "{{.Os}}/{{.Architecture}}",
            image,
        ],
        capture=True,
    )
    if platform != "linux/amd64":
        raise RuntimeError("candidate_platform_mismatch")
    return image


def _container_exists(name: str) -> bool:
    return subprocess.run(
        ["docker", "container", "inspect", name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0


def _container_image(name: str) -> str:
    return _run(
        ["docker", "container", "inspect", "--format", "{{.Config.Image}}", name],
        capture=True,
    )


def _container_running(name: str) -> bool:
    return (
        _run(
            [
                "docker",
                "container",
                "inspect",
                "--format",
                "{{.State.Running}}",
                name,
            ],
            capture=True,
        )
        == "true"
    )


def _remove_container(name: str) -> None:
    if _container_exists(name):
        subprocess.run(
            ["docker", "rm", "-f", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


def _run_container(
    root: Path,
    name: str,
    image: str,
    *,
    candidate: bool,
    state_dir: Path | None = None,
) -> None:
    mounted_state = state_dir or root / "state"
    arguments = [
        "docker",
        "run",
        "-d",
        "--name",
        name,
        "--restart",
        "no" if candidate else "unless-stopped",
        "--security-opt",
        "no-new-privileges:true",
        "--cap-drop",
        "ALL",
        "--cap-add",
        "CHOWN",
        "--cap-add",
        "NET_BIND_SERVICE",
        "--cap-add",
        "SETGID",
        "--cap-add",
        "SETUID",
        "--read-only",
        "--tmpfs",
        "/run:rw,noexec,nosuid,nodev,size=16m",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=64m",
        "--tmpfs",
        "/var/lib/nginx:rw,noexec,nosuid,nodev,size=64m",
        "--tmpfs",
        "/var/log/nginx:rw,noexec,nosuid,nodev,size=16m",
        "--env-file",
        str(root / ".env"),
        "-p",
        "127.0.0.1:18443:443" if candidate else "80:80",
    ]
    if not candidate:
        arguments.extend(["-p", "443:443"])
    arguments.extend(
        [
            "-v",
            f"{root / 'tls'}:/etc/aidp-lab/tls:ro,Z",
            "-v",
            f"{root / '.oci'}:/etc/aidp-lab/oci:ro,Z",
            "-v",
            f"{root / 'autonomous'}:/etc/aidp-lab/autonomous:ro,Z",
            "-v",
            f"{mounted_state}:/var/lib/aidp-lab:Z",
        ]
    )
    if not candidate:
        arguments.extend(
            [
                "-v",
                f"{root / 'state/update'}:/var/lib/aidp-lab/update:ro,Z",
                "-v",
                f"{root / 'state/update/inbox'}:/var/lib/aidp-lab/update/inbox:rw,Z",
            ]
        )
    arguments.append(image)
    _run(arguments)


def _healthy(url: str, attempts: int = 60) -> bool:
    for _ in range(attempts):
        result = subprocess.run(
            [
                "curl",
                "--silent",
                "--insecure",
                "--output",
                "/dev/null",
                "--write-out",
                "%{http_code}",
                url,
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode == 0 and result.stdout.strip() == "200":
            return True
        time.sleep(5)
    return False


def _restore_previous_container(expected_image: str | None = None) -> None:
    if (
        expected_image
        and _container_exists(APP_NAME)
        and _container_image(APP_NAME) == expected_image
    ):
        if not _container_running(APP_NAME):
            _run(["docker", "start", APP_NAME])
        if not _healthy("https://127.0.0.1/api/health", attempts=24):
            raise RuntimeError("rollback_health_failed")
        return
    if _container_exists(ROLLBACK_NAME):
        _remove_container(APP_NAME)
        _run(["docker", "rename", ROLLBACK_NAME, APP_NAME])
    if not _container_exists(APP_NAME):
        raise RuntimeError("rollback_container_missing")
    if not _container_running(APP_NAME):
        _run(["docker", "start", APP_NAME])
    if not _healthy("https://127.0.0.1/api/health", attempts=24):
        raise RuntimeError("rollback_health_failed")


def _activate(root: Path, image: str, current_sha: str) -> None:
    current_image = f"aidp-lab:{current_sha}"
    if _container_exists(ROLLBACK_NAME):
        if not _container_exists(APP_NAME):
            _restore_previous_container(current_image)
        else:
            active_image = _container_image(APP_NAME)
            if active_image == image:
                if _healthy("https://127.0.0.1/api/health", attempts=24):
                    return
                _restore_previous_container(current_image)
                raise RuntimeError("replacement_health_failed")
            if active_image != current_image:
                _restore_previous_container(current_image)
                raise RuntimeError("unexpected_active_container")
            if not _container_running(APP_NAME):
                _run(["docker", "start", APP_NAME])
            if not _healthy("https://127.0.0.1/api/health", attempts=24):
                _restore_previous_container(current_image)
                raise RuntimeError("current_health_failed")
            # The active current release is verified before discarding a rollback
            # retained from an older, already-completed update.
            _remove_container(ROLLBACK_NAME)
    if not _container_exists(APP_NAME):
        raise RuntimeError("active_container_missing")
    active_image = _container_image(APP_NAME)
    if active_image == image:
        if _healthy("https://127.0.0.1/api/health", attempts=24):
            return
        raise RuntimeError("replacement_health_failed_without_rollback")
    if active_image != current_image:
        raise RuntimeError("unexpected_active_container")
    if not _container_running(APP_NAME):
        _run(["docker", "start", APP_NAME])
    if not _healthy("https://127.0.0.1/api/health", attempts=24):
        raise RuntimeError("current_health_failed")
    _run(["docker", "stop", "--time", "30", APP_NAME])
    _run(["docker", "rename", APP_NAME, ROLLBACK_NAME])
    _run_container(root, APP_NAME, image, candidate=False)
    if not _healthy("https://127.0.0.1/api/health"):
        _restore_previous_container(current_image)
        raise RuntimeError("replacement_health_failed")


def reconcile(root: Path, operation_id: str) -> None:
    state_dir = root / "state"
    update_dir = state_dir / "update"
    releases_dir = root / "releases"
    status_path = update_dir / "status/status.json"
    release_state_path = root / "release.json"
    current_release, current_sha = _release_state(release_state_path)
    previous = _json(status_path)
    if (
        previous.get("operation_id") == operation_id
        and previous.get("status") in TERMINAL
    ):
        return
    phase = "checking"
    target_release = ""
    target_sha = ""
    stage: Path | None = None
    snapshot: Path | None = None
    current_release_state = _json(release_state_path)
    try:
        _status(
            status_path,
            operation_id,
            phase,
            "Checking the latest immutable GitHub release.",
            current_release,
            current_sha,
        )
        release = latest_release()
        target_release = release["tag"]
        if semantic_version(target_release) <= semantic_version(current_release):  # type: ignore[operator]
            _status(
                status_path,
                operation_id,
                "up_to_date",
                "The application already uses the latest immutable release.",
                current_release,
                current_sha,
                target_release,
            )
            return
        phase = "downloading"
        _status(
            status_path,
            operation_id,
            phase,
            "Downloading the verified release image.",
            current_release,
            current_sha,
            target_release,
            target_sha,
        )
        releases_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        stage, manifest, archive = _prepare_artifact(
            releases_dir, operation_id, release
        )
        target_sha = manifest["commit_sha"]
        image = _load_image(archive, target_release, target_sha)
        phase = "validating"
        _status(
            status_path,
            operation_id,
            phase,
            "Validating the candidate against live control services.",
            current_release,
            current_sha,
            target_release,
            target_sha,
        )
        _remove_container(CANDIDATE_NAME)
        snapshot = _snapshot_state(state_dir, releases_dir, operation_id)
        _run_container(
            root,
            CANDIDATE_NAME,
            image,
            candidate=True,
            state_dir=snapshot,
        )
        if not _healthy("https://127.0.0.1:18443/api/health"):
            raise RuntimeError("candidate_health_failed")
        _remove_container(CANDIDATE_NAME)

        phase = "activating"
        _status(
            status_path,
            operation_id,
            phase,
            "Activating the verified release.",
            current_release,
            current_sha,
            target_release,
            target_sha,
        )
        _activate(root, image, current_sha)
        _atomic_json(
            release_state_path,
            {
                "schema_version": 1,
                "repository": REPOSITORY.removesuffix(".git"),
                "release": target_release,
                "commit_sha": target_sha,
                "installed_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        _status(
            status_path,
            operation_id,
            "succeeded",
            f"Application updated to {target_release}.",
            target_release,
            target_sha,
            target_release,
            target_sha,
        )
    except Exception as exc:
        _remove_container(CANDIDATE_NAME)
        rollback_error: Exception | None = None
        try:
            _restore_previous_container(f"aidp-lab:{current_sha}")
            if current_release_state:
                _atomic_json(release_state_path, current_release_state)
        except Exception as restore_exc:
            rollback_error = restore_exc
        message = (
            f"Update failed during {phase}; no healthy rollback could be verified "
            f"({type(rollback_error).__name__}). Manual intervention is required."
            if rollback_error is not None
            else (
                f"Update failed during {phase}; the previous release was restored "
                f"and passed health checks ({type(exc).__name__})."
            )
        )
        _status(
            status_path,
            operation_id,
            "failed",
            message,
            current_release,
            current_sha,
            target_release,
            target_sha,
        )
        if rollback_error is not None:
            raise RuntimeError("rollback_health_failed") from rollback_error
        raise
    finally:
        if snapshot is not None:
            _safe_rmtree(snapshot, releases_dir)
        if stage is not None:
            _safe_rmtree(stage, releases_dir)


@contextmanager
def _singleton_lock(path: Path) -> Iterator[None]:
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("update_already_running") from None
        yield


def main() -> int:
    root = Path(os.environ.get("AIDP_APP_ROOT", "/opt/aidp-lab"))
    operation_id = _request(root / "state/update/inbox/request.json")
    if not operation_id:
        return 0
    try:
        with _singleton_lock(Path("/run/aidp-lab-update.lock")):
            reconcile(root, operation_id)
    except Exception as exc:
        print(f"AIDP application update failed ({type(exc).__name__})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
