from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest

from app.config import Settings
from app.releases import (
    ApplicationReleaseManager,
    ReleaseUpdateConflict,
    ReleaseUpdateUnavailable,
    semantic_version,
    validated_latest_release,
)


def _settings(tmp_path: Path, *, enabled: bool = True) -> Settings:
    return Settings(
        application_release="v2.2.0",
        application_commit_sha="a" * 40,
        application_update_dir=str(tmp_path / "update"),
        vm_update_enabled=enabled,
    )


def _latest(tag: str = "v2.3.0") -> dict[str, object]:
    return {
        "tag_name": tag,
        "draft": False,
        "prerelease": False,
        "immutable": True,
        "published_at": "2026-08-24T12:00:00Z",
        "assets": [
            {
                "name": name,
                "browser_download_url": (
                    "https://github.com/jgangini/"
                    f"oci-ai-data-platform-workbench-starter-kits/releases/download/{tag}/{name}"
                ),
                "digest": f"sha256:{character * 64}",
            }
            for name, character in (
                ("aidp-release.json", "a"),
                ("aidp-lab-image-amd64.tar.gz", "b"),
            )
        ],
    }


def test_release_contract_requires_stable_immutable_semver() -> None:
    assert semantic_version("v2.10.3") == (2, 10, 3)
    assert semantic_version("v2.10.3-rc.1") is None
    assert validated_latest_release(_latest())["tag"] == "v2.3.0"
    for update in (
        {"immutable": False},
        {"draft": True},
        {"prerelease": True},
        {"tag_name": "main"},
        {"assets": []},
    ):
        with pytest.raises(ReleaseUpdateUnavailable):
            validated_latest_release({**_latest(), **update})


def test_release_snapshot_uses_image_metadata_and_pack_manifests(tmp_path: Path) -> None:
    manager = ApplicationReleaseManager(_settings(tmp_path))

    async def latest() -> dict[str, object]:
        return _latest()

    manager._fetch_latest = latest  # type: ignore[method-assign]
    snapshot = asyncio.run(manager.snapshot())

    assert snapshot["current_release"] == "v2.2.0"
    assert snapshot["current_commit_sha"] == "a" * 40
    assert snapshot["latest_release"] == "v2.3.0"
    assert snapshot["latest_release_immutable"] is True
    assert snapshot["update_available"] is True
    assert {(item["package_id"], item["bundled_version"]) for item in snapshot["packages"]} == {
        ("banking", "2.0.0"),
        ("telecommunications", "2.0.0"),
        ("telco_lineage", "2.0.0"),
        ("retail", "2.0.0"),
        ("healthcare", "2.0.0"),
        ("ai_data_governance_vsc_extension", "3.0.0"),
    }


def test_update_request_is_uuid_idempotent_and_singleton(tmp_path: Path) -> None:
    manager = ApplicationReleaseManager(_settings(tmp_path))
    first = str(uuid4())
    second = str(uuid4())

    assert manager.request_update(first)["status"] == "pending"
    request = json.loads((tmp_path / "update/inbox/request.json").read_text(encoding="utf-8"))
    assert request == {
        "action": "update",
        "operation_id": first,
        "requested_at": request["requested_at"],
        "schema_version": 1,
    }

    (tmp_path / "update/status").mkdir()
    (tmp_path / "update/status/status.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "operation_id": first,
                "status": "building",
                "message": "Building candidate",
            }
        ),
        encoding="utf-8",
    )
    assert manager.request_update(first)["phase"] == "building"
    with pytest.raises(ReleaseUpdateConflict):
        manager.request_update(second)

    (tmp_path / "update/status/status.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "operation_id": first,
                "status": "succeeded",
                "message": "Updated",
            }
        ),
        encoding="utf-8",
    )
    assert manager.request_update(first)["status"] == "active"
    assert manager.request_update(second)["status"] == "pending"
    assert manager.current_operation()["operation_id"] == second


def test_update_request_is_disabled_outside_the_vm(tmp_path: Path) -> None:
    manager = ApplicationReleaseManager(_settings(tmp_path, enabled=False))
    with pytest.raises(ReleaseUpdateUnavailable, match="deployed VM"):
        manager.request_update(str(uuid4()))
