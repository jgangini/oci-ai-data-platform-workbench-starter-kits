from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts import vm_release_updater as updater


ROOT = Path(__file__).parents[2]


def release_payload(tag: str = "v2.3.0") -> dict[str, object]:
    return {
        "tag_name": tag,
        "draft": False,
        "prerelease": False,
        "immutable": True,
        "assets": [
            {
                "name": name,
                "browser_download_url": (
                    f"{updater.REPOSITORY.removesuffix('.git')}/releases/download/{tag}/{name}"
                ),
                "digest": f"sha256:{character * 64}",
            }
            for name, character in (
                (updater.MANIFEST_ASSET, "a"),
                (updater.IMAGE_ASSET, "b"),
            )
        ],
    }


def test_updater_accepts_only_stable_immutable_semver_releases() -> None:
    release = release_payload()
    assert updater.validated_release(release)["tag"] == "v2.3.0"
    for change in (
        {"tag_name": "main"},
        {"draft": True},
        {"prerelease": True},
        {"immutable": False},
        {"assets": []},
    ):
        with pytest.raises(RuntimeError):
            updater.validated_release({**release, **change})


def test_updater_resolves_annotated_and_lightweight_tags(monkeypatch) -> None:
    tag_sha = "a" * 40
    commit_sha = "b" * 40
    monkeypatch.setattr(
        updater,
        "_run",
        lambda *_args, **_kwargs: (
            f"{tag_sha}\trefs/tags/v2.3.0\n"
            f"{commit_sha}\trefs/tags/v2.3.0^{{}}"
        ),
    )
    assert updater.resolved_tag_sha("v2.3.0") == commit_sha


def test_release_manifest_binds_the_asset_tag_commit_and_protocol() -> None:
    release = updater.validated_release(release_payload())
    commit_sha = "c" * 40
    manifest = {
        "schema_version": 1,
        "updater_protocol": 1,
        "release": "v2.3.0",
        "commit_sha": commit_sha,
        "repository": updater.REPOSITORY.removesuffix(".git"),
        "image": {
            "asset_name": updater.IMAGE_ASSET,
            "sha256": "b" * 64,
            "platform": "linux/amd64",
            "image_tag": f"aidp-lab:{commit_sha}",
        },
    }

    assert updater._validated_manifest(manifest, release)["commit_sha"] == commit_sha
    for invalid in (
        {"updater_protocol": 2},
        {"release": "v2.4.0"},
        {"commit_sha": "not-a-sha"},
        {"image": {**manifest["image"], "sha256": "d" * 64}},
    ):
        with pytest.raises(RuntimeError, match="invalid_release_manifest"):
            updater._validated_manifest({**manifest, **invalid}, release)


def test_prebuilt_image_must_match_release_metadata_and_platform(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    commit_sha = "c" * 40
    calls: list[list[str]] = []
    monkeypatch.setattr(
        updater,
        "_image_environment",
        lambda _image: {
            "APP_RELEASE_TAG": "v2.3.0",
            "APP_RELEASE_SHA": commit_sha,
            "APP_REPOSITORY": updater.REPOSITORY.removesuffix(".git"),
        },
    )
    monkeypatch.setattr(
        updater,
        "_run",
        lambda arguments, **kwargs: calls.append(arguments) or (
            "linux/amd64" if kwargs.get("capture") else ""
        ),
    )

    image = updater._load_image(tmp_path / updater.IMAGE_ASSET, "v2.3.0", commit_sha)

    assert image == f"aidp-lab:{commit_sha}"
    assert calls[0][:4] == ["docker", "image", "load", "--input"]


def test_updater_request_contract_has_no_target_or_command_input(tmp_path: Path) -> None:
    path = tmp_path / "request.json"
    operation_id = "d9282ff6-8717-4db7-9f59-241469a2c526"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "operation_id": operation_id,
                "action": "update",
                "target": "main",
                "command": "ignored",
            }
        ),
        encoding="utf-8",
    )
    assert updater._request(path) == operation_id
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "operation_id": operation_id,
                "action": "shell",
            }
        ),
        encoding="utf-8",
    )
    assert updater._request(path) is None


