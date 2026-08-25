from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx

from .config import Settings
from .lab_packs import lab_catalog


REPOSITORY = "https://github.com/jgangini/oci-ai-data-platform-workbench-starter-kits"
LATEST_RELEASE_API = (
    "https://api.github.com/repos/jgangini/"
    "oci-ai-data-platform-workbench-starter-kits/releases/latest"
)
SEMVER_TAG = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
SHA = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
RELEASE_ASSETS = ("aidp-release.json", "aidp-lab-image-amd64.tar.gz")
RUNNING_UPDATE_STATES = frozenset(
    {"queued", "checking", "downloading", "building", "validating", "activating"}
)
SUCCESS_UPDATE_STATES = frozenset({"succeeded", "up_to_date"})
UPDATE_STATES = RUNNING_UPDATE_STATES | SUCCESS_UPDATE_STATES | {"failed"}
logger = logging.getLogger(__name__)


class ReleaseUpdateUnavailable(RuntimeError):
    pass


class ReleaseUpdateConflict(RuntimeError):
    pass


def semantic_version(tag: str) -> tuple[int, int, int] | None:
    match = SEMVER_TAG.fullmatch(tag)
    return tuple(map(int, match.groups())) if match else None


def validated_latest_release(payload: Any) -> dict[str, str | bool]:
    if not isinstance(payload, dict):
        raise ReleaseUpdateUnavailable("GitHub returned an invalid release document")
    tag = str(payload.get("tag_name") or "")
    if semantic_version(tag) is None:
        raise ReleaseUpdateUnavailable("GitHub returned an invalid release tag")
    if payload.get("draft") is not False or payload.get("prerelease") is not False:
        raise ReleaseUpdateUnavailable("GitHub did not return a stable release")
    if payload.get("immutable") is not True:
        raise ReleaseUpdateUnavailable("The latest GitHub release is not immutable")
    raw_assets = payload.get("assets")
    if not isinstance(raw_assets, list):
        raise ReleaseUpdateUnavailable("The latest GitHub release has no update artifacts")
    assets = {
        str(item.get("name") or ""): item
        for item in raw_assets
        if isinstance(item, dict)
    }
    for name in RELEASE_ASSETS:
        asset = assets.get(name)
        expected_url = f"{REPOSITORY}/releases/download/{tag}/{name}"
        if (
            sum(
                1
                for item in raw_assets
                if isinstance(item, dict) and item.get("name") == name
            )
            != 1
            or not isinstance(asset, dict)
            or asset.get("browser_download_url") != expected_url
            or SHA256.fullmatch(str(asset.get("digest") or "")) is None
        ):
            raise ReleaseUpdateUnavailable(
                "The latest GitHub release has an invalid update artifact"
            )
    published_at = str(payload.get("published_at") or "")[:64]
    return {
        "tag": tag,
        "immutable": True,
        "published_at": published_at,
        "url": f"{REPOSITORY}/releases/tag/{tag}",
    }


def _safe_text(value: Any, maximum: int = 240) -> str:
    return " ".join(str(value or "").split())[:maximum]


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _operation(raw: dict[str, Any]) -> dict[str, Any] | None:
    try:
        operation_id = str(UUID(str(raw.get("operation_id") or "")))
    except ValueError:
        return None
    state = str(raw.get("status") or "")
    if raw.get("schema_version") != 1 or state not in UPDATE_STATES:
        return None
    result: dict[str, Any] = {
        "operation_id": operation_id,
        "status": state,
        "phase": state,
        "message": _safe_text(raw.get("message")),
    }
    for key in ("current_release", "target_release"):
        value = str(raw.get(key) or "")
        if semantic_version(value) is not None:
            result[key] = value
    for key in ("current_commit_sha", "target_commit_sha"):
        value = str(raw.get(key) or "")
        if SHA.fullmatch(value):
            result[key] = value
    return result