def test_vm_update_bridge_keeps_docker_privilege_on_the_host() -> None:
    cloud_init = (ROOT / "terraform/templatefile/user_data.sh").read_text(
        encoding="utf-8"
    )
    dockerfile = (ROOT / "docker/Dockerfile").read_text(encoding="utf-8")
    updater_source = (ROOT / "scripts/vm_release_updater.py").read_text(
        encoding="utf-8"
    )

    assert "VM_UPDATE_ENABLED=true" in cloud_init
    assert "PathChanged=/opt/aidp-lab/state/update/inbox/request.json" in cloud_init
    assert "ExecStart=/usr/local/sbin/aidp-lab-release-update" in cloud_init
    assert "NoNewPrivileges=true" in cloud_init
    assert "ProtectSystem=strict" in cloud_init
    assert "/var/run/docker.sock" not in cloud_init + dockerfile
    assert "releases/latest" in updater_source
    assert 'payload.get("immutable") is not True' in updater_source
    assert "candidate_health_failed" in updater_source
    assert "aidp-lab-rollback" in updater_source
    assert "APP_RELEASE_TAG=$APP_RELEASE_TAG" in dockerfile
    assert "--security-opt no-new-privileges:true" in cloud_init
    assert "--cap-drop ALL" in cloud_init
    assert "--read-only" in cloud_init
    assert "/var/lib/aidp-lab/update:ro,Z" in cloud_init
    assert "/var/lib/aidp-lab/update/inbox:rw,Z" in cloud_init
    assert '"docker", "image", "load", "--input"' in updater_source
    assert "_build_image" not in updater_source


def test_candidate_uses_an_isolated_state_snapshot(tmp_path: Path) -> None:
    state = tmp_path / "state"
    releases = tmp_path / "releases"
    (state / "update").mkdir(parents=True)
    releases.mkdir()
    (state / "settings.json").write_text('{"enabled":true}\n', encoding="utf-8")
    (state / "update/inbox").mkdir()
    (state / "update/inbox/request.json").write_text("queued\n", encoding="utf-8")

    snapshot = updater._snapshot_state(
        state, releases, "d9282ff6-8717-4db7-9f59-241469a2c526"
    )
    (snapshot / "settings.json").write_text("candidate\n", encoding="utf-8")

    assert (state / "settings.json").read_text(encoding="utf-8") == '{"enabled":true}\n'
    assert not (snapshot / "update/inbox/request.json").exists()
    assert (snapshot / "update/status").is_dir()


def test_candidate_mount_and_runtime_hardening_are_explicit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    observed: list[str] = []
    snapshot = tmp_path / "releases/candidate-state"
    monkeypatch.setattr(
        updater,
        "_run",
        lambda arguments, **_kwargs: observed.extend(arguments) or "",
    )

    updater._run_container(
        tmp_path,
        updater.CANDIDATE_NAME,
        "aidp-lab:" + "b" * 40,
        candidate=True,
        state_dir=snapshot,
    )

    assert f"{snapshot}:/var/lib/aidp-lab:Z" in observed
    assert f"{tmp_path / 'state'}:/var/lib/aidp-lab:Z" not in observed
    assert ["--security-opt", "no-new-privileges:true"] == observed[
        observed.index("--security-opt") : observed.index("--security-opt") + 2
    ]
    assert "--cap-drop" in observed and "ALL" in observed
    assert "--read-only" in observed
    assert "/var/run/docker.sock" not in " ".join(observed)


@pytest.mark.parametrize("status, expected", [("200", True), ("204", False), ("302", False)])
def test_health_requires_exact_http_200(
    monkeypatch: pytest.MonkeyPatch, status: str, expected: bool
) -> None:
    monkeypatch.setattr(
        updater.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, stdout=status),
    )
    monkeypatch.setattr(updater.time, "sleep", lambda _seconds: None)
    assert updater._healthy("https://127.0.0.1/api/health", attempts=1) is expected


def _fake_containers(
    monkeypatch: pytest.MonkeyPatch, initial: dict[str, dict[str, object]]
) -> tuple[dict[str, dict[str, object]], list[str]]:
    containers = {name: dict(value) for name, value in initial.items()}
    removed: list[str] = []

    monkeypatch.setattr(updater, "_container_exists", lambda name: name in containers)
    monkeypatch.setattr(
        updater, "_container_image", lambda name: str(containers[name]["image"])
    )
    monkeypatch.setattr(
        updater, "_container_running", lambda name: bool(containers[name]["running"])
    )

    def remove(name: str) -> None:
        if name in containers:
            removed.append(name)
            containers.pop(name)

    def run(arguments: list[str], **_kwargs: object) -> str:
        if arguments[1] == "rename":
            containers[arguments[3]] = containers.pop(arguments[2])
        elif arguments[1] == "start":
            containers[arguments[2]]["running"] = True
        elif arguments[1] == "stop":
            containers[arguments[-1]]["running"] = False
        return ""

    def run_container(
        _root: Path,
        name: str,
        image: str,
        *,
        candidate: bool,
        state_dir: Path | None = None,
    ) -> None:
        del candidate, state_dir
        containers[name] = {"image": image, "running": True}

    monkeypatch.setattr(updater, "_remove_container", remove)
    monkeypatch.setattr(updater, "_run", run)
    monkeypatch.setattr(updater, "_run_container", run_container)
    monkeypatch.setattr(updater, "_healthy", lambda *_args, **_kwargs: True)
    return containers, removed


def test_activation_recovers_after_the_current_container_was_renamed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    current_sha = "a" * 40
    target_image = "aidp-lab:" + "b" * 40
    containers, removed = _fake_containers(
        monkeypatch,
        {
            updater.ROLLBACK_NAME: {
                "image": f"aidp-lab:{current_sha}",
                "running": False,
            }
        },
    )

    updater._activate(tmp_path, target_image, current_sha)

    assert containers[updater.APP_NAME] == {"image": target_image, "running": True}
    assert containers[updater.ROLLBACK_NAME] == {
        "image": f"aidp-lab:{current_sha}",
        "running": False,
    }
    assert updater.ROLLBACK_NAME not in removed


def test_activation_resume_keeps_the_only_healthy_rollback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    current_sha = "a" * 40
    target_image = "aidp-lab:" + "b" * 40
    containers, removed = _fake_containers(
        monkeypatch,
        {
            updater.APP_NAME: {"image": target_image, "running": True},
            updater.ROLLBACK_NAME: {
                "image": f"aidp-lab:{current_sha}",
                "running": False,
            },
        },
    )

    updater._activate(tmp_path, target_image, current_sha)

    assert set(containers) == {updater.APP_NAME, updater.ROLLBACK_NAME}
    assert removed == []


def test_failure_before_activation_keeps_the_current_healthy_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_image = "aidp-lab:" + "b" * 40
    containers, removed = _fake_containers(
        monkeypatch,
        {
            updater.APP_NAME: {"image": current_image, "running": True},
            updater.ROLLBACK_NAME: {
                "image": "aidp-lab:" + "a" * 40,
                "running": False,
            },
        },
    )

    updater._restore_previous_container(current_image)

    assert containers[updater.APP_NAME]["image"] == current_image
    assert updater.ROLLBACK_NAME in containers
    assert removed == []


def test_reconcile_reports_when_no_healthy_rollback_can_be_verified(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    (state / "update/status").mkdir(parents=True)
    (tmp_path / "release.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "repository": updater.REPOSITORY.removesuffix(".git"),
                "release": "v2.2.0",
                "commit_sha": "a" * 40,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        updater, "latest_release", lambda: (_ for _ in ()).throw(RuntimeError("offline"))
    )
    monkeypatch.setattr(
        updater, "_container_exists", lambda name: name == updater.APP_NAME
    )
    monkeypatch.setattr(updater, "_container_running", lambda _name: True)
    monkeypatch.setattr(updater, "_healthy", lambda *_args, **_kwargs: False)

    with pytest.raises(RuntimeError, match="rollback_health_failed"):
        updater.reconcile(tmp_path, "d9282ff6-8717-4db7-9f59-241469a2c526")

    status = json.loads((state / "update/status/status.json").read_text(encoding="utf-8"))
    assert status["status"] == "failed"
    assert "no healthy rollback could be verified" in status["message"]
    assert "Manual intervention is required" in status["message"]