class ApplicationReleaseManager:
    """Read release metadata and enqueue one fixed, host-owned VM update action."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.update_dir = Path(settings.application_update_dir)
        self._latest: dict[str, str | bool] | None = None
        self._latest_expires_at = 0.0
        self._latest_lock = asyncio.Lock()
        self._request_lock = threading.Lock()

    async def _fetch_latest(self) -> Any:
        async with httpx.AsyncClient(
            timeout=10,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "oci-aidp-starter-kits",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        ) as client:
            response = await client.get(LATEST_RELEASE_API)
            response.raise_for_status()
            return response.json()

    async def latest_release(self) -> dict[str, str | bool]:
        now = time.monotonic()
        if self._latest is not None and now < self._latest_expires_at:
            return dict(self._latest)
        async with self._latest_lock:
            now = time.monotonic()
            if self._latest is None or now >= self._latest_expires_at:
                self._latest = validated_latest_release(await self._fetch_latest())
                self._latest_expires_at = now + 300
            return dict(self._latest)

    def current_operation(self) -> dict[str, Any] | None:
        status = _operation(_read_json(self.update_dir / "status/status.json"))
        request = _read_json(self.update_dir / "inbox/request.json")
        try:
            operation_id = str(UUID(str(request.get("operation_id") or "")))
        except ValueError:
            operation_id = ""
        if (
            operation_id
            and request.get("schema_version") == 1
            and request.get("action") == "update"
            and (not status or status["operation_id"] != operation_id)
        ):
            return {
                "operation_id": operation_id,
                "status": "queued",
                "phase": "queued",
                "message": "The VM accepted the update request.",
            }
        return status

    async def snapshot(self) -> dict[str, Any]:
        latest: dict[str, str | bool] | None = None
        check_error = ""
        try:
            latest = await self.latest_release()
        except (httpx.HTTPError, ReleaseUpdateUnavailable, ValueError, TypeError) as exc:
            logger.warning("GitHub release check failed (%s)", type(exc).__name__)
            check_error = "Unable to verify the latest immutable GitHub release."
        current = semantic_version(self.settings.application_release)
        latest_version = semantic_version(str(latest["tag"])) if latest else None
        return {
            "repository": REPOSITORY,
            "current_release": self.settings.application_release,
            "current_commit_sha": self.settings.application_commit_sha,
            "latest_release": latest["tag"] if latest else None,
            "latest_published_at": latest["published_at"] if latest else None,
            "latest_release_url": latest["url"] if latest else None,
            "latest_release_immutable": bool(latest and latest["immutable"]),
            "update_available": bool(current and latest_version and latest_version > current),
            "updater_available": self.settings.vm_update_enabled,
            "update_check_error": check_error,
            "operation": self.current_operation(),
            "packages": [
                {
                    "package_id": pack.lab_id,
                    "display_name": pack.display_name,
                    "bundled_version": pack.pack_version,
                    "kind": pack.kind,
                    "scope": pack.scope,
                    "status": pack.status,
                }
                for pack in lab_catalog()
            ],
        }

    @staticmethod
    def _poll_payload(operation: dict[str, Any]) -> dict[str, Any]:
        state = operation["status"]
        return {
            "status": (
                "pending"
                if state in RUNNING_UPDATE_STATES
                else "active"
                if state in SUCCESS_UPDATE_STATES
                else "error"
            ),
            "phase": operation.get("phase") or state,
            "operation_id": operation["operation_id"],
            "message": operation.get("message") or (
                "Application update completed."
                if state in SUCCESS_UPDATE_STATES
                else "Application update failed."
            ),
        }

    def request_update(self, operation_id: str) -> dict[str, Any]:
        if not self.settings.vm_update_enabled:
            raise ReleaseUpdateUnavailable("GitHub updates are available only on the deployed VM")
        operation_id = str(UUID(operation_id))
        with self._request_lock:
            current = self.current_operation()
            if current and current["operation_id"] == operation_id:
                return self._poll_payload(current)
            if current and current["status"] in RUNNING_UPDATE_STATES:
                raise ReleaseUpdateConflict("Another application update is already running")
            request = {
                "schema_version": 1,
                "operation_id": operation_id,
                "action": "update",
                "requested_at": datetime.now(timezone.utc).isoformat(),
            }
            inbox = self.update_dir / "inbox"
            inbox.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = inbox / f".request-{operation_id}.tmp"
            with temporary.open("x", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(request, sort_keys=True, separators=(",", ":")) + "\n"
                )
            try:
                temporary.chmod(0o600)
            except OSError:
                pass
            os.replace(temporary, inbox / "request.json")
        return {
            "status": "pending",
            "phase": "queued",
            "operation_id": operation_id,
            "message": "The VM accepted the update request.",
        }
