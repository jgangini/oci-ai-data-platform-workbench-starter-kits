"""Idempotent AIDP provisioning for isolated lab participants."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from oci._vendor import requests

from .config import Settings
from .governance import (
    GOVERNANCE_AGENT_COMPUTE_NAME,
    GOVERNANCE_AGENT_NAME,
    GOVERNANCE_BUCKET_NAME,
    GOVERNANCE_CREDENTIAL_NAME,
    GOVERNANCE_DISPLAY_NAME,
    GOVERNANCE_JOB_NAME,
    GOVERNANCE_MODULE_ID,
    GOVERNANCE_TABLES,
    agent_source,
    governance_sync_notebook,
)
from .lab_packs import LabAsset, LabPack, available_lab_ids, load_lab_pack
from .notebooks import (
    LAYER_PREFIXES,
    WORKSPACE_ROOT,
    participant_folder,
    participant_key,
    schema_name,
    table_name,
    workspace_root,
)


API_VERSION = "20260430"
SHARED_COMPUTE_NAME = "aidp_cluster_shared_compute"
AGENT_COMPUTE_NAME = GOVERNANCE_AGENT_COMPUTE_NAME
CATALOG_NAME = "oci_medallion"
LEGACY_CATALOG_NAME = "aidp_lab"
LAYOUT_VERSION = 5
CONTROL_ROOT = f"{WORKSPACE_ROOT}/.control"
MODULE_CONTROL_ROOT = f"{CONTROL_ROOT}/modules"
MODULE_ROOT = f"{MODULE_CONTROL_ROOT}/{GOVERNANCE_MODULE_ID}"
MODULE_MANIFEST_PATH = f"{MODULE_ROOT}/manifest.json"
LEGACY_MEDALLION_ROOT = "/Workspace/medallon"
LEGACY_WORKSPACE_ROOT = "/Workspace/lab-users"
LEGACY_LAB_IDS = frozenset({"banking", "telecommunications", "retail", "healthcare"})


def participant_catalog_name(key: str) -> str:
    if re.fullmatch(r"u[1-9][0-9]*", key) is None or int(key[1:]) < 101:
        raise ValueError("A participant key starting at u101 is required")
    return CATALOG_NAME


def catalog_name_for(key: str) -> str:
    return participant_catalog_name(key) if re.fullmatch(r"u[1-9][0-9]*", key) else LEGACY_CATALOG_NAME


class AidpProvisionPending(Exception):
    def __init__(self, message: str, phase: str = "content") -> None:
        super().__init__(message)
        self.phase = phase


class AidpProvisionError(Exception):
    pass


class AidpProvisionConflict(Exception):
    pass


@dataclass(frozen=True, slots=True)
class UserMaterial:
    email: str
    lab_id: str
    participant_key: str
    workspace_path: str
    job_name: str
    pack_version: str = "1.0.0"
    phase: str = "active"
    participant_code: int | None = None


def _module_payload(manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    state = manifest or {}
    status_value = str(state.get("status") or "not_installed")
    operation = state.get("operation") if isinstance(state.get("operation"), dict) else {}
    return {
        "module_id": GOVERNANCE_MODULE_ID,
        "display_name": GOVERNANCE_DISPLAY_NAME,
        "status": status_value,
        "installed": status_value != "not_installed",
        "operation_id": str(operation.get("operation_id") or "") or None,
        "operation_type": str(operation.get("type") or "") or None,
        "phase": str(state.get("phase") or "not_installed"),
        "enabled": bool(state.get("enabled", False)),
    }


def _validated_lab_ids(lab_ids: str | list[str] | tuple[str, ...]) -> tuple[str, ...]:
    values = (lab_ids,) if isinstance(lab_ids, str) else tuple(lab_ids)
    if not values or len(values) != len(set(values)):
        raise ValueError("Choose at least one available lab without duplicates")
    supported = set(available_lab_ids())
    if any(lab_id not in supported for lab_id in values):
        raise ValueError("Choose only available labs")
    return values


def participant_owner_key(user_ocid: str) -> str:
    """Derive the stable control-manifest owner from an OCI user OCID."""
    if not user_ocid.startswith("ocid1.user.") or any(character.isspace() for character in user_ocid):
        raise ValueError("A valid OCI user OCID is required")
    return f"u_{hashlib.sha256(user_ocid.encode('utf-8')).hexdigest()[:16]}"


class LocalAidpClient:
    """In-memory AIDP adapter for the Docker development and test profile."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.users: dict[str, dict[str, UserMaterial]] = {}
        self._operations: dict[tuple[str, str], tuple[str, UserMaterial]] = {}
        # ponytail: process-local locks are sufficient for the single-process development adapter.
        self._locks: dict[str, asyncio.Lock] = {}
        self._module_lock = asyncio.Lock()
        self._module: dict[str, Any] | None = None
        self._platform_admin_ocids: set[str] = set()

    async def close(self) -> None:
        return None

    async def healthcheck(self) -> None:
        return None

    async def is_platform_admin(self, user_ocid: str) -> bool:
        return user_ocid in self._platform_admin_ocids

    async def platform_admin_user_ocids(self) -> set[str]:
        return set(self._platform_admin_ocids)

    async def platform_admin_principals(self) -> tuple[set[str], set[str]]:
        return set(self._platform_admin_ocids), set()

    def grant_platform_admin(self, user_ocid: str) -> None:
        if not user_ocid.startswith("ocid1.user."):
            raise ValueError("A valid OCI user OCID is required")
        self._platform_admin_ocids.add(user_ocid)

    async def list_modules(self) -> list[dict[str, Any]]:
        if self.settings.deployment_mode != "production":
            return []
        return [_module_payload(self._module)]

    async def install_governance_module(
        self,
        user_ocid: str,
        operation_id: str,
        *,
        role_membership_verified: bool = False,
    ) -> dict[str, Any]:
        if self.settings.deployment_mode != "production":
            raise AidpProvisionConflict("Governance modules are available only in production mode")
        if not role_membership_verified and not await self.is_platform_admin(user_ocid):
            raise AidpProvisionConflict("The selected user is not an AI_DATA_PLATFORM_ADMIN")
        async with self._module_lock:
            if self._module is None:
                self._module = {
                    "schema_version": 1,
                    "module_id": GOVERNANCE_MODULE_ID,
                    "status": "active",
                    "phase": "active",
                    "enabled": True,
                    "operation": {"operation_id": operation_id, "type": "install", "phase": "complete"},
                }
            return _module_payload(self._module)

    async def redeploy_governance_module(
        self,
        user_ocid: str,
        operation_id: str,
        *,
        role_membership_verified: bool = False,
    ) -> dict[str, Any]:
        if not role_membership_verified and not await self.is_platform_admin(user_ocid):
            raise AidpProvisionConflict("The selected user is not an AI_DATA_PLATFORM_ADMIN")
        async with self._module_lock:
            if self._module is None:
                raise AidpProvisionConflict("The governance module is not installed")
            enabled = bool(self._module.get("enabled"))
            self._module.update(
                status="active",
                phase="active",
                enabled=enabled,
                operation={"operation_id": operation_id, "type": "redeploy", "phase": "complete"},
            )
            return _module_payload(self._module)

    async def delete_governance_module(
        self,
        user_ocid: str,
        operation_id: str,
        *,
        role_membership_verified: bool = False,
    ) -> dict[str, Any]:
        if not role_membership_verified and not await self.is_platform_admin(user_ocid):
            raise AidpProvisionConflict("The selected user is not an AI_DATA_PLATFORM_ADMIN")
        async with self._module_lock:
            self._module = None
            result = _module_payload()
            result.update(operation_id=operation_id, operation_type="delete", phase="complete")
            return result

    @staticmethod
    def _material(user_ocid: str, email: str, lab_id: str, participant_code: int) -> UserMaterial:
        pack = load_lab_pack(lab_id)
        key = participant_key(participant_code)
        return UserMaterial(
            email,
            lab_id,
            key,
            workspace_root(key, lab_id, email),
            f"wf_{key}_{lab_id}",
            pack.pack_version,
            participant_code=participant_code,
        )

    async def provision_user(
        self, user_ocid: str, email: str, lab_ids: str | list[str], participant_code: int
    ) -> UserMaterial | tuple[UserMaterial, ...]:
        requested = _validated_lab_ids(lab_ids)
        owner_key = participant_owner_key(user_ocid)
        async with self._locks.setdefault(owner_key, asyncio.Lock()):
            labs = self.users.setdefault(owner_key, {})
            for lab_id in requested:
                labs.setdefault(lab_id, self._material(user_ocid, email, lab_id, participant_code))
            result = tuple(labs[lab_id] for lab_id in requested)
            return result[0] if isinstance(lab_ids, str) else result

    async def add_lab(self, user_ocid: str, email: str, lab_id: str) -> UserMaterial:
        owner_key = participant_owner_key(user_ocid)
        assigned = next(iter(self.users.get(owner_key, {}).values()), None)
        if assigned is None or assigned.participant_code is None:
            raise AidpProvisionConflict("This participant has no assigned laboratory")
        return await self.provision_user(user_ocid, email, lab_id, assigned.participant_code)

    async def redeploy_lab(
        self, user_ocid: str, email: str, lab_id: str, operation_id: str
    ) -> UserMaterial:
        _validated_lab_ids(lab_id)
        owner_key = participant_owner_key(user_ocid)
        async with self._locks.setdefault(owner_key, asyncio.Lock()):
            if lab_id not in self.users.get(owner_key, {}):
                raise AidpProvisionConflict("This lab is not assigned to the participant")
            operation_key = (owner_key, lab_id)
            completed = self._operations.get(operation_key)
            if completed is not None and completed[0] == operation_id:
                return completed[1]
            assigned = self.users[owner_key][lab_id]
            material = self._material(user_ocid, email, lab_id, assigned.participant_code or 0)
            self.users[owner_key][lab_id] = material
            self._operations[operation_key] = (operation_id, material)
            return material

    async def delete_lab(
        self, user_ocid: str, lab_id: str, operation_id: str
    ) -> None:
        owner_key = participant_owner_key(user_ocid)
        async with self._locks.setdefault(owner_key, asyncio.Lock()):
            labs = self.users.get(owner_key, {})
            if lab_id not in labs:
                return
            if len(labs) == 1:
                raise AidpProvisionConflict(
                    "The last lab cannot be removed; delete the participant instead"
                )
            labs.pop(lab_id)

    async def list_user_labs(
        self, user_ocids: list[str]
    ) -> dict[str, list[UserMaterial]]:
        return {
            user_ocid: list(self.users.get(participant_owner_key(user_ocid), {}).values())
            for user_ocid in user_ocids
            if self.users.get(participant_owner_key(user_ocid))
        }

    async def cleanup_user(self, user_ocid: str) -> None:
        owner_key = participant_owner_key(user_ocid)
        async with self._locks.setdefault(owner_key, asyncio.Lock()):
            self.users.pop(owner_key, None)
            for operation_key in [item for item in self._operations if item[0] == owner_key]:
                self._operations.pop(operation_key, None)

class AidpClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base = (
            f"https://datalake.{settings.aidp_region}.oci.oraclecloud.com/{API_VERSION}/"
            f"aiDataPlatforms/{settings.aidp_platform_id}"
        )
        import oci

        self._oci = oci
        config = oci.config.from_file(settings.oci_config_file, "DEFAULT")
        self._oci_config = config
        self.signer = oci.signer.Signer(
            tenancy=config["tenancy"],
            user=config["user"],
            fingerprint=config["fingerprint"],
            private_key_file_location=config["key_file"],
            pass_phrase=config.get("pass_phrase"),
        )
        self.object_storage = oci.object_storage.ObjectStorageClient(config)
        self.session = requests.Session()
        self._session_lock = threading.Lock()
        # ponytail: process-local locks serialize one participant; use a distributed lock if the API is replicated.
        self._locks: dict[str, asyncio.Lock] = {}

    async def close(self) -> None:
        self.session.close()

    @staticmethod
    def _request_headers(
        method: str,
        path: str,
        payload: dict[str, Any] | None,
        data: bytes | None,
        headers: dict[str, str] | None,
        retry_scope: str,
    ) -> dict[str, str]:
        request_headers = {"Accept": "application/json", **(headers or {})}
        if payload is not None:
            request_headers["Content-Type"] = "application/json"
        if method.upper() != "POST":
            return request_headers
        content = (
            data
            if data is not None
            else json.dumps(payload or {}, sort_keys=True).encode("utf-8")
        )
        identity_headers = {
            key.casefold(): str(value)
            for key, value in request_headers.items()
            if key.casefold() != "opc-retry-token"
        }
        retry_identity = json.dumps(
            {
                "method": method.upper(),
                "path": path,
                "scope": retry_scope,
                "headers": identity_headers,
                "content_sha256": hashlib.sha256(content).hexdigest(),
            },
            sort_keys=True,
        )
        request_headers["opc-retry-token"] = str(
            uuid.uuid5(uuid.NAMESPACE_URL, retry_identity)
        )
        return request_headers

    @staticmethod
    def _response_body(response: Any) -> Any:
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return response.content

    @staticmethod
    def _safe_diagnostic_fragment(value: Any, *, limit: int = 240) -> str:
        if not isinstance(value, str):
            return ""
        fragment = " ".join(value.split())
        fragment = re.sub(r"https?://\S+", "[url]", fragment, flags=re.IGNORECASE)
        fragment = re.sub(r"\bocid1\.[A-Za-z0-9._-]+", "[ocid]", fragment)
        fragment = re.sub(
            r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
            "[email]",
            fragment,
            flags=re.IGNORECASE,
        )
        fragment = re.sub(
            r"(?i)\b(authorization|credential|password|secret|token)\s*[:=]\s*\S+",
            r"\1=[redacted]",
            fragment,
        )
        return fragment[:limit]

    @classmethod
    def _request_error_message(
        cls,
        method: str,
        path: str,
        phase: str,
        request_headers: dict[str, str],
        response: Any,
    ) -> str:
        endpoint = re.sub(r"(/workspaces/)[^/]+", r"\1{workspace}", path)
        endpoint = cls._safe_diagnostic_fragment(endpoint, limit=180)
        target = cls._safe_diagnostic_fragment(request_headers.get("path"), limit=180)
        body = cls._response_body(response)
        code = cls._safe_diagnostic_fragment(
            body.get("code") if isinstance(body, dict) else None, limit=80
        )
        message = cls._safe_diagnostic_fragment(
            body.get("message") if isinstance(body, dict) else None
        )
        context = f"{method.upper()} {endpoint} during {phase}"
        if target:
            context += f" for {target}"
        oracle_detail = ""
        if code or message:
            oracle_detail = f" Oracle {code or 'error'}: {message or 'No message provided.'}"
        return (
            f"AIDP rejected {context} ({response.status_code}).{oracle_detail} "
            "Check the AIDP policy and request contract, then retry."
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        allow_not_found: bool = False,
        include_headers: bool = False,
        phase: str = "content",
        retry_scope: str = "",
    ) -> Any:
        request_headers = self._request_headers(
            method, path, payload, data, headers, retry_scope
        )
        try:
            with self._session_lock:
                response = self.session.request(
                    method,
                    f"{self.base}{path}",
                    auth=self.signer,
                    json=payload,
                    data=data,
                    headers=request_headers,
                    params=params,
                    timeout=(10, 60),
                )
        except requests.exceptions.RequestException as exc:
            raise AidpProvisionPending(
                "AIDP is still accepting the requested material. Retry shortly.", phase
            ) from exc
        if response.status_code in {408, 409, 429, 500, 502, 503, 504}:
            raise AidpProvisionPending("AIDP is still reconciling the requested material. Retry shortly.", phase)
        if response.status_code == 404 and allow_not_found:
            return (None, response.headers) if include_headers else None
        if response.status_code >= 400:
            raise AidpProvisionError(
                self._request_error_message(
                    method, path, phase, request_headers, response
                )
            )
        result = self._response_body(response)
        return (result, response.headers) if include_headers else result

    @staticmethod
    def _page_items(body: Any) -> list[dict[str, Any]]:
        if isinstance(body, list):
            values = body
        elif isinstance(body, dict) and ("items" in body or "Items" in body):
            values = body.get("items") if "items" in body else body.get("Items")
        else:
            raise AidpProvisionError("AIDP returned an invalid paginated response.")
        if not isinstance(values, list) or not all(isinstance(item, dict) for item in values):
            raise AidpProvisionError("AIDP returned invalid list items.")
        return values

    def _list(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
        phase: str = "content",
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page: str | None = None
        seen_pages: set[str] = set()
        for _ in range(1000):
            query = {"limit": "100", **(params or {})}
            if page:
                query["page"] = page
            body, response_headers = self._request(
                "GET", path, params=query, include_headers=True, phase=phase
            )
            items.extend(self._page_items(body))
            page = response_headers.get("opc-next-page") or response_headers.get("Opc-Next-Page")
            if not page:
                return items
            if page in seen_pages:
                raise AidpProvisionError("AIDP returned a repeated pagination token.")
            seen_pages.add(page)
        raise AidpProvisionError("AIDP pagination exceeded the safety limit.")

    def _workspace(self) -> dict[str, Any]:
        workspaces = [
            item
            for item in self._list("/workspaces", phase="workspace")
            if item.get("displayName") == self.settings.aidp_workspace_name
        ]
        if len(workspaces) != 1:
            raise AidpProvisionPending("The default AIDP workspace is not ready yet. Retry shortly.", "workspace")
        return self._require_operational_state(
            workspaces[0], {"ACTIVE"}, "workspace", "workspace"
        )

    def _catalog(
        self,
        name: str,
        *,
        allow_missing: bool = False,
        allow_deleting: bool = False,
    ) -> dict[str, Any] | None:
        catalogs = [
            item
            for item in self._list("/catalogs", phase="schemas")
            if (item.get("displayName") or item.get("name")) == name
        ]
        if not catalogs and allow_missing:
            return None
        if len(catalogs) != 1:
            raise AidpProvisionPending(f"The {name} catalog is not ready yet. Retry shortly.", "schemas")
        catalog = catalogs[0]
        state = str(catalog.get("lifecycleState") or catalog.get("state") or "").upper()
        if allow_deleting and state == "DELETING":
            return catalog
        return self._require_operational_state(catalog, {"ACTIVE"}, "catalog", "schemas")

    def _ensure_catalog(self, name: str) -> tuple[dict[str, Any], bool]:
        current = self._catalog(name, allow_missing=True)
        if current is not None:
            return current, False
        self._request(
            "POST",
            "/catalogs",
            payload={
                "displayName": name,
                "description": f"Private participant medallion catalog {name}",
                "catalogType": "INTERNAL",
            },
            phase="schemas",
        )
        published = self._catalog(name, allow_missing=True)
        if published is None:
            raise AidpProvisionPending(f"AIDP has not published catalog {name} yet.", "schemas")
        return published, True

    def _shared_compute(self, workspace_key: str) -> dict[str, Any]:
        clusters = [
            item
            for item in self._list(f"/workspaces/{workspace_key}/clusters", phase="workspace")
            if item.get("displayName") == SHARED_COMPUTE_NAME
        ]
        if len(clusters) != 1:
            raise AidpProvisionPending("The shared AIDP compute is not ready yet. Retry shortly.", "workspace")
        # AIDP auto-starts an idle-timeout STOPPED cluster when a notebook or workflow uses it.
        return self._require_operational_state(
            clusters[0], {"ACTIVE", "STOPPED"}, "compute", "workspace"
        )

    def _agent_compute_async_operations(
        self, workspace_key: str
    ) -> list[tuple[dict[str, Any], str]]:
        operations: list[tuple[dict[str, Any], str]] = []
        workspace_prefix = f"{workspace_key}."
        for item in self._list(
            "/asyncOperations",
            params={"resourceType": "AI_COMPUTE"},
            phase="workspace",
        ):
            action = str(item.get("actionType") or "").upper()
            resource_name = str(item.get("resourceName") or "")
            if (
                item.get("resourceDisplayName") != AGENT_COMPUTE_NAME
                or action not in {"CREATE_CLUSTER", "DELETE_CLUSTER"}
                or not resource_name.startswith(workspace_prefix)
                or not resource_name.removeprefix(workspace_prefix)
            ):
                continue
            operations.append((item, resource_name.removeprefix(workspace_prefix)))
        operations.sort(
            key=lambda entry: str(entry[0].get("timeStarted") or ""), reverse=True
        )
        return operations

    @staticmethod
    def _async_operation_status(operation: dict[str, Any]) -> str:
        return str(
            operation.get("status")
            or operation.get("lifecycleState")
            or operation.get("state")
            or ""
        ).upper()

    @staticmethod
    def _operation_retry_scope(operation: dict[str, Any], message: str) -> str:
        retry_scope = str(operation.get("key") or "")
        if not retry_scope:
            raise AidpProvisionError(message)
        return retry_scope

    def _deleted_agent_compute_retry_scope(
        self, operation: dict[str, Any], operation_status: str
    ) -> str:
        if operation_status in {"SUCCESS", "SUCCEEDED"}:
            return self._operation_retry_scope(
                operation,
                "The completed governance AI compute deletion has no operation identifier.",
            )
        if operation_status in {"FAILED", "ERROR", "CANCELED", "CANCELLED"}:
            raise AidpProvisionError(
                "The dedicated governance AI compute deletion failed closed."
            )
        raise AidpProvisionPending(
            "AIDP is still deleting the dedicated governance AI compute.",
            "workspace",
        )

    def _hidden_agent_compute(
        self, workspace_key: str, resource_key: str
    ) -> dict[str, Any] | None:
        candidate = self._request(
            "GET",
            f"/workspaces/{workspace_key}/clusters/{resource_key}",
            allow_not_found=True,
            phase="workspace",
        )
        if not isinstance(candidate, dict):
            return None
        if self._resource_name(candidate) != AGENT_COMPUTE_NAME:
            return None
        resource_type = str(
            candidate.get("type") or candidate.get("sourceApi") or ""
        ).upper()
        return candidate if resource_type == "AI_COMPUTE" else None

    def _recover_hidden_agent_compute(
        self, workspace_key: str
    ) -> tuple[dict[str, Any] | None, str]:
        operations = self._agent_compute_async_operations(workspace_key)
        if not operations:
            return None, ""
        operation, resource_key = operations[0]
        operation_status = self._async_operation_status(operation)
        if str(operation.get("actionType") or "").upper() == "DELETE_CLUSTER":
            return None, self._deleted_agent_compute_retry_scope(
                operation, operation_status
            )
        retry_scope = str(operation.get("key") or "")
        candidate = self._hidden_agent_compute(workspace_key, resource_key)
        if candidate is not None:
            return {
                **candidate,
                "_async_operation_action": operation.get("actionType"),
                "_async_operation_status": operation_status,
            }, retry_scope
        if operation_status in {"FAILED", "ERROR", "CANCELED", "CANCELLED"}:
            return None, self._operation_retry_scope(
                operation,
                "The failed governance AI compute creation has no operation identifier.",
            )
        raise AidpProvisionPending(
            "AIDP is still creating the dedicated governance AI compute.",
            "workspace",
        )

    def _remove_failed_agent_compute(
        self, workspace_key: str, compute: dict[str, Any] | None
    ) -> None:
        if not compute:
            return
        resource_state = str(
            compute.get("lifecycleState") or compute.get("state") or ""
        ).upper()
        failed_create = (
            str(compute.get("_async_operation_action") or "").upper() == "CREATE_CLUSTER"
            and str(compute.get("_async_operation_status") or "").upper()
            in {"FAILED", "ERROR", "CANCELED", "CANCELLED"}
        )
        failed_resource = resource_state == "FAILED" or resource_state.endswith(
            "_FAILED"
        )
        if (not failed_create and not failed_resource) or resource_state in {
            "ACTIVE",
            "STOPPED",
        }:
            return
        cluster_key = str(compute.get("key") or compute.get("id") or "")
        if not cluster_key:
            raise AidpProvisionError(
                "The failed AIDP AI compute has no identifier for safe recovery."
            )
        self._request(
            "DELETE",
            f"/workspaces/{workspace_key}/clusters/{cluster_key}",
            allow_not_found=True,
            phase="workspace",
        )
        raise AidpProvisionPending(
            "The failed AIDP AI compute is being removed before a safe retry.",
            "workspace",
        )

    def _ensure_agent_compute(self, workspace_key: str) -> tuple[dict[str, Any], bool]:
        retry_scope = ""

        def matches() -> list[dict[str, Any]]:
            return [
                item
                for item in self._list(
                    f"/workspaces/{workspace_key}/clusters", phase="workspace"
                )
                if self._resource_name(item) == AGENT_COMPUTE_NAME
                and str(item.get("type") or item.get("sourceApi") or "").upper()
                in {"AI_COMPUTE"}
            ]

        current = matches()
        if not current:
            hidden, retry_scope = self._recover_hidden_agent_compute(workspace_key)
            if hidden:
                current = [hidden]
        if len(current) > 1:
            raise AidpProvisionError(f"AIDP has duplicate AI compute named {AGENT_COMPUTE_NAME}.")
        self._remove_failed_agent_compute(workspace_key, current[0] if current else None)
        if not current:
            self._request(
                "POST",
                f"/workspaces/{workspace_key}/clusters",
                payload={
                    "type": "AI_COMPUTE",
                    "displayName": AGENT_COMPUTE_NAME,
                    "description": "Dedicated AI compute for the global data governance Agent",
                    "driverConfig": {"driverShapeConfig": {"ocpus": 1, "memoryInGBs": 16}},
                    "replicaConfig": {"minReplica": 1, "maxReplica": 1},
                },
                phase="workspace",
                retry_scope=f"agent-compute:{retry_scope}",
            )
            current = matches()
            if len(current) != 1:
                raise AidpProvisionPending(
                    "AIDP has not published the dedicated governance AI compute yet.", "workspace"
                )
            created = True
        else:
            created = False
        return (
            self._require_operational_state(
                current[0], {"ACTIVE", "STOPPED"}, "AI compute", "workspace"
            ),
            created,
        )

    def _agents(self, workspace_key: str, name: str) -> list[dict[str, Any]]:
        return [
            item
            for item in self._list(
                f"/workspaces/{workspace_key}/agents", phase="content"
            )
            if self._resource_name(item) == name
        ]

    def _ensure_agent(
        self,
        workspace_key: str,
        compute_key: str,
        name: str,
        root: str,
        source: bytes,
        descriptor: bytes,
        *,
        repair_drift: bool,
    ) -> tuple[str, bool]:
        session_config = {"variables": {}}
        entry_path = f"{root}/governance_agent.py"
        dependencies_path = f"{root}/requirements.txt"
        descriptor_path = f"{root}/agent-manifest.json"
        changed = self._upload_file(
            workspace_key, entry_path, source, repair_drift=repair_drift
        )
        changed = self._upload_file(
            workspace_key,
            dependencies_path,
            b"# AIDP provides aidputils, LangGraph and OCI runtime libraries.\n",
            repair_drift=repair_drift,
        ) or changed
        changed = self._upload_file(
            workspace_key,
            descriptor_path,
            descriptor,
            repair_drift=repair_drift,
        ) or changed
        agents = self._agents(workspace_key, name)
        if len(agents) > 1:
            raise AidpProvisionError(f"AIDP has duplicate agents named {name}.")
        created = False
        if not agents:
            self._request(
                "POST",
                f"/workspaces/{workspace_key}/agents",
                payload={
                    "displayName": name,
                    "description": "Global read-only Master Catalog governance Agent",
                    "pathInfo": root,
                    "type": "CODE",
                    "entryFilePath": entry_path,
                    "dependenciesFilePath": dependencies_path,
                    "computeKey": compute_key,
                    "sessionConfig": session_config,
                },
                phase="content",
            )
            agents = self._agents(workspace_key, name)
            if len(agents) != 1:
                raise AidpProvisionPending(
                    "AIDP has not published the global governance Agent yet.", "content"
                )
            created = True
            changed = True
        agent_key = str(agents[0].get("key") or agents[0].get("id") or "")
        if not agent_key:
            raise AidpProvisionPending(
                "AIDP has not published the global governance Agent identifier yet.", "content"
            )
        if changed and not created:
            self._request(
                "PUT",
                f"/workspaces/{workspace_key}/agents/{agent_key}",
                payload={
                    "displayName": name,
                    "description": "Global read-only Master Catalog governance Agent",
                    "entryFilePath": entry_path,
                    "dependenciesFilePath": dependencies_path,
                    "computeKey": compute_key,
                    "sessionConfig": session_config,
                },
                phase="content",
                retry_scope=f"agent-update:{hashlib.sha256(source).hexdigest()}",
            )
        return agent_key, changed

    def _ensure_agent_deployment(
        self,
        workspace_key: str,
        agent_key: str,
        compute_key: str,
        agent_name: str,
        *,
        redeploy_revision: str = "",
    ) -> tuple[dict[str, Any], bool]:
        path = f"/workspaces/{workspace_key}/agents/{agent_key}/deployments"
        deployments = self._list(path, phase="deployment")
        if len(deployments) > 1:
            raise AidpProvisionError("AIDP has duplicate global governance Agent deployments.")
        for deployment in deployments:
            state = str(
                deployment.get("lifecycleState") or deployment.get("state") or ""
            ).upper()
            if state == "ACTIVE":
                if redeploy_revision:
                    updated = self._request(
                        "POST",
                        f"{path}/actions/redeploy",
                        payload={
                            "displayName": (
                                self._resource_name(deployment)
                                or f"{agent_name}_deployment"
                            ),
                            "description": (
                                "Production deployment for the global governance Agent"
                            ),
                            "agentComputeKey": compute_key,
                            "agentKey": agent_key,
                        },
                        phase="deployment",
                        retry_scope=(
                            f"agent-redeploy:{agent_key}:{redeploy_revision}"
                        ),
                    )
                    return updated if isinstance(updated, dict) else {}, True
                return deployment, False
            if state in {"CREATING", "DEPLOYING", "UPDATING"}:
                raise AidpProvisionPending(
                    "The global governance Agent deployment is still starting.", "deployment"
                )
        if any(
            str(item.get("lifecycleState") or item.get("state") or "").upper()
            in {"FAILED", "CREATE_FAILED", "UPDATE_FAILED"}
            for item in deployments
        ):
            raise AidpProvisionError(
                "The global governance Agent deployment failed; retry the module redeploy."
            )
        deployment = self._request(
            "POST",
            f"{path}/actions/deploy",
            payload={
                "displayName": f"{agent_name}_deployment",
                "description": "Production deployment for the global governance Agent",
                "agentComputeKey": compute_key,
                "agentKey": agent_key,
            },
            phase="deployment",
            retry_scope=f"agent-deploy:{agent_key}",
        )
        return deployment if isinstance(deployment, dict) else {}, True

    @staticmethod
    def _require_operational_state(
        resource: dict[str, Any],
        allowed: set[str],
        kind: str,
        phase: str,
    ) -> dict[str, Any]:
        state = str(resource.get("lifecycleState") or resource.get("state") or "").upper()
        if state in allowed:
            return resource
        if state in {"FAILED", "DELETING", "DELETED", "DELETE_FAILED", "CANCELED", "CANCELLED"} or state.endswith("FAILED"):
            raise AidpProvisionError(f"The AIDP {kind} is in terminal state {state or 'UNKNOWN'}.")
        raise AidpProvisionPending(f"The AIDP {kind} is not operational yet. Retry shortly.", phase)

    def _workspace_object(
        self,
        workspace_key: str,
        path: str,
        *,
        phase: str,
    ) -> tuple[Any, dict[str, Any]]:
        body, headers = self._request(
            "GET",
            f"/workspaces/{workspace_key}/objects/{quote(path, safe='')}",
            headers={"Accept": "*/*"},
            allow_not_found=True,
            include_headers=True,
            phase=phase,
        )
        return body, headers

    @staticmethod
    def _workspace_object_exists(body: Any, headers: dict[str, Any]) -> bool:
        return body is not None or any(
            str(value)
            for name, value in headers.items()
            if name.casefold() in {"object-key", "object-type", "type"}
        )

    @staticmethod
    def _workspace_object_type(body: Any, headers: dict[str, Any]) -> str:
        for name, value in headers.items():
            if name.casefold() in {"object-type", "type"}:
                return str(value).upper()
        if isinstance(body, dict) and body.get("type") in {"FILE", "FOLDER"}:
            return str(body["type"])
        return ""

    @staticmethod
    def _content_matches(body: Any, expected: bytes) -> bool:
        if isinstance(body, bytes):
            actual = body
        elif isinstance(body, str):
            actual = body.encode("utf-8")
        elif isinstance(body, (dict, list)):
            actual = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        else:
            return False
        return actual == expected

    @classmethod
    def _notebook_matches(cls, actual: Any, expected: Any) -> bool:
        if isinstance(expected, dict):
            return isinstance(actual, dict) and all(
                key in actual and cls._notebook_matches(actual[key], value)
                for key, value in expected.items()
            )
        if isinstance(expected, list):
            if isinstance(actual, str) and all(
                isinstance(item, str) for item in expected
            ):
                return "".join(expected) == actual
            return (
                isinstance(actual, list)
                and len(actual) == len(expected)
                and all(
                    cls._notebook_matches(actual_item, expected_item)
                    for actual_item, expected_item in zip(actual, expected, strict=True)
                )
            )
        return actual == expected

    def _export_notebook(
        self,
        workspace_key: str,
        path: str,
    ) -> dict[str, Any] | None:
        exported = self._request(
            "POST",
            f"/workspaces/{workspace_key}/notebook/api/actions/export/contents/{quote(path, safe='')}",
            payload={"format": "ipynb"},
            allow_not_found=True,
            phase="content",
            retry_scope=path,
        )
        if exported is None:
            return None
        content = exported.get("content") if isinstance(exported, dict) else None
        if not isinstance(content, dict):
            raise AidpProvisionPending(
                f"AIDP has not exported notebook {path} yet.", "content"
            )
        return content

    def _notebook_state(
        self,
        workspace_key: str,
        path: str,
    ) -> tuple[bool, dict[str, Any] | None]:
        object_body, object_headers = self._workspace_object(
            workspace_key, path, phase="content"
        )
        exists = self._workspace_object_exists(object_body, object_headers)
        if not exists:
            return False, None
        if self._workspace_object_type(object_body, object_headers) not in {
            "",
            "NOTEBOOK",
        }:
            raise AidpProvisionError(f"Workspace path {path} exists but is not a notebook.")
        return True, self._export_notebook(workspace_key, path)

    def _ensure_folder(self, workspace_key: str, path: str) -> bool:
        body, headers = self._workspace_object(workspace_key, path, phase="workspace")
        if self._workspace_object_exists(body, headers):
            if self._workspace_object_type(body, headers) not in {"", "FOLDER"}:
                raise AidpProvisionError(f"Workspace path {path} exists but is not a folder.")
            return False
        self._request(
            "POST",
            f"/workspaces/{workspace_key}/objects",
            data=b"",
            headers={
                "Accept": "*/*",
                "path": path,
                "type": "FOLDER",
                "is-overwrite": "false",
                "Content-Type": "application/octet-stream",
            },
            phase="workspace",
        )
        body, headers = self._workspace_object(workspace_key, path, phase="workspace")
        if not self._workspace_object_exists(body, headers):
            raise AidpProvisionPending(f"AIDP has not published workspace folder {path} yet.", "workspace")
        return True

    def _upload_file(
        self,
        workspace_key: str,
        path: str,
        content: bytes,
        *,
        repair_drift: bool = True,
    ) -> bool:
        body, headers = self._workspace_object(workspace_key, path, phase="content")
        exists = self._workspace_object_exists(body, headers)
        if exists and self._workspace_object_type(body, headers) == "FOLDER":
            raise AidpProvisionError(f"Workspace path {path} exists but is not a file.")
        if exists and (self._content_matches(body, content) or not repair_drift):
            return False
        self._request(
            "POST",
            f"/workspaces/{workspace_key}/objects",
            data=content,
            headers={
                "Accept": "*/*",
                "path": path,
                "type": "FILE",
                "is-overwrite": str(exists).lower(),
                "Content-Type": "application/octet-stream",
            },
            phase="content",
        )
        body, _ = self._workspace_object(workspace_key, path, phase="content")
        if not self._content_matches(body, content):
            raise AidpProvisionPending(f"AIDP has not published workspace file {path} yet.", "content")
        return True

    def _workspace_object_key(self, workspace_key: str, path: str) -> str:
        body, headers = self._workspace_object(workspace_key, path, phase="permissions")
        normalized = {str(name).casefold(): value for name, value in headers.items()}
        object_key = str(normalized.get("object-key") or "")
        if object_key:
            return object_key
        object_path = str(
            normalized.get("folder")
            or normalized.get("path")
            or (body.get("path") if isinstance(body, dict) else "")
            or ""
        )
        if object_path and object_path != path:
            raise AidpProvisionError(
                f"AIDP returned a mismatched workspace object path for {path}."
            )
        if not object_path:
            raise AidpProvisionPending(f"AIDP has not published workspace object {path} yet.", "permissions")
        return object_path

    def _upload_notebook(
        self,
        workspace_key: str,
        path: str,
        notebook: dict[str, Any],
        *,
        repair_drift: bool = True,
    ) -> bool:
        content_path = f"/workspaces/{workspace_key}/notebook/api/contents/{quote(path, safe='')}"
        exists, current_content = self._notebook_state(workspace_key, path)
        if current_content is not None and (
            self._notebook_matches(current_content, notebook) or not repair_drift
        ):
            return False
        if exists and current_content is None and not repair_drift:
            raise AidpProvisionPending(
                f"AIDP has not published readable notebook {path} yet.", "content"
            )
        if not exists:
            parent = path.rsplit("/", 1)[0]
            created = self._request(
                "POST",
                f"/workspaces/{workspace_key}/notebook/api/contents/{quote(parent, safe='')}",
                payload={"copy_from": None, "ext": ".ipynb", "type": "notebook"},
                phase="content",
                retry_scope=path,
            )
            created_path = str((created or {}).get("path") or "")
            if not created_path:
                raise AidpProvisionPending("AIDP has not published the notebook path yet.", "content")
            self._request(
                "PATCH",
                f"/workspaces/{workspace_key}/notebook/api/contents/{quote(created_path, safe='')}",
                payload={"path": path},
                phase="content",
            )
        self._request(
            "PUT",
            content_path,
            payload={
                "name": path.rsplit("/", 1)[-1],
                "path": path,
                "type": "notebook",
                "content": notebook,
                "format": "json",
            },
            phase="content",
        )
        current_content = self._export_notebook(workspace_key, path)
        if current_content is None or not self._notebook_matches(current_content, notebook):
            raise AidpProvisionPending(f"AIDP has not published notebook {path} yet.", "content")
        return True

    @staticmethod
    def _control_manifest_path(key: str) -> str:
        return f"{CONTROL_ROOT}/{key}.json"

    @staticmethod
    def _legacy_control_manifest_path(key: str) -> str:
        return f"{LEGACY_WORKSPACE_ROOT}/.control/{key}.json"

    def _workspace_json(
        self,
        workspace_key: str,
        path: str,
        invalid_message: str,
    ) -> dict[str, Any] | None:
        body, headers = self._workspace_object(workspace_key, path, phase="workspace")
        if not self._workspace_object_exists(body, headers):
            return None
        if self._workspace_object_type(body, headers) == "FOLDER":
            raise AidpProvisionError(invalid_message)
        try:
            if isinstance(body, dict):
                payload = body
            elif isinstance(body, bytes):
                payload = json.loads(body.decode("utf-8"))
            elif isinstance(body, str):
                payload = json.loads(body)
            else:
                raise TypeError("unsupported control manifest body")
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AidpProvisionError(invalid_message) from exc
        if not isinstance(payload, dict):
            raise AidpProvisionError(invalid_message)
        return payload

    def _manifest(self, workspace_key: str, owner_key: str) -> dict[str, Any] | None:
        return self._workspace_json(
            workspace_key,
            self._control_manifest_path(owner_key),
            "The participant control manifest is invalid; delete the participant before retrying.",
        )

    @staticmethod
    def _manifest_participant_key(manifest: dict[str, Any], owner_key: str) -> str:
        key = str(manifest.get("participant_key") or "")
        declared_owner = manifest.get("owner_key")
        if declared_owner is None:
            if key != owner_key:
                raise AidpProvisionError("The participant control manifest has a different owner.")
            return key
        if declared_owner != owner_key:
            raise AidpProvisionError("The participant control manifest has a different owner.")
        code = manifest.get("participant_code")
        if (
            not isinstance(code, int)
            or isinstance(code, bool)
            or code < 101
            or key != participant_key(code)
        ):
            raise AidpProvisionError("The participant control manifest has an invalid participant code.")
        participant_folder(str(manifest.get("participant_email") or ""))
        return key

    @staticmethod
    def _legacy_manifest_workspace_path(manifest: dict[str, Any], key: str) -> str:
        workspace_path = str(manifest.get("workspace_path") or "")
        relative_path = workspace_path.removeprefix(f"{LEGACY_MEDALLION_ROOT}/")
        parts = relative_path.split("/")
        if (
            manifest.get("layout_version") != 2
            or manifest.get("participant_key") != key
            or not workspace_path.startswith(f"{LEGACY_MEDALLION_ROOT}/")
            or len(parts) != 2
            or parts[0] in {"", ".control"}
            or parts[1] not in LEGACY_LAB_IDS
            or manifest.get("industry") != parts[1]
        ):
            raise AidpProvisionError(
                "The participant control manifest does not contain an exact workspace path; cleanup stopped."
            )
        return workspace_path

    @classmethod
    def _manifest_labs(
        cls, manifest: dict[str, Any], key: str
    ) -> dict[str, dict[str, Any]]:
        if manifest.get("layout_version") == 2:
            workspace_path = cls._legacy_manifest_workspace_path(manifest, key)
            lab_id = str(manifest["industry"])
            return {
                lab_id: {
                    "pack_version": "legacy-v2",
                    "pack_hash": "",
                    "workspace_path": workspace_path,
                    "job_name": f"wf_{key}_{lab_id}_medallion",
                    "phase": str(manifest.get("phase") or "workspace"),
                    "operation": manifest.get("reset"),
                }
            }
        labs = manifest.get("labs")
        if (
            manifest.get("layout_version") not in {3, 4, LAYOUT_VERSION}
            or not isinstance(labs, dict)
        ):
            raise AidpProvisionError(
                "The participant control manifest is invalid; cleanup stopped."
            )
        participant = cls._manifest_participant_key(manifest, key)
        email = str(manifest.get("participant_email") or "") if manifest.get("owner_key") else None
        for lab_id, state in labs.items():
            if (
                not isinstance(state, dict)
                or lab_id not in LEGACY_LAB_IDS | set(available_lab_ids())
            ):
                raise AidpProvisionError("The participant lab journal is invalid; cleanup stopped.")
            if not cls._lab_workspace_path_is_exact(state, participant, lab_id, email):
                raise AidpProvisionError(
                    "The participant control manifest does not contain an exact lab workspace path; cleanup stopped."
                )
        return labs

    @staticmethod
    def _lab_workspace_path_is_exact(
        state: dict[str, Any], key: str, lab_id: str, email: str | None = None
    ) -> bool:
        if state.get("pack_version") == "legacy-v2":
            pattern = rf"{re.escape(LEGACY_MEDALLION_ROOT)}/(?!\.control(?:/|$))[^/]+/{re.escape(lab_id)}"
            return re.fullmatch(pattern, str(state.get("workspace_path") or "")) is not None
        if email is None and re.fullmatch(r"u[1-9][0-9]*", key):
            workspace_path = str(state.get("workspace_path") or "")
            patterns = (
                rf"{re.escape(WORKSPACE_ROOT)}/{re.escape(lab_id)}/{re.escape(key)}_[^/]+",
                rf"{re.escape(LEGACY_MEDALLION_ROOT)}/{re.escape(key)}_[^/]+/{re.escape(lab_id)}",
            )
            return any(re.fullmatch(pattern, workspace_path) for pattern in patterns)
        workspace_path = str(state.get("workspace_path") or "")
        legacy_folder = (
            f"{key}_{participant_folder(email)}"
            if email and re.fullmatch(r"u[1-9][0-9]*", key)
            else key
        )
        return workspace_path in {
            workspace_root(key, lab_id, email),
            f"{LEGACY_MEDALLION_ROOT}/{legacy_folder}/{lab_id}",
        }

    def _write_manifest(
        self,
        workspace_key: str,
        key: str,
        manifest: dict[str, Any],
    ) -> None:
        content = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
        try:
            self._upload_file(
                workspace_key,
                self._control_manifest_path(key),
                content,
                repair_drift=True,
            )
        except AidpProvisionPending as exc:
            phase = str(manifest.get("phase") or "content")
            reset = manifest.get("reset")
            if isinstance(reset, dict) and reset.get("phase") == "cleanup":
                phase = "cleanup"
            for state in (manifest.get("labs") or {}).values():
                if not isinstance(state, dict):
                    continue
                operation = state.get("operation")
                if isinstance(operation, dict) and operation.get("phase") == "cleanup":
                    phase = "cleanup"
                    break
                if state.get("phase") != "active":
                    phase = str(state.get("phase") or phase)
            if phase not in {
                "cleanup", "workspace", "database", "schemas", "content", "permissions",
                "deployment",
            }:
                phase = "permissions"
            raise AidpProvisionPending(
                "AIDP is still accepting the participant control manifest.", phase
            ) from exc

    def _migrate_manifest(
        self,
        workspace_key: str,
        key: str,
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        if manifest.get("layout_version") == LAYOUT_VERSION:
            return manifest
        if manifest.get("layout_version") == 2:
            manifest = {
                "layout_version": 3,
                "participant_key": key,
                "labs": self._manifest_labs(manifest, key),
            }
        if manifest.get("layout_version") != 3:
            return manifest
        participant = self._manifest_participant_key(manifest, key)
        labs = self._manifest_labs(manifest, key)
        for state in labs.values():
            state.setdefault("catalog_name", LEGACY_CATALOG_NAME)
            state.setdefault("catalog_key", "")
        migrated = {
            **manifest,
            "layout_version": LAYOUT_VERSION,
            "region": self.settings.aidp_region,
            "model_id": self.settings.agent_model_id,
            "catalog": {"name": catalog_name_for(participant), "key": ""},
            "sync_state": {},
            "labs": labs,
        }
        self._write_manifest(workspace_key, key, migrated)
        return migrated

    def _ensure_manifest(
        self,
        workspace_key: str,
        owner_key: str,
        email: str,
        participant_code: int,
        lab_ids: str | tuple[str, ...],
    ) -> dict[str, Any]:
        requested = _validated_lab_ids(lab_ids)
        normalized_email = email.strip().casefold()
        participant_folder(normalized_email)
        existing = self._manifest(workspace_key, owner_key)
        if existing is not None:
            existing = self._migrate_manifest(workspace_key, owner_key, existing)
        if existing is None:
            existing = {
                "layout_version": LAYOUT_VERSION,
                "owner_key": owner_key,
                "participant_key": participant_key(participant_code),
                "participant_code": participant_code,
                "participant_email": normalized_email,
                "region": self.settings.aidp_region,
                "model_id": self.settings.agent_model_id,
                "catalog": {
                    "name": participant_catalog_name(participant_key(participant_code)),
                    "key": "",
                },
                "sync_state": {},
                "labs": {},
            }
        key = self._manifest_participant_key(existing, owner_key)
        if existing.get("owner_key") and existing.get("participant_email") != normalized_email:
            raise AidpProvisionConflict("The participant email does not match the assigned workspace")
        labs = self._manifest_labs(existing, owner_key)
        changed = False
        for lab_id in requested:
            if lab_id in labs:
                continue
            pack = load_lab_pack(lab_id)
            labs[lab_id] = {
                "pack_version": pack.pack_version,
                "pack_hash": pack.pack_sha256,
                "workspace_path": workspace_root(key, lab_id, normalized_email if existing.get("owner_key") else None),
                "job_name": f"wf_{key}_{lab_id}",
                "catalog_name": participant_catalog_name(key),
                "catalog_key": "",
                "phase": "workspace",
                "operation": None,
            }
            changed = True
        if changed or self._manifest(workspace_key, owner_key) is None:
            self._write_manifest(workspace_key, owner_key, existing)
        return existing

    def _advance_lab_manifest(
        self,
        workspace_key: str,
        manifest: dict[str, Any],
        lab_id: str,
        phase: str,
    ) -> None:
        owner_key = str(manifest.get("owner_key") or manifest["participant_key"])
        state = self._manifest_labs(manifest, owner_key)[lab_id]
        if state.get("phase") == phase:
            return
        state["phase"] = phase
        self._write_manifest(
            workspace_key,
            owner_key,
            manifest,
        )

    @staticmethod
    def _resource_name(resource: dict[str, Any]) -> str:
        return str(resource.get("displayName") or resource.get("name") or "")

    def _ensure_schema(
        self,
        catalog_key: str,
        catalog_name: str,
        layer: str,
    ) -> tuple[dict[str, Any], bool]:
        name = schema_name(layer)

        def matches() -> list[dict[str, Any]]:
            return [
                item
                for item in self._list(
                    "/schemas", params={"catalogKey": catalog_key}, phase="schemas"
                )
                if self._resource_name(item) == name
            ]

        existing = matches()
        if len(existing) > 1:
            raise AidpProvisionError(f"AIDP has duplicate schemas named {name}.")
        if existing:
            state = str(existing[0].get("lifecycleState") or "ACTIVE").upper()
            if state != "ACTIVE":
                raise AidpProvisionPending(
                    f"AIDP schema {name} is still {state.lower()}.", "schemas"
                )
            return existing[0], False
        self._request(
            "POST",
            "/schemas",
            payload={
                "displayName": name,
                "description": f"Shared collaborative {layer.title()} schema for the AIDP lab",
                "catalogName": catalog_name,
            },
            phase="schemas",
        )
        published = matches()
        if len(published) > 1:
            raise AidpProvisionError(f"AIDP has duplicate schemas named {name}.")
        if len(published) != 1 or not published[0].get("key"):
            raise AidpProvisionPending(f"AIDP has not published schema {name} yet.", "schemas")
        state = str(published[0].get("lifecycleState") or "ACTIVE").upper()
        if state != "ACTIVE":
            raise AidpProvisionPending(
                f"AIDP schema {name} is still {state.lower()}.", "schemas"
            )
        return published[0], True

    def _ensure_catalog_contract(
        self,
        catalog_key: str,
        catalog_name: str,
    ) -> tuple[dict[str, dict[str, Any]], bool]:
        schemas: dict[str, dict[str, Any]] = {}
        changed = False
        for layer in LAYER_PREFIXES:
            schemas[layer], created = self._ensure_schema(catalog_key, catalog_name, layer)
            changed = changed or created
        return schemas, changed

    @staticmethod
    def _job_tasks(
        root: str,
        compute_key: str,
        notebooks: tuple[LabAsset, ...],
        parameters: dict[str, str],
    ) -> list[dict[str, Any]]:
        return [
            {
                "type": "NOTEBOOK_TASK",
                "taskKey": notebook.task_key,
                "dependsOn": [{"taskKey": task_key} for task_key in notebook.depends_on],
                "runIf": "ALL_SUCCESS",
                "maxRetries": 0,
                "isRetryOnTimeout": False,
                "notebookPath": f"{root}/{notebook.name}",
                "cluster": {"clusterKey": compute_key},
                "parameters": [
                    {"name": name, "value": value}
                    for name, value in parameters.items()
                ],
            }
            for notebook in notebooks
        ]

    @staticmethod
    def _job_task_matches(
        actual: Any,
        expected: dict[str, Any],
        compute_key: str,
    ) -> bool:
        return bool(
            isinstance(actual, dict)
            and actual.get("type") == "NOTEBOOK_TASK"
            and actual.get("taskKey") == expected["taskKey"]
            and isinstance(actual.get("dependsOn"), list)
            and [
                dependency.get("taskKey")
                if isinstance(dependency, dict)
                else None
                for dependency in actual["dependsOn"]
            ]
            == [dependency["taskKey"] for dependency in expected["dependsOn"]]
            and actual.get("runIf") == "ALL_SUCCESS"
            and actual.get("notebookPath") == expected["notebookPath"]
            and (actual.get("cluster") or {}).get("clusterKey") == compute_key
            and actual.get("parameters") == expected["parameters"]
        )

    @classmethod
    def _job_tasks_match(
        cls,
        actual_tasks: Any,
        expected_tasks: list[dict[str, Any]],
        compute_key: str,
    ) -> bool:
        if not isinstance(actual_tasks, list) or len(actual_tasks) != len(expected_tasks):
            return False
        return all(
            cls._job_task_matches(actual, expected, compute_key)
            for actual, expected in zip(actual_tasks, expected_tasks, strict=True)
        )

    @staticmethod
    def _job_compute_matches(clusters: Any, compute_key: str) -> bool:
        if not isinstance(clusters, list) or len(clusters) != 1:
            return False
        cluster = clusters[0]
        return isinstance(cluster, dict) and cluster.get("clusterKey") == compute_key

    def _job_contract_is_visible(
        self,
        details: Any,
        payload: dict[str, Any],
        compute_key: str,
    ) -> bool:
        if not isinstance(details, dict):
            return False
        return bool(
            self._resource_name(details) == payload["name"]
            and details.get("path") == payload["path"]
            and details.get("maxConcurrentRuns") == payload["maxConcurrentRuns"]
            and self._job_tasks_match(details.get("tasks"), payload["tasks"], compute_key)
            and self._job_compute_matches(details.get("jobClusters"), compute_key)
            and (
                "continuous" not in payload
                or details.get("continuous") == payload["continuous"]
            )
        )

    def _job_key(self, workspace_key: str, job_name: str) -> str:
        jobs = [
            item
            for item in self._list(
                f"/workspaces/{workspace_key}/jobs", phase="content"
            )
            if self._resource_name(item) == job_name
        ]
        if len(jobs) > 1:
            raise AidpProvisionError(f"AIDP has duplicate jobs named {job_name}.")
        return str((jobs[0] if jobs else {}).get("key") or "")

    def _create_job(self, workspace_key: str, payload: dict[str, Any]) -> str:
        created = self._request(
            "POST",
            f"/workspaces/{workspace_key}/jobs",
            payload={
                name: payload[name]
                for name in ("name", "path", "description", "maxConcurrentRuns", "continuous")
                if name in payload
            },
            phase="content",
        )
        job_key = str((created or {}).get("key") or "")
        if not job_key:
            raise AidpProvisionPending(
                "AIDP has not published the participant workflow yet.", "content"
            )
        return job_key

    def _publish_job(
        self,
        workspace_key: str,
        job_key: str,
        payload: dict[str, Any],
        compute_key: str,
    ) -> None:
        self._request(
            "PUT",
            f"/workspaces/{workspace_key}/jobs/{job_key}",
            payload=payload,
            phase="content",
        )
        details = self._request(
            "GET",
            f"/workspaces/{workspace_key}/jobs/{job_key}",
            allow_not_found=True,
            phase="content",
        )
        if not self._job_contract_is_visible(details, payload, compute_key):
            raise AidpProvisionPending(
                "AIDP has not published the complete participant workflow yet.", "content"
            )

    def _ensure_job(
        self,
        workspace_key: str,
        compute_key: str,
        key: str,
        pack: LabPack,
        root: str,
        catalog_name: str,
        *,
        repair_drift: bool = True,
    ) -> tuple[str, str, bool]:
        job_name = f"wf_{key}_{pack.lab_id}"
        parameters = {
            "participant_key": key,
            "lab_id": pack.lab_id,
            "workspace_root": root,
            "bucket_name": self.settings.bucket_name,
            "objectstorage_namespace": self.settings.objectstorage_namespace,
            "catalog_name": catalog_name,
        }
        tasks = self._job_tasks(root, compute_key, pack.notebooks, parameters)
        payload = {
            "name": job_name,
            "path": root,
            "description": f"{pack.display_name} medallion tutorial for {key}",
            "maxConcurrentRuns": 1,
            "jobClusters": [{"clusterKey": compute_key}],
            "tasks": tasks,
        }
        job_key = self._job_key(workspace_key, job_name)
        if job_key:
            details = self._request(
                "GET",
                f"/workspaces/{workspace_key}/jobs/{job_key}",
                allow_not_found=True,
                phase="content",
            )
            if self._job_contract_is_visible(details, payload, compute_key):
                return job_name, job_key, False
            if details is None:
                raise AidpProvisionPending(
                    "AIDP has not published the participant workflow yet.", "content"
                )
            if not repair_drift:
                return job_name, job_key, False
        if not job_key:
            job_key = self._create_job(workspace_key, payload)
        self._publish_job(workspace_key, job_key, payload, compute_key)
        return job_name, job_key, True

    @staticmethod
    def _permission_grantee(item: dict[str, Any]) -> tuple[str, str]:
        grantee = item.get("grantee")
        if isinstance(grantee, dict):
            target = grantee.get("target")
            grantee_type = grantee.get("type")
        else:
            target = grantee
            grantee_type = None
        return (
            str(target or item.get("granteeName") or ""),
            str(grantee_type or item.get("granteeType") or "").upper(),
        )

    @staticmethod
    def _permission_values(item: dict[str, Any]) -> set[str]:
        return set(item.get("granteePermissions") or item.get("permissions") or [])

    @classmethod
    def _permission_matches(
        cls,
        item: dict[str, Any],
        user_ocid: str,
        permission: str,
        inheritable: bool | None = None,
    ) -> bool:
        grantee_value, grantee_type = cls._permission_grantee(item)
        return (
            grantee_value == user_ocid
            and grantee_type == "USER"
            and permission in cls._permission_values(item)
            and (inheritable is None or item.get("isPermissionsInheritable") is inheritable)
        )

    @classmethod
    def _permission_is_exact(
        cls,
        item: dict[str, Any],
        user_ocid: str,
        permission: str,
        inheritable: bool | None,
    ) -> bool:
        return cls._permission_matches(
            item, user_ocid, permission, inheritable
        ) and cls._permission_values(item) == {permission}

    @classmethod
    def _assert_no_permission_conflict(
        cls,
        items: list[dict[str, Any]],
        user_ocid: str,
        permission: str,
        inheritable: bool | None,
    ) -> None:
        if any(
            cls._permission_grantee(item) == (user_ocid, "USER")
            and not cls._permission_is_exact(
                item, user_ocid, permission, inheritable
            )
            for item in items
        ):
            raise AidpProvisionError(
                "AIDP found a conflicting direct permission for this participant; "
                "an administrator must remove the broader grant before retrying."
            )

    def _ensure_permission(
        self,
        resource_path: str,
        assignment_key: str,
        user_ocid: str,
        permission: str,
        *,
        inheritable: bool | None = None,
    ) -> bool:
        permissions_path = f"{resource_path}/permissions"
        current = self._list(permissions_path, phase="permissions")
        self._assert_no_permission_conflict(
            current, user_ocid, permission, inheritable
        )
        if any(
            self._permission_is_exact(item, user_ocid, permission, inheritable)
            for item in current
        ):
            return False
        assignment: dict[str, Any] = {
            "assignees": {"type": "USER", "targets": [user_ocid]},
            "permissions": [permission],
        }
        if inheritable is not None:
            assignment["isPermissionsInheritable"] = inheritable
        self._request(
            "POST",
            f"{resource_path}/actions/managePermission",
            payload={assignment_key: assignment},
            phase="permissions",
        )
        current = self._list(permissions_path, phase="permissions")
        self._assert_no_permission_conflict(
            current, user_ocid, permission, inheritable
        )
        if not any(
            self._permission_is_exact(item, user_ocid, permission, inheritable)
            for item in current
        ):
            raise AidpProvisionPending("AIDP has not applied the participant permission yet.", "permissions")
        return True

    def _assert_permission_absent(
        self,
        resource_path: str,
        user_ocid: str,
        permission: str,
    ) -> None:
        current = self._list(f"{resource_path}/permissions", phase="permissions")
        if any(
            self._permission_grantee(item) == (user_ocid, "USER")
            and permission in self._permission_values(item)
            for item in current
        ):
            raise AidpProvisionError(
                "This participant still has direct catalog SELECT. An AI_DATA_PLATFORM_ADMIN "
                "must revoke that permission before table-level isolation can be enforced."
            )

    def _ensure_lab_table_permissions(
        self,
        catalog_key: str,
        participant_key: str,
        lab_id: str,
        user_ocid: str,
    ) -> bool:
        pack = load_lab_pack(lab_id)
        schemas = self._shared_schemas(catalog_key)
        changed = False
        for layer, logical_names in pack.tables.items():
            schema = schemas.get(layer)
            schema_key = str((schema or {}).get("key") or "")
            if not schema_key:
                continue
            expected = {
                table_name(participant_key, lab_id, logical_name)
                for logical_name in logical_names
            }
            for table in self._schema_tables(catalog_key, schema_key):
                table_key = str(table.get("key") or "")
                if table_key and self._resource_name(table) in expected:
                    changed = self._ensure_permission(
                        f"/tables/{table_key}",
                        "assignTablePermissionDetails",
                        user_ocid,
                        "SELECT",
                    ) or changed
        return changed

    def _ensure_permissions(
        self,
        workspace_key: str,
        user_ocid: str,
        participant_root: str,
        job_key: str,
        catalog_key: str,
        participant_key: str,
        lab_id: str,
    ) -> bool:
        root_key = self._workspace_object_key(workspace_key, WORKSPACE_ROOT)
        participant_object_key = self._workspace_object_key(workspace_key, participant_root)
        changed = self._ensure_permission(
            f"/workspaces/{workspace_key}/objects/{quote(root_key, safe='')}",
            "assignWorkspaceObjectPermissionDetails",
            user_ocid,
            "READ",
            inheritable=False,
        )
        changed = self._ensure_permission(
            f"/workspaces/{workspace_key}/objects/{quote(participant_object_key, safe='')}",
            "assignWorkspaceObjectPermissionDetails",
            user_ocid,
            "ADMIN",
            inheritable=True,
        ) or changed
        catalog_path = f"/catalogs/{catalog_key}"
        self._assert_permission_absent(catalog_path, user_ocid, "SELECT")
        changed = self._ensure_lab_table_permissions(
            catalog_key, participant_key, lab_id, user_ocid
        ) or changed
        changed = self._ensure_permission(
            f"/workspaces/{workspace_key}/jobs/{job_key}",
            "assignJobPermissionDetails",
            user_ocid,
            "MANAGE",
        ) or changed
        return changed

    def _ensure_workspace_layout(
        self,
        workspace_key: str,
        paths: tuple[str, ...],
    ) -> bool:
        changed = False
        for path in paths:
            changed = self._ensure_folder(workspace_key, path) or changed
        return changed

    def _pending_after_change(
        self,
        changed: bool,
        was_active: bool,
        workspace_key: str,
        manifest: dict[str, Any],
        lab_id: str,
        next_phase: str,
        message: str,
    ) -> None:
        if not changed:
            return
        if not was_active:
            self._advance_lab_manifest(workspace_key, manifest, lab_id, next_phase)
        raise AidpProvisionPending(message, next_phase)

    def _ensure_participant_content(
        self,
        workspace_key: str,
        compute_key: str,
        key: str,
        pack: LabPack,
        root: str,
        catalog_name: str,
        repair_drift: bool,
    ) -> tuple[str, str, bool]:
        content_changed = self._upload_file(
            workspace_key,
            f"{root}/lab-manifest.json",
            json.dumps(
                {
                    "participant_key": key,
                    "lab_id": pack.lab_id,
                    "pack_version": pack.pack_version,
                    "pack_hash": pack.pack_sha256,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
            repair_drift=repair_drift,
        )
        for asset in pack.datasets:
            content_changed = self._upload_file(
                workspace_key,
                f"{root}/source/{asset.name}",
                asset.read_bytes(),
                repair_drift=repair_drift,
            ) or content_changed
        for asset in pack.notebooks:
            notebook = json.loads(asset.read_bytes())
            content_changed = self._upload_notebook(
                workspace_key,
                f"{root}/{asset.name}",
                notebook,
                repair_drift=repair_drift,
            ) or content_changed
        job_name, job_key, job_changed = self._ensure_job(
            workspace_key,
            compute_key,
            key,
            pack,
            root,
            catalog_name,
            repair_drift=repair_drift,
        )
        return job_name, job_key, content_changed or job_changed

    def _active_lab_material(
        self,
        user_ocid: str,
        email: str,
        lab_id: str,
        key: str,
        state: dict[str, Any],
        participant_code: Any,
    ) -> UserMaterial | None:
        if str(state.get("phase") or "workspace") != "active":
            return None
        catalog_name = str(state.get("catalog_name") or catalog_name_for(key))
        if state.get("pack_version") != "legacy-v2" and catalog_name == CATALOG_NAME:
            catalog = self._catalog(catalog_name, allow_missing=True)
            if catalog and self._ensure_lab_table_permissions(
                str(catalog.get("key") or ""), key, lab_id, user_ocid
            ):
                raise AidpProvisionPending(
                    "Participant table permissions were repaired; final verification is next.",
                    "permissions",
                )
        return UserMaterial(
            email,
            lab_id,
            key,
            str(state["workspace_path"]),
            str(state["job_name"]),
            str(state.get("pack_version") or "legacy-v2"),
            participant_code=participant_code if isinstance(participant_code, int) else None,
        )

    def _provision_lab(
        self,
        user_ocid: str,
        email: str,
        lab_id: str,
        manifest: dict[str, Any],
    ) -> UserMaterial:
        pack = load_lab_pack(lab_id)
        owner_key = participant_owner_key(user_ocid)
        key = self._manifest_participant_key(manifest, owner_key)
        participant_code = manifest.get("participant_code")
        participant_folder(email)
        workspace_key = str(self._workspace()["key"])
        workspace_changed = self._ensure_workspace_layout(
            workspace_key,
            (WORKSPACE_ROOT, CONTROL_ROOT),
        )
        state = self._manifest_labs(manifest, owner_key)[lab_id]
        active_material = self._active_lab_material(
            user_ocid, email, lab_id, key, state, participant_code
        )
        if active_material is not None:
            return active_material
        root = str(state["workspace_path"])
        lab_root = root.rsplit("/", 1)[0]
        participant_root = root
        job_name = str(state["job_name"])
        repair_drift = True

        compute_key = str(self._shared_compute(workspace_key)["key"])
        workspace_changed = self._ensure_workspace_layout(
            workspace_key,
            (lab_root, participant_root, f"{root}/source"),
        ) or workspace_changed
        self._pending_after_change(
            workspace_changed,
            False,
            workspace_key,
            manifest,
            lab_id,
            "schemas",
            "Participant workspace is ready; schemas are next.",
        )

        catalog_name = str(state.get("catalog_name") or catalog_name_for(key))
        catalog, catalog_changed = self._ensure_catalog(catalog_name)
        catalog_key = str(catalog["key"])
        if state.get("catalog_key") != catalog_key:
            state["catalog_key"] = catalog_key
            self._write_manifest(workspace_key, owner_key, manifest)
        _schemas, schemas_changed = self._ensure_catalog_contract(catalog_key, catalog_name)
        self._pending_after_change(
            catalog_changed or schemas_changed,
            False,
            workspace_key,
            manifest,
            lab_id,
            "content",
            "Participant schemas are ready; content is next.",
        )

        job_name, job_key, content_changed = self._ensure_participant_content(
            workspace_key, compute_key, key, pack, root, catalog_name, repair_drift
        )
        self._pending_after_change(
            content_changed,
            False,
            workspace_key,
            manifest,
            lab_id,
            "permissions",
            "Participant content is ready; permissions are next.",
        )

        self._ensure_permissions(
            workspace_key,
            user_ocid,
            participant_root,
            job_key,
            catalog_key,
            key,
            lab_id,
        )
        self._advance_lab_manifest(workspace_key, manifest, lab_id, "active")
        return UserMaterial(
            email, lab_id, key, root, job_name, pack.pack_version,
            participant_code=participant_code if isinstance(participant_code, int) else None,
        )

    def _provision_user(
        self, user_ocid: str, email: str, lab_ids: str | list[str], participant_code: int
    ) -> UserMaterial | tuple[UserMaterial, ...]:
        requested = _validated_lab_ids(lab_ids)
        owner_key = participant_owner_key(user_ocid)
        participant_folder(email)
        workspace_key = str(self._workspace()["key"])
        self._ensure_workspace_layout(workspace_key, (WORKSPACE_ROOT, CONTROL_ROOT))
        manifest = self._ensure_manifest(
            workspace_key, owner_key, email, participant_code, requested
        )
        materials = tuple(
            self._provision_lab(user_ocid, email, lab_id, manifest)
            for lab_id in requested
        )
        return materials[0] if isinstance(lab_ids, str) else materials

    async def provision_user(
        self, user_ocid: str, email: str, lab_ids: str | list[str], participant_code: int
    ) -> UserMaterial | tuple[UserMaterial, ...]:
        owner_key = participant_owner_key(user_ocid)
        async with self._locks.setdefault(owner_key, asyncio.Lock()):
            return await asyncio.to_thread(
                self._provision_user, user_ocid, email, lab_ids, participant_code
            )

    def _user_labs(self, user_ocids: list[str]) -> dict[str, list[UserMaterial]]:
        if not user_ocids:
            return {}
        workspace_key = str(self._workspace()["key"])
        keys = {participant_owner_key(user_ocid): user_ocid for user_ocid in user_ocids}
        result: dict[str, list[UserMaterial]] = {}
        for key, user_ocid in keys.items():
            manifest = self._manifest(workspace_key, key)
            if manifest is not None:
                manifest = self._migrate_manifest(workspace_key, key, manifest)
                labs = self._manifest_labs(manifest, key)
            else:
                legacy = self._workspace_json(
                    workspace_key,
                    self._legacy_control_manifest_path(key),
                    "The legacy participant control manifest is invalid.",
                )
                labs = self._manifest_labs(legacy, key) if legacy else {}
            technical_key = self._manifest_participant_key(manifest, key) if manifest else key
            participant_code = manifest.get("participant_code") if manifest else None
            materials = [
                UserMaterial(
                    "",
                    lab_id,
                    technical_key,
                    str(state["workspace_path"]),
                    str(state["job_name"]),
                    str(state.get("pack_version") or "legacy-v2"),
                    str(state.get("phase") or "workspace"),
                    participant_code if isinstance(participant_code, int) else None,
                )
                for lab_id, state in labs.items()
            ]
            if materials:
                result[user_ocid] = materials
        return result

    async def list_user_labs(
        self, user_ocids: list[str]
    ) -> dict[str, list[UserMaterial]]:
        return await asyncio.to_thread(self._user_labs, user_ocids)

    async def add_lab(self, user_ocid: str, email: str, lab_id: str) -> UserMaterial:
        owner_key = participant_owner_key(user_ocid)
        async with self._locks.setdefault(owner_key, asyncio.Lock()):
            return await asyncio.to_thread(self._add_lab, user_ocid, email, lab_id)

    def _add_lab(self, user_ocid: str, email: str, lab_id: str) -> UserMaterial:
        owner_key = participant_owner_key(user_ocid)
        workspace_key = str(self._workspace()["key"])
        manifest = self._manifest(workspace_key, owner_key)
        if manifest is None:
            raise AidpProvisionConflict("This participant has no assigned laboratory")
        manifest = self._migrate_manifest(workspace_key, owner_key, manifest)
        participant_code = manifest.get("participant_code")
        if not isinstance(participant_code, int):
            raise AidpProvisionConflict("Legacy participants must be recreated before adding laboratories")
        material = self._provision_user(user_ocid, email, lab_id, participant_code)
        assert isinstance(material, UserMaterial)
        return material

    def _redeploy_lab(
        self,
        user_ocid: str,
        email: str,
        lab_id: str,
        operation_id: str,
    ) -> UserMaterial:
        _validated_lab_ids(lab_id)
        owner_key = participant_owner_key(user_ocid)
        workspace_key = str(self._workspace()["key"])
        manifest = self._manifest(workspace_key, owner_key)
        if manifest is None:
            raise AidpProvisionConflict("This lab is not assigned to the participant")
        manifest = self._migrate_manifest(workspace_key, owner_key, manifest)
        key = self._manifest_participant_key(manifest, owner_key)
        if lab_id not in self._manifest_labs(manifest, owner_key):
            raise AidpProvisionConflict("This lab is not assigned to the participant")
        state = self._manifest_labs(manifest, owner_key)[lab_id]
        operation = state.get("operation")
        if isinstance(operation, dict) and operation.get("operation_id") == operation_id:
            if operation.get("phase") == "complete":
                return UserMaterial(
                    email, lab_id, key, str(state["workspace_path"]),
                    str(state["job_name"]), str(state["pack_version"]), "active",
                    manifest.get("participant_code") if isinstance(manifest.get("participant_code"), int) else None,
                )
        elif isinstance(operation, dict) and operation.get("phase") in {"cleanup", "provision"}:
            raise AidpProvisionConflict("Another operation is already in progress for this lab")
        else:
            operation = {"operation_id": operation_id, "type": "redeploy", "phase": "cleanup"}
            state["operation"] = operation
            self._write_manifest(workspace_key, owner_key, manifest)

        if operation.get("phase") == "cleanup":
            self._cleanup_lab(
                workspace_key, key, lab_id, state, preserve_workspace=True
            )
            pack = load_lab_pack(lab_id)
            state.update(
                pack_version=pack.pack_version,
                pack_hash=pack.pack_sha256,
                workspace_path=workspace_root(
                    key, lab_id, str(manifest.get("participant_email") or "") or None
                ),
                job_name=f"wf_{key}_{lab_id}",
                catalog_name=participant_catalog_name(key),
                catalog_key="",
                phase="workspace",
            )
            operation["phase"] = "provision"
            self._write_manifest(workspace_key, owner_key, manifest)
        material = self._provision_lab(user_ocid, email, lab_id, manifest)
        operation["phase"] = "complete"
        self._write_manifest(workspace_key, owner_key, manifest)
        return material

    async def redeploy_lab(
        self, user_ocid: str, email: str, lab_id: str, operation_id: str
    ) -> UserMaterial:
        owner_key = participant_owner_key(user_ocid)
        async with self._locks.setdefault(owner_key, asyncio.Lock()):
            return await asyncio.to_thread(
                self._redeploy_lab, user_ocid, email, lab_id, operation_id
            )

    def _delete_lab(
        self, user_ocid: str, lab_id: str, operation_id: str
    ) -> None:
        owner_key = participant_owner_key(user_ocid)
        workspace_key = str(self._workspace()["key"])
        manifest = self._manifest(workspace_key, owner_key)
        if manifest is None:
            return
        manifest = self._migrate_manifest(workspace_key, owner_key, manifest)
        key = self._manifest_participant_key(manifest, owner_key)
        labs = self._manifest_labs(manifest, owner_key)
        if lab_id not in labs:
            return
        if len(labs) == 1:
            raise AidpProvisionConflict(
                "The last lab cannot be removed; delete the participant instead"
            )
        state = labs[lab_id]
        operation = state.get("operation")
        if isinstance(operation, dict) and operation.get("operation_id") != operation_id and operation.get("phase") != "complete":
            raise AidpProvisionConflict("Another operation is already in progress for this lab")
        state["operation"] = {"operation_id": operation_id, "type": "delete", "phase": "cleanup"}
        self._write_manifest(workspace_key, owner_key, manifest)
        self._cleanup_lab(workspace_key, key, lab_id, state)
        labs.pop(lab_id)
        self._write_manifest(workspace_key, owner_key, manifest)

    async def delete_lab(
        self, user_ocid: str, lab_id: str, operation_id: str
    ) -> None:
        owner_key = participant_owner_key(user_ocid)
        async with self._locks.setdefault(owner_key, asyncio.Lock()):
            await asyncio.to_thread(self._delete_lab, user_ocid, lab_id, operation_id)

    def _delete_object_storage_prefix(self, prefix: str) -> None:
        start: str | None = None
        while True:
            response = self.object_storage.list_objects(
                self.settings.objectstorage_namespace,
                self.settings.bucket_name,
                prefix=prefix,
                start=start,
            )
            for item in response.data.objects:
                self.object_storage.delete_object(
                    self.settings.objectstorage_namespace,
                    self.settings.bucket_name,
                    item.name,
                )
            start = response.data.next_start_with
            if not start:
                return

    def _object_storage_prefix_exists(self, prefix: str) -> bool:
        response = self.object_storage.list_objects(
            self.settings.objectstorage_namespace,
            self.settings.bucket_name,
            prefix=prefix,
            start=None,
        )
        return bool(response.data.objects)

    def _lab_jobs(
        self, workspace_key: str, job_name: str
    ) -> list[dict[str, Any]]:
        return [
            job
            for job in self._list(
                f"/workspaces/{workspace_key}/jobs", phase="content"
            )
            if self._resource_name(job) == job_name
        ]

    def _cleanup_lab_job(self, workspace_key: str, job_name: str) -> None:
        for job in self._lab_jobs(workspace_key, job_name):
            job_key = job.get("key") or job.get("id")
            state = str(job.get("lifecycleState") or job.get("state") or "").upper()
            if job_key and state != "DELETING":
                self._request(
                    "DELETE",
                    f"/workspaces/{workspace_key}/jobs/{job_key}",
                    allow_not_found=True,
                    phase="content",
                )
        if self._lab_jobs(workspace_key, job_name):
            raise AidpProvisionPending(
                "Lab workflow deletion is still in progress.", "cleanup"
            )

    def _cleanup_agent(self, workspace_key: str, agent_name: str) -> None:
        for agent in self._agents(workspace_key, agent_name):
            agent_key = str(agent.get("key") or agent.get("id") or "")
            if agent_key:
                self._request(
                    "DELETE",
                    f"/workspaces/{workspace_key}/agents/{agent_key}",
                    allow_not_found=True,
                    phase="content",
                )
        if self._agents(workspace_key, agent_name):
            raise AidpProvisionPending("Agent deletion is still in progress.", "cleanup")

    def _shared_schemas(
        self,
        catalog_key: str,
    ) -> dict[str, dict[str, Any]]:
        schemas = self._list(
            "/schemas",
            params={"catalogKey": catalog_key},
            phase="schemas",
        )
        result: dict[str, dict[str, Any]] = {}
        for layer in LAYER_PREFIXES:
            name = schema_name(layer)
            matches = [schema for schema in schemas if self._resource_name(schema) == name]
            if len(matches) > 1:
                raise AidpProvisionError(f"AIDP has duplicate schemas named {name}.")
            if matches:
                result[layer] = matches[0]
        return result

    def _legacy_participant_schemas(
        self,
        catalog_key: str,
        key: str,
    ) -> list[dict[str, Any]]:
        expected = {f"{key}_{layer}" for layer in LAYER_PREFIXES}
        return [
            schema
            for schema in self._list(
                "/schemas", params={"catalogKey": catalog_key}, phase="schemas"
            )
            if self._resource_name(schema) in expected
        ]

    def _schema_tables(
        self,
        catalog_key: str,
        schema_key: str,
    ) -> list[dict[str, Any]]:
        return self._list(
            "/tables",
            params={"catalogKey": catalog_key, "schemaKey": schema_key},
            phase="schemas",
        )

    def _cleanup_lab_tables(
        self, catalog_key: str, key: str, lab_id: str
    ) -> None:
        pack = load_lab_pack(lab_id)
        schemas = self._shared_schemas(catalog_key)
        for layer, logical_names in pack.tables.items():
            schema = schemas.get(layer)
            if schema is None:
                continue
            if not schema.get("key"):
                continue
            schema_key = str(schema["key"])
            expected = {
                table_name(key, lab_id, logical_name)
                for logical_name in logical_names
            }
            for table in self._schema_tables(catalog_key, schema_key):
                table_key = table.get("key")
                if table_key and self._resource_name(table) in expected:
                    self._request(
                        "DELETE",
                        f"/tables/{table_key}",
                        allow_not_found=True,
                        phase="schemas",
                    )
        for layer, logical_names in pack.tables.items():
            schema = self._shared_schemas(catalog_key).get(layer)
            if schema is None:
                continue
            schema_key = str(schema.get("key") or "")
            expected = {
                table_name(key, lab_id, logical_name)
                for logical_name in logical_names
            }
            if schema_key and any(
                self._resource_name(table) in expected
                for table in self._schema_tables(catalog_key, schema_key)
            ):
                raise AidpProvisionPending(
                    "Lab table deletion is still in progress.", "cleanup"
                )

    def _cleanup_legacy_tables(self, catalog_key: str, key: str) -> None:
        for schema in self._legacy_participant_schemas(catalog_key, key):
            schema_key = str(schema.get("key") or "")
            if schema_key:
                for table in self._schema_tables(catalog_key, schema_key):
                    table_key = str(table.get("key") or "")
                    if table_key:
                        self._request(
                            "DELETE",
                            f"/tables/{table_key}",
                            allow_not_found=True,
                            phase="schemas",
                        )
                if self._schema_tables(catalog_key, schema_key):
                    raise AidpProvisionPending(
                        "Legacy participant table deletion is still in progress.", "schemas"
                    )

    def _cleanup_legacy_schemas(self, catalog_key: str, key: str) -> None:
        for schema in self._legacy_participant_schemas(catalog_key, key):
            schema_key = str(schema.get("key") or "")
            state = str(schema.get("lifecycleState") or "").upper()
            if not schema_key or state == "DELETING":
                continue
            self._request(
                "DELETE",
                f"/schemas/{schema_key}",
                allow_not_found=True,
                phase="schemas",
            )
        if self._legacy_participant_schemas(catalog_key, key):
            raise AidpProvisionPending(
                "Legacy participant schema deletion is still in progress.", "schemas"
            )

    def _cleanup_private_catalog(self, key: str) -> None:
        if re.fullmatch(r"u[1-9][0-9]*", key) is None:
            return
        name = f"{key}_aidp"
        catalog = self._catalog(name, allow_missing=True)
        if catalog is None:
            return
        catalog_key = str(catalog.get("key") or "")
        if not catalog_key:
            raise AidpProvisionPending(
                "The participant catalog identifier is not ready.", "cleanup"
            )
        self._request(
            "DELETE", f"/catalogs/{catalog_key}", allow_not_found=True, phase="schemas"
        )
        if self._catalog(name, allow_missing=True) is not None:
            raise AidpProvisionPending(
                "Participant catalog deletion is still in progress.", "cleanup"
            )

    def _cleanup_lab_object_storage(self, key: str, lab_id: str) -> None:
        prefixes = [
            f"{prefix}/users/{key}/{lab_id}/"
            for prefix in LAYER_PREFIXES.values()
        ]
        for prefix in prefixes:
            self._delete_object_storage_prefix(prefix)
        if any(
            self._object_storage_prefix_exists(prefix) for prefix in prefixes
        ):
            raise AidpProvisionPending(
                "Lab Object Storage cleanup is still in progress.", "cleanup"
            )

    def _cleanup_participant_object_storage(self, key: str) -> None:
        prefixes = [
            f"{prefix}/users/{key}/"
            for prefix in LAYER_PREFIXES.values()
        ]
        for prefix in prefixes:
            self._delete_object_storage_prefix(prefix)
        if any(self._object_storage_prefix_exists(prefix) for prefix in prefixes):
            raise AidpProvisionPending(
                "Participant Object Storage cleanup is still in progress.", "cleanup"
            )

    def _delete_workspace_path(
        self,
        workspace_key: str,
        path: str,
        pending_message: str,
    ) -> None:
        body, headers = self._workspace_object(
            workspace_key, path, phase="content"
        )
        if not self._workspace_object_exists(body, headers):
            return
        self._request(
            "DELETE",
            f"/workspaces/{workspace_key}/objects/{quote(path, safe='')}",
            headers={"Accept": "*/*"},
            allow_not_found=True,
            phase="content",
        )
        body, headers = self._workspace_object(
            workspace_key, path, phase="content"
        )
        if self._workspace_object_exists(body, headers):
            raise AidpProvisionPending(pending_message, "content")

    def _cleanup_lab(
        self,
        workspace_key: str,
        key: str,
        lab_id: str,
        state: dict[str, Any],
        *,
        preserve_workspace: bool = False,
    ) -> None:
        workspace_path = str(state.get("workspace_path") or "")
        validated = {
            "layout_version": LAYOUT_VERSION,
            "participant_key": key,
            "labs": {lab_id: state},
        }
        self._manifest_labs(validated, key)
        self._cleanup_lab_job(workspace_key, str(state.get("job_name") or f"wf_{key}_{lab_id}"))
        catalog_name = str(state.get("catalog_name") or catalog_name_for(key))
        catalog = self._catalog(catalog_name, allow_missing=True)
        if catalog is not None:
            self._cleanup_lab_tables(str(catalog["key"]), key, lab_id)
        self._cleanup_lab_object_storage(key, lab_id)
        if not preserve_workspace:
            self._delete_workspace_path(
                workspace_key,
                workspace_path,
                "Lab workspace deletion is still in progress.",
            )

    def _cleanup_user(self, owner_key: str, preserve_manifest: bool = False) -> None:
        workspace_key = str(self._workspace()["key"])
        manifest = self._manifest(workspace_key, owner_key)
        participant_roots: set[str] = set()
        if manifest is not None:
            key = self._manifest_participant_key(manifest, owner_key)
            labs = self._manifest_labs(manifest, owner_key)
            for lab_id, state in labs.items():
                participant_roots.add(str(state["workspace_path"]))
                self._cleanup_lab(workspace_key, key, lab_id, state)
        else:
            key = owner_key
        legacy_catalog = self._catalog(LEGACY_CATALOG_NAME, allow_missing=True)
        if legacy_catalog is not None:
            catalog_key = str(legacy_catalog["key"])
            self._cleanup_legacy_tables(catalog_key, key)
            self._cleanup_legacy_schemas(catalog_key, key)
        self._cleanup_private_catalog(key)
        self._cleanup_participant_object_storage(key)
        for participant_root in participant_roots:
            self._delete_workspace_path(
                workspace_key,
                participant_root,
                "Participant workspace deletion is still in progress.",
            )
        self._delete_workspace_path(
            workspace_key,
            f"{LEGACY_WORKSPACE_ROOT}/{owner_key}",
            "Legacy participant workspace deletion is still in progress.",
        )
        if not preserve_manifest:
            self._delete_workspace_path(
                workspace_key,
                self._control_manifest_path(owner_key),
                "Participant control manifest deletion is still in progress.",
            )
        self._delete_workspace_path(
            workspace_key,
            self._legacy_control_manifest_path(owner_key),
            "Legacy participant control manifest deletion is still in progress.",
        )


    async def cleanup_user(self, user_ocid: str) -> None:
        owner_key = participant_owner_key(user_ocid)
        async with self._locks.setdefault(owner_key, asyncio.Lock()):
            await asyncio.to_thread(self._cleanup_user, owner_key)

    def _role(self, name: str) -> dict[str, Any]:
        matches = [item for item in self._list("/roles", params={"displayName": name}, phase="permissions") if self._resource_name(item) == name]
        if len(matches) != 1 or not matches[0].get("key"):
            raise AidpProvisionPending(f"The AIDP role {name} is not ready yet.", "permissions")
        return matches[0]

    def _role_principal_ocids(self, name: str) -> tuple[set[str], set[str]]:
        role = self._role(name)
        pending = [str(role["key"])]
        visited: set[str] = set()
        users: set[str] = set()
        groups: set[str] = set()
        while pending:
            role_key = pending.pop()
            if role_key in visited:
                continue
            if len(visited) >= 100:
                raise AidpProvisionError("The AIDP administrator role hierarchy is too deep.")
            visited.add(role_key)
            details = self._request(
                "GET",
                f"/roles/{quote(role_key, safe='')}",
                phase="permissions",
            )
            assignees = details.get("assignees") if isinstance(details, dict) else None
            if not isinstance(assignees, list):
                raise AidpProvisionError("AIDP returned invalid administrator role assignees.")
            for item in assignees:
                if not isinstance(item, dict):
                    raise AidpProvisionError("AIDP returned an invalid administrator role assignee.")
                principal_type = str(item.get("type") or "").upper()
                target = str(item.get("target") or "")
                if principal_type == "USER" and target.startswith("ocid1.user."):
                    users.add(target)
                elif principal_type == "GROUP" and target.startswith("ocid1.group."):
                    groups.add(target)
                elif principal_type == "ROLE" and target:
                    pending.append(target)
                else:
                    raise AidpProvisionError("AIDP returned an unsupported administrator role assignee.")
        return users, groups

    def _role_user_ocids(self, name: str) -> set[str]:
        return self._role_principal_ocids(name)[0]

    async def platform_admin_user_ocids(self) -> set[str]:
        return await asyncio.to_thread(self._role_user_ocids, "AI_DATA_PLATFORM_ADMIN")

    async def platform_admin_principals(self) -> tuple[set[str], set[str]]:
        return await asyncio.to_thread(
            self._role_principal_ocids, "AI_DATA_PLATFORM_ADMIN"
        )

    async def is_platform_admin(self, user_ocid: str) -> bool:
        return user_ocid in await self.platform_admin_user_ocids()

    @staticmethod
    def _module_manifest_valid(manifest: Any) -> bool:
        if not isinstance(manifest, dict):
            return False
        operation = manifest.get("operation")
        operation_id = operation.get("operation_id") if isinstance(operation, dict) else None
        try:
            canonical_operation_id = str(uuid.UUID(operation_id))
        except (AttributeError, TypeError, ValueError):
            return False
        return bool(
            manifest.get("schema_version") == 1
            and manifest.get("module_id") == GOVERNANCE_MODULE_ID
            and manifest.get("status") in {"installing", "active", "redeploying", "deleting", "error"}
            and isinstance(operation, dict)
            and operation_id == canonical_operation_id
            and operation.get("type") in {"install", "redeploy", "delete"}
        )

    def _module_manifest(self, workspace_key: str) -> dict[str, Any] | None:
        manifest = self._workspace_json(
            workspace_key,
            MODULE_MANIFEST_PATH,
            "The global governance module manifest is invalid; reconcile it before retrying.",
        )
        if manifest is not None and not self._module_manifest_valid(manifest):
            raise AidpProvisionError("The global governance module manifest is invalid; cleanup stopped.")
        return manifest

    def _write_module_manifest(self, workspace_key: str, manifest: dict[str, Any]) -> None:
        manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._upload_file(
            workspace_key,
            MODULE_MANIFEST_PATH,
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            repair_drift=True,
        )

    def _module_status(self) -> dict[str, Any]:
        if self.settings.deployment_mode != "production":
            return _module_payload()
        workspace_key = str(self._workspace()["key"])
        manifest = self._module_manifest(workspace_key)
        if manifest is None:
            return _module_payload()
        effective = dict(manifest)
        effective["enabled"] = self._governance_enabled(workspace_key, manifest)
        return _module_payload(effective)

    def _governance_enabled(self, workspace_key: str, manifest: dict[str, Any]) -> bool:
        workflow_key = str((manifest.get("resources") or {}).get("workflow_key") or "")
        if not workflow_key:
            return False
        try:
            details = self._request(
                "GET",
                f"/workspaces/{workspace_key}/jobs/{quote(workflow_key, safe='')}",
                allow_not_found=True,
                phase="sync",
            )
        except (AidpProvisionError, AidpProvisionPending):
            return False
        continuous = details.get("continuous") if isinstance(details, dict) else None
        return bool(
            isinstance(continuous, dict)
            and str(continuous.get("pauseStatus") or "").upper() == "UNPAUSED"
        )

    async def list_modules(self) -> list[dict[str, Any]]:
        if self.settings.deployment_mode != "production":
            return []
        return [await asyncio.to_thread(self._module_status)]

    def _ensure_governance_bucket(self) -> bool:
        if self.settings.artifacts_bucket_name != GOVERNANCE_BUCKET_NAME:
            raise AidpProvisionError("The governance artifacts bucket must be named oci_artifacts.")
        try:
            self.object_storage.head_bucket(
                self.settings.objectstorage_namespace, self.settings.artifacts_bucket_name
            )
            return False
        except self._oci.exceptions.ServiceError as exc:
            if exc.status != 404:
                raise AidpProvisionError("The fixed governance artifacts bucket is unavailable.") from exc
        self.object_storage.create_bucket(
            self.settings.objectstorage_namespace,
            {"name": self.settings.artifacts_bucket_name, "compartment_id": self.settings.compartment_id},
        )
        raise AidpProvisionPending("The fixed oci_artifacts bucket is being created.", "control")

    def _credential_payload(self) -> dict[str, Any]:
        key_path = Path(str(self._oci_config.get("key_file") or ""))
        if not key_path.is_absolute():
            key_path = Path(self.settings.oci_config_file).parent / key_path
        try:
            private_key = key_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise AidpProvisionError("The dedicated governance OCI credential cannot be loaded.") from exc
        required = {name: str(self._oci_config.get(name) or "") for name in ("tenancy", "user", "fingerprint")}
        if any(not value for value in required.values()) or not private_key.strip():
            raise AidpProvisionError("The dedicated governance OCI credential is incomplete.")
        return {
            "displayName": GOVERNANCE_CREDENTIAL_NAME,
            "credentialDescription": "Dedicated OCI credential for the global governance extension",
            "type": "SECRET_TOKEN",
            "credentialDetails": {
                "credentialType": "SECRET_TOKEN",
                "secretTokenPair": [
                    {"secretKey": name, "secretValue": value}
                    for name, value in (
                        ("tenancy", required["tenancy"]),
                        ("user", required["user"]),
                        ("fingerprint", required["fingerprint"]),
                        ("region", self.settings.aidp_region),
                        ("private_key", private_key),
                    )
                ],
            },
        }

    def _ensure_governance_credential(self) -> tuple[str, bool]:
        matches = [item for item in self._list("/credentials", params={"displayName": GOVERNANCE_CREDENTIAL_NAME}, phase="control") if self._resource_name(item) == GOVERNANCE_CREDENTIAL_NAME]
        if len(matches) > 1:
            raise AidpProvisionError(f"AIDP has duplicate credentials named {GOVERNANCE_CREDENTIAL_NAME}.")
        payload = self._credential_payload()
        if not matches:
            self._request("POST", "/credentials", payload=payload, phase="control")
            matches = [item for item in self._list("/credentials", params={"displayName": GOVERNANCE_CREDENTIAL_NAME}, phase="control") if self._resource_name(item) == GOVERNANCE_CREDENTIAL_NAME]
            if len(matches) != 1:
                raise AidpProvisionPending("AIDP has not published the governance credential yet.", "control")
            return str(matches[0].get("key") or matches[0].get("id") or ""), True
        credential = matches[0]
        if str(credential.get("type") or credential.get("credentialType") or "") != "SECRET_TOKEN":
            raise AidpProvisionError("The existing governance credential has an incompatible type.")
        key = str(credential.get("key") or credential.get("id") or "")
        if not key:
            raise AidpProvisionPending("The governance credential identifier is not ready.", "control")
        self._request("PUT", f"/credentials/{quote(key, safe='')}", payload=payload, phase="control", retry_scope=f"credential:{hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()}")
        return key, False

    def _governance_job_payload(
        self,
        workspace_key: str,
        compute_key: str,
        *,
        job_key: str = "",
        desired_enabled: bool | None,
        paused: bool,
        bootstrap_snapshot: bool = False,
    ) -> tuple[dict[str, Any], bool]:
        notebook_path = f"{MODULE_ROOT}/data_governance_sync.ipynb"
        notebook = governance_sync_notebook(
            namespace=self.settings.objectstorage_namespace,
            platform_id=self.settings.aidp_platform_id,
            region=self.settings.aidp_region,
            desired_enabled=desired_enabled,
            bootstrap_snapshot=bootstrap_snapshot,
            workspace_key=workspace_key,
            job_key=job_key,
        )
        changed = self._upload_notebook(workspace_key, notebook_path, notebook, repair_drift=True)
        return {
            "name": GOVERNANCE_JOB_NAME,
            "path": MODULE_ROOT,
            "description": "Continuous 30-second Master Catalog metadata reconciliation",
            "maxConcurrentRuns": 1,
            "continuous": {"pauseStatus": "PAUSED" if paused else "UNPAUSED"},
            "jobClusters": [{"clusterKey": compute_key}],
            "tasks": [{
                "type": "NOTEBOOK_TASK",
                "taskKey": "sync_master_catalog",
                "dependsOn": [],
                "runIf": "ALL_SUCCESS",
                "maxRetries": 0,
                "isRetryOnTimeout": False,
                "notebookPath": notebook_path,
                "cluster": {"clusterKey": compute_key},
                "parameters": [],
            }],
        }, changed

    def _ensure_governance_job(
        self,
        workspace_key: str,
        compute_key: str,
        *,
        desired_enabled: bool | None,
        paused: bool,
        bootstrap_snapshot: bool = False,
    ) -> tuple[str, bool]:
        job_key = self._job_key(workspace_key, GOVERNANCE_JOB_NAME)
        payload, changed = self._governance_job_payload(
            workspace_key,
            compute_key,
            job_key=job_key,
            desired_enabled=desired_enabled,
            paused=paused,
            bootstrap_snapshot=bootstrap_snapshot,
        )
        if job_key:
            details = self._request("GET", f"/workspaces/{workspace_key}/jobs/{job_key}", allow_not_found=True, phase="sync")
            if self._job_contract_is_visible(details, payload, compute_key) and not changed:
                return job_key, False
        if not job_key:
            job_key = self._create_job(workspace_key, payload)
            changed = True
            payload, notebook_changed = self._governance_job_payload(
                workspace_key,
                compute_key,
                job_key=job_key,
                desired_enabled=desired_enabled,
                paused=paused,
                bootstrap_snapshot=bootstrap_snapshot,
            )
            changed = changed or notebook_changed
        self._publish_job(workspace_key, job_key, payload, compute_key)
        return job_key, True

    @staticmethod
    def _timestamp(value: Any) -> float | None:
        if isinstance(value, (int, float)):
            return float(value) / (1000 if value > 10_000_000_000 else 1)
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None

    def _governance_sync_marker(
        self, workspace_key: str, job_key: str, requested_at: str
    ) -> tuple[str, float | None]:
        details = self._request(
            "GET",
            f"/workspaces/{workspace_key}/jobs/{job_key}",
            allow_not_found=True,
            phase="sync",
        )
        details = details if isinstance(details, dict) else {}
        revision = str(
            details.get("version")
            or details.get("revision")
            or details.get("jobVersion")
            or ""
        )
        threshold = self._timestamp(requested_at) or self._timestamp(
            details.get("timeUpdated")
            or details.get("timePublished")
            or details.get("timeCreated")
        )
        return revision, threshold

    def _governance_run_matches(
        self, run: dict[str, Any], revision: str, threshold: float | None
    ) -> bool:
        run_revision = str(run.get("jobVersion") or run.get("revision") or "")
        if revision and run_revision:
            return run_revision == revision
        run_time = self._timestamp(
            run.get("timeStarted") or run.get("startTime") or run.get("timeCreated")
        )
        return bool(
            threshold is not None and run_time is not None and run_time >= threshold
        )

    def _governance_run_order(self, run: dict[str, Any]) -> tuple[float, str]:
        return (
            self._timestamp(
                run.get("timeStarted") or run.get("startTime") or run.get("timeCreated")
            )
            or 0,
            str(run.get("key") or run.get("id") or ""),
        )

    @staticmethod
    def _governance_run_state(run: dict[str, Any]) -> str:
        return str(
            run.get("runState")
            or run.get("lifecycleState")
            or run.get("state")
            or ""
        ).upper()

    def _successful_governance_sync(
        self, workspace_key: str, job_key: str, *, requested_at: str = ""
    ) -> bool:
        runs = self._list(f"/workspaces/{workspace_key}/jobs/{job_key}/runs", phase="sync")
        revision, threshold = self._governance_sync_marker(
            workspace_key, job_key, requested_at
        )
        current_runs = [
            run
            for run in runs
            if self._governance_run_matches(run, revision, threshold)
        ]
        if not current_runs:
            return False
        state = self._governance_run_state(
            max(current_runs, key=self._governance_run_order)
        )
        if state in {"FAILED", "ERROR", "CANCELED", "CANCELLED"}:
            raise AidpProvisionError("The governance metadata synchronization failed closed.")
        return state in {"SUCCESS", "SUCCEEDED"}

    @classmethod
    def _role_permission_matches(
        cls,
        item: dict[str, Any],
        role_name: str,
        permission: str,
        inheritable: bool | None = None,
        *,
        include_columns: bool = False,
    ) -> bool:
        grantee, grantee_type = cls._permission_grantee(item)
        values = cls._permission_values(item)
        return bool(
            grantee == role_name
            and grantee_type == "ROLE"
            and values == {permission}
            and item.get("isInherited") is not True
            and (inheritable is None or item.get("isPermissionsInheritable") is inheritable)
            and (
                not include_columns
                or not (item.get("columns") or item.get("includeColumns"))
                and not item.get("excludeColumns")
            )
        )

    @classmethod
    def _permission_signature(
        cls,
        item: dict[str, Any],
        *,
        include_columns: bool,
        inheritable: bool,
    ) -> tuple[Any, ...]:
        grantee, grantee_type = cls._permission_grantee(item)
        return (
            grantee,
            grantee_type,
            frozenset(cls._permission_values(item)),
            item.get("isPermissionsInheritable") if inheritable else None,
            frozenset(item.get("columns") or item.get("includeColumns") or [])
            if include_columns
            else frozenset(),
            frozenset(item.get("excludeColumns") or [])
            if include_columns
            else frozenset(),
        )

    def _revoke_direct_permission(
        self,
        resource_path: str,
        revoke_key: str,
        item: dict[str, Any],
        *,
        include_columns: bool,
        inheritable: bool,
    ) -> None:
        grantee, grantee_type = self._permission_grantee(item)
        permissions = sorted(self._permission_values(item))
        if grantee_type not in {"USER", "ROLE", "GROUP"} or not grantee or not permissions:
            raise AidpProvisionError("AIDP returned an invalid direct governance permission.")
        details: dict[str, Any] = {
            "assignees": {"type": grantee_type, "targets": [grantee]},
            "permissions": permissions,
        }
        if include_columns:
            details.update(
                includeColumns=sorted(
                    item.get("columns") or item.get("includeColumns") or []
                ),
                excludeColumns=sorted(item.get("excludeColumns") or []),
            )
        if inheritable:
            details["isPermissionsInheritable"] = (
                item.get("isPermissionsInheritable") is not False
            )
        self._request(
            "POST",
            f"{resource_path}/actions/managePermission",
            payload={revoke_key: details},
            phase="permissions",
        )

    def _assert_no_forbidden_direct_permissions(
        self,
        resource_path: str,
        forbidden: set[str],
        *,
        allowed_grantees: set[tuple[str, str]],
        inheritable_only: bool = False,
    ) -> None:
        if any(
            item.get("isInherited") is not True
            and (
                not inheritable_only
                or item.get("isPermissionsInheritable") is not False
            )
            and self._permission_grantee(item) not in allowed_grantees
            and self._permission_values(item).intersection(forbidden)
            for item in self._list(f"{resource_path}/permissions", phase="permissions")
        ):
            raise AidpProvisionError(
                "A shared workspace ancestor grants governance edit access outside "
                "AI_DATA_PLATFORM_ADMIN; remove that grant before retrying."
            )

    @classmethod
    def _assert_no_inherited_governance_editor(
        cls,
        permissions: list[dict[str, Any]],
        allowed_grantees: set[tuple[str, str]] | None = None,
    ) -> None:
        allowed = allowed_grantees or {("AI_DATA_PLATFORM_ADMIN", "ROLE")}
        for item in permissions:
            if (
                item.get("isInherited") is True
                and cls._permission_grantee(item) not in allowed
                and cls._permission_values(item).intersection({"MANAGE", "ADMIN"})
            ):
                raise AidpProvisionError(
                    "A non-administrator inherits governance edit access from a parent resource."
                )

    def _reconcile_role_permissions_exact(
        self,
        resource_path: str,
        assignment_key: str,
        revoke_key: str,
        expected: tuple[tuple[str, str, bool | None], ...],
        *,
        include_columns: bool = False,
        inheritable: bool = False,
        reject_inherited_editors: bool = False,
        allowed_inherited_editors: set[tuple[str, str]] | None = None,
    ) -> bool:
        permissions_path = f"{resource_path}/permissions"
        current = self._list(permissions_path, phase="permissions")
        if reject_inherited_editors:
            self._assert_no_inherited_governance_editor(
                current, allowed_inherited_editors
            )
        direct = [item for item in current if item.get("isInherited") is not True]
        expected_signatures = {
            (
                role_name,
                "ROLE",
                frozenset({permission}),
                expected_inheritable if inheritable else None,
                frozenset(),
                frozenset(),
            )
            for role_name, permission, expected_inheritable in expected
        }
        signatures = {
            self._permission_signature(
                item,
                include_columns=include_columns,
                inheritable=inheritable,
            )
            for item in direct
        }
        if len(direct) == len(expected_signatures) and signatures == expected_signatures:
            return False
        for item in direct:
            self._revoke_direct_permission(
                resource_path,
                revoke_key,
                item,
                include_columns=include_columns,
                inheritable=inheritable,
            )
        remaining = [
            item
            for item in self._list(permissions_path, phase="permissions")
            if item.get("isInherited") is not True
        ]
        if remaining:
            raise AidpProvisionPending(
                "AIDP is still revoking divergent governance permissions.",
                "permissions",
            )
        for role_name, permission, expected_inheritable in expected:
            self._ensure_role_permission(
                resource_path,
                assignment_key,
                role_name,
                permission,
                inheritable=expected_inheritable,
                include_columns=include_columns,
            )
        return True

    def _ensure_role_permission(
        self,
        resource_path: str,
        assignment_key: str,
        role_name: str,
        permission: str,
        *,
        inheritable: bool | None = None,
        include_columns: bool = False,
    ) -> bool:
        path = f"{resource_path}/permissions"
        current = self._list(path, phase="permissions")
        matches = [
            item
            for item in current
            if item.get("isInherited") is not True
            and self._permission_grantee(item) == (role_name, "ROLE")
        ]
        if any(
            not self._role_permission_matches(
                item,
                role_name,
                permission,
                inheritable,
                include_columns=include_columns,
            )
            for item in matches
        ) or len(matches) > 1:
            raise AidpProvisionError(f"Role {role_name} has a conflicting direct governance permission.")
        if any(
            self._role_permission_matches(
                item,
                role_name,
                permission,
                inheritable,
                include_columns=include_columns,
            )
            for item in matches
        ):
            return False
        assignment: dict[str, Any] = {
            "assignees": {"type": "ROLE", "targets": [role_name]},
            "permissions": [permission],
        }
        if inheritable is not None:
            assignment["isPermissionsInheritable"] = inheritable
        if include_columns:
            assignment.update(includeColumns=[], excludeColumns=[])
        self._request(
            "POST",
            f"{resource_path}/actions/managePermission",
            payload={assignment_key: assignment},
            phase="permissions",
        )
        current = self._list(path, phase="permissions")
        if not any(
            self._role_permission_matches(
                item,
                role_name,
                permission,
                inheritable,
                include_columns=include_columns,
            )
            for item in current
        ):
            raise AidpProvisionPending("AIDP has not applied the governance permission yet.", "permissions")
        return True

    def _governance_agent_artifacts(self) -> tuple[bytes, bytes, str]:
        if not all((self.settings.agent_model_id, self.settings.aidp_region, self.settings.compartment_id, self.settings.aidp_platform_id)):
            raise AidpProvisionError("The selected Agent model and regional runtime are incomplete.")
        source = agent_source(
            model_id=self.settings.agent_model_id,
            region=self.settings.aidp_region,
            compartment_id=self.settings.compartment_id,
            platform_id=self.settings.aidp_platform_id,
        )
        source_hash = hashlib.sha256(source).hexdigest()
        descriptor = json.dumps({
            "schema_version": 1,
            "module_id": GOVERNANCE_MODULE_ID,
            "entry_file": "governance_agent.py",
            "entry_sha256": source_hash,
            "tools": ["catalog_inventory", "catalog_lineage"],
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return source, descriptor, source_hash

    @staticmethod
    def _deployment_marker(item: dict[str, Any]) -> str:
        return json.dumps(
            [
                item.get("version") or item.get("revision") or item.get("deploymentVersion"),
                item.get("timeUpdated") or item.get("updatedAt"),
                item.get("etag"),
            ],
            separators=(",", ":"),
        )

    @staticmethod
    def _governance_redeploy_revision(
        manifest: dict[str, Any], source_hash: str
    ) -> str:
        if manifest.get("status") != "redeploying":
            return ""
        operation = manifest.get("operation") or {}
        return f"{operation.get('operation_id')}:{source_hash}"

    @staticmethod
    def _governance_redeploy_trigger(
        resources: dict[str, Any], redeploy_revision: str
    ) -> str:
        requested = str(resources.get("deployment_redeploy_requested") or "")
        completed = str(resources.get("deployment_revision") or "")
        if redeploy_revision and completed != redeploy_revision and requested != redeploy_revision:
            return redeploy_revision
        return ""

    def _record_governance_redeploy(
        self,
        resources: dict[str, Any],
        redeploy_revision: str,
        trigger_revision: str,
        visible_deployments: list[dict[str, Any]],
        deployment: dict[str, Any],
    ) -> None:
        if trigger_revision:
            baseline = (
                self._deployment_marker(visible_deployments[0])
                if visible_deployments
                else ""
            )
            resources.update(
                deployment_redeploy_requested=trigger_revision,
                deployment_redeploy_baseline=baseline,
            )
            return
        completed_revision = str(resources.get("deployment_revision") or "")
        if not redeploy_revision or completed_revision == redeploy_revision:
            return
        current_marker = self._deployment_marker(deployment)
        baseline = str(resources.get("deployment_redeploy_baseline") or "")
        if not baseline or current_marker == baseline:
            raise AidpProvisionPending(
                "The global governance Agent redeploy revision is not visible yet.",
                "agent",
            )
        resources["deployment_revision"] = redeploy_revision
        resources.pop("deployment_redeploy_requested", None)
        resources.pop("deployment_redeploy_baseline", None)

    def _ensure_global_agent_deployment(
        self,
        workspace_key: str,
        compute_key: str,
        agent_key: str,
        manifest: dict[str, Any],
        source_hash: str,
    ) -> tuple[dict[str, Any], bool]:
        resources = manifest.setdefault("resources", {})
        redeploy_revision = self._governance_redeploy_revision(manifest, source_hash)
        deployment_path = f"/workspaces/{workspace_key}/agents/{agent_key}/deployments"
        visible_deployments = self._list(deployment_path, phase="deployment")
        if len(visible_deployments) > 1:
            raise AidpProvisionError("AIDP has duplicate global governance Agent deployments.")
        trigger_revision = self._governance_redeploy_trigger(
            resources, redeploy_revision
        )
        deployment, changed = self._ensure_agent_deployment(
            workspace_key,
            agent_key,
            compute_key,
            GOVERNANCE_AGENT_NAME,
            redeploy_revision=trigger_revision,
        )
        self._record_governance_redeploy(
            resources,
            redeploy_revision,
            trigger_revision,
            visible_deployments,
            deployment,
        )
        return deployment, changed

    def _ensure_global_agent(self, workspace_key: str, manifest: dict[str, Any]) -> bool:
        source, descriptor, source_hash = self._governance_agent_artifacts()
        compute, compute_changed = self._ensure_agent_compute(workspace_key)
        agent_key, agent_changed = self._ensure_agent(
            workspace_key,
            str(compute["key"]),
            GOVERNANCE_AGENT_NAME,
            f"{MODULE_ROOT}/agent",
            source,
            descriptor,
            repair_drift=True,
        )
        resources = manifest.setdefault("resources", {})
        deployment, deployment_changed = self._ensure_global_agent_deployment(
            workspace_key,
            str(compute["key"]),
            agent_key,
            manifest,
            source_hash,
        )
        resources.update({
            "agent_key": agent_key,
            "agent_compute_key": str(compute["key"]),
            "deployment_key": str(deployment.get("key") or deployment.get("id") or ""),
            "agent_source_hash": source_hash,
        })
        return any((compute_changed, agent_changed, deployment_changed))

    def _ensure_global_agent_permissions(self, workspace_key: str, manifest: dict[str, Any]) -> bool:
        resources = manifest.get("resources") or {}
        agent_key = str(resources.get("agent_key") or "")
        compute_key = str(resources.get("agent_compute_key") or "")
        if not agent_key or not compute_key:
            raise AidpProvisionError("The governance Agent manifest is incomplete.")
        self._role("AIDP_DEVELOPER")
        admin_users, admin_groups = self._role_principal_ocids(
            "AI_DATA_PLATFORM_ADMIN"
        )
        admin_grantees = {
            ("AI_DATA_PLATFORM_ADMIN", "ROLE"),
            *((user_ocid, "USER") for user_ocid in admin_users),
            *((group_ocid, "GROUP") for group_ocid in admin_groups),
        }
        workspace_path = f"/workspaces/{workspace_key}"
        self._assert_no_forbidden_direct_permissions(
            workspace_path,
            {"ADMINISTRATOR"},
            allowed_grantees=admin_grantees,
        )
        workspace_root_key = self._workspace_object_key(workspace_key, WORKSPACE_ROOT)
        workspace_root_path = (
            f"/workspaces/{workspace_key}/objects/{quote(workspace_root_key, safe='')}"
        )
        self._assert_no_forbidden_direct_permissions(
            workspace_root_path,
            {"READ", "USE", "MANAGE", "ADMIN"},
            allowed_grantees=admin_grantees,
            inheritable_only=True,
        )
        changed = False
        for path, expected in (
            (CONTROL_ROOT, (("AI_DATA_PLATFORM_ADMIN", "ADMIN", True),)),
            (MODULE_CONTROL_ROOT, ()),
        ):
            object_key = self._workspace_object_key(workspace_key, path)
            changed = self._reconcile_role_permissions_exact(
                f"/workspaces/{workspace_key}/objects/{quote(object_key, safe='')}",
                "assignWorkspaceObjectPermissionDetails",
                "revokeWorkspaceObjectPermissionDetails",
                expected,
                inheritable=True,
                reject_inherited_editors=True,
                allowed_inherited_editors=admin_grantees,
            ) or changed
        agent_path = f"/workspaces/{workspace_key}/agents/{agent_key}"
        changed = self._reconcile_role_permissions_exact(
            agent_path,
            "assignAgentPermissionDetails",
            "revokeAgentPermissionDetails",
            (
                ("AIDP_DEVELOPER", "USE", None),
                ("AI_DATA_PLATFORM_ADMIN", "ADMIN", None),
            ),
            include_columns=True,
            reject_inherited_editors=True,
            allowed_inherited_editors=admin_grantees,
        ) or changed
        compute_path = f"/workspaces/{workspace_key}/clusters/{compute_key}"
        changed = self._reconcile_role_permissions_exact(
            compute_path,
            "assignClusterPermissionDetails",
            "revokeClusterPermissionDetails",
            (),
        ) or changed
        module_object_key = self._workspace_object_key(workspace_key, MODULE_ROOT)
        module_path = f"/workspaces/{workspace_key}/objects/{quote(module_object_key, safe='')}"
        changed = self._reconcile_role_permissions_exact(
            module_path,
            "assignWorkspaceObjectPermissionDetails",
            "revokeWorkspaceObjectPermissionDetails",
            (("AI_DATA_PLATFORM_ADMIN", "ADMIN", True),),
            inheritable=True,
            reject_inherited_editors=True,
            allowed_inherited_editors=admin_grantees,
        ) or changed
        for path, is_folder in (
            (MODULE_MANIFEST_PATH, False),
            (f"{MODULE_ROOT}/agent", True),
            (f"{MODULE_ROOT}/agent/governance_agent.py", False),
            (f"{MODULE_ROOT}/agent/requirements.txt", False),
            (f"{MODULE_ROOT}/agent/agent-manifest.json", False),
            (f"{MODULE_ROOT}/data_governance_sync.ipynb", False),
        ):
            object_key = self._workspace_object_key(workspace_key, path)
            changed = self._reconcile_role_permissions_exact(
                f"/workspaces/{workspace_key}/objects/{quote(object_key, safe='')}",
                "assignWorkspaceObjectPermissionDetails",
                "revokeWorkspaceObjectPermissionDetails",
                (),
                inheritable=is_folder,
                reject_inherited_editors=True,
                allowed_inherited_editors=admin_grantees,
            ) or changed
        return changed

    def _new_module_manifest(self, operation_id: str, operation_type: str) -> dict[str, Any]:
        status_value = "installing" if operation_type == "install" else f"{operation_type}ing"
        if operation_type == "delete":
            status_value = "deleting"
        return {
            "schema_version": 1,
            "module_id": GOVERNANCE_MODULE_ID,
            "status": status_value,
            "phase": "verify" if operation_type == "install" else ("disable" if operation_type == "delete" else "control"),
            "enabled": False,
            "operation": {"operation_id": operation_id, "type": operation_type, "phase": "started"},
            "resources": {},
        }

    def _governance_workspace(self) -> str:
        if self.settings.deployment_mode != "production":
            raise AidpProvisionConflict("Governance modules are available only in production mode")
        return str(self._workspace()["key"])

    def _start_governance_install(
        self, workspace_key: str, operation_id: str, operation_type: str
    ) -> dict[str, Any]:
        if operation_type != "install":
            raise AidpProvisionConflict("The governance module is not installed")
        self._ensure_workspace_layout(
            workspace_key,
            (
                WORKSPACE_ROOT,
                CONTROL_ROOT,
                MODULE_CONTROL_ROOT,
                MODULE_ROOT,
                f"{MODULE_ROOT}/agent",
            ),
        )
        manifest = self._new_module_manifest(operation_id, operation_type)
        self._write_module_manifest(workspace_key, manifest)
        return manifest

    def _resume_governance_operation(
        self,
        workspace_key: str,
        manifest: dict[str, Any],
        operation_id: str,
        operation_type: str,
    ) -> None:
        if manifest["status"] != "error":
            return
        current = manifest["operation"]
        if current.get("operation_id") != operation_id or current.get("type") != operation_type:
            raise AidpProvisionConflict(
                "The failed governance operation must resume with its original operation_id"
            )
        manifest["status"] = "installing" if operation_type == "install" else "redeploying"
        current["phase"] = "resumed"
        manifest.pop("error_code", None)
        self._write_module_manifest(workspace_key, manifest)

    @staticmethod
    def _governance_operation_complete(
        manifest: dict[str, Any], operation_id: str, operation_type: str
    ) -> bool:
        if operation_type == "install" and manifest["status"] == "active":
            return True
        current = manifest["operation"]
        return bool(
            manifest["status"] == "active"
            and current.get("operation_id") == operation_id
            and current.get("type") == operation_type
            and current.get("phase") == "complete"
        )

    @staticmethod
    def _assert_governance_operation_available(
        manifest: dict[str, Any], operation_id: str, operation_type: str
    ) -> None:
        if manifest["status"] in {"active", "error"}:
            return
        current = manifest["operation"]
        if current.get("type") == "install" and operation_type == "install":
            if current.get("operation_id") != operation_id:
                raise AidpProvisionPending(
                    "The existing global governance installation is still in progress.",
                    str(manifest.get("phase") or "control"),
                )
            return
        if current.get("operation_id") != operation_id or current.get("type") != operation_type:
            raise AidpProvisionConflict(
                "Another global governance module operation is already in progress"
            )

    def _begin_governance_repair(
        self,
        workspace_key: str,
        manifest: dict[str, Any],
        operation_id: str,
        operation_type: str,
    ) -> dict[str, Any]:
        previous_enabled = self._governance_enabled(workspace_key, manifest)
        resources = dict(manifest.get("resources") or {})
        replacement = self._new_module_manifest(operation_id, operation_type)
        replacement.update(
            enabled=previous_enabled,
            previous_enabled=previous_enabled,
            resources=resources,
        )
        self._write_module_manifest(workspace_key, replacement)
        return replacement

    def _prepare_governance_reconciliation(
        self, workspace_key: str, operation_id: str, operation_type: str
    ) -> tuple[dict[str, Any], bool]:
        manifest = self._module_manifest(workspace_key)
        if manifest is None:
            return self._start_governance_install(
                workspace_key, operation_id, operation_type
            ), False
        self._resume_governance_operation(
            workspace_key, manifest, operation_id, operation_type
        )
        if self._governance_operation_complete(manifest, operation_id, operation_type):
            return manifest, True
        self._assert_governance_operation_available(
            manifest, operation_id, operation_type
        )
        if manifest["status"] in {"active", "error"} and operation_type in {"redeploy", "delete"}:
            manifest = self._begin_governance_repair(
                workspace_key, manifest, operation_id, operation_type
            )
        return manifest, False

    def _reconcile_governance_verify(
        self, workspace_key: str, manifest: dict[str, Any], _operation_type: str
    ) -> None:
        self._ensure_governance_bucket()
        manifest["phase"] = "control"
        self._write_module_manifest(workspace_key, manifest)

    def _reconcile_governance_control(
        self, workspace_key: str, manifest: dict[str, Any], operation_type: str
    ) -> None:
        self._ensure_governance_bucket()
        credential_key, _ = self._ensure_governance_credential()
        shared_compute = self._shared_compute(workspace_key)
        previous_enabled = bool(manifest.get("previous_enabled"))
        sync_requested_at = datetime.now(timezone.utc).isoformat()
        job_key, changed = self._ensure_governance_job(
            workspace_key,
            str(shared_compute["key"]),
            desired_enabled=None,
            paused=(operation_type == "redeploy" and not previous_enabled),
            bootstrap_snapshot=(operation_type == "install" or previous_enabled),
        )
        resources = manifest["resources"]
        resources.update(credential_key=credential_key, workflow_key=job_key)
        if changed:
            resources["sync_requested_at"] = sync_requested_at
        manifest["phase"] = (
            "agent"
            if operation_type == "redeploy" and not previous_enabled
            else "sync"
        )
        self._write_module_manifest(workspace_key, manifest)
        if changed:
            raise AidpProvisionPending(
                "The governance tables and first metadata snapshot are starting.",
                "sync",
            )

    def _reconcile_governance_sync(
        self, workspace_key: str, manifest: dict[str, Any], _operation_type: str
    ) -> None:
        resources = manifest["resources"]
        job_key = str(resources.get("workflow_key") or "")
        if not job_key or not self._successful_governance_sync(
            workspace_key,
            job_key,
            requested_at=str(resources.get("sync_requested_at") or ""),
        ):
            raise AidpProvisionPending(
                "The first governance metadata snapshot is still running.", "sync"
            )
        manifest["phase"] = "agent"
        self._write_module_manifest(workspace_key, manifest)

    def _reconcile_governance_agent(
        self, workspace_key: str, manifest: dict[str, Any], _operation_type: str
    ) -> None:
        if self._ensure_global_agent(workspace_key, manifest):
            self._write_module_manifest(workspace_key, manifest)
            raise AidpProvisionPending(
                "The global governance Agent deployment is starting.", "agent"
            )
        manifest["phase"] = "permissions"
        self._write_module_manifest(workspace_key, manifest)

    def _reconcile_governance_permissions(
        self, workspace_key: str, manifest: dict[str, Any], _operation_type: str
    ) -> None:
        if self._ensure_global_agent_permissions(workspace_key, manifest):
            raise AidpProvisionPending(
                "The exact governance Agent role permissions are being applied.",
                "permissions",
            )
        manifest["phase"] = "activation"
        self._write_module_manifest(workspace_key, manifest)

    def _reconcile_governance_activation(
        self, workspace_key: str, manifest: dict[str, Any], operation_type: str
    ) -> None:
        desired = (
            True
            if operation_type == "install"
            else bool(manifest.get("previous_enabled", manifest.get("enabled")))
        )
        shared_compute = self._shared_compute(workspace_key)
        sync_requested_at = datetime.now(timezone.utc).isoformat()
        job_key, changed = self._ensure_governance_job(
            workspace_key,
            str(shared_compute["key"]),
            desired_enabled=desired,
            paused=not desired,
        )
        resources = manifest["resources"]
        resources["workflow_key"] = job_key
        if changed:
            resources["sync_requested_at"] = sync_requested_at
            self._write_module_manifest(workspace_key, manifest)
            raise AidpProvisionPending(
                "The governance activation cycle is starting.", "activation"
            )
        if desired and not self._successful_governance_sync(
            workspace_key,
            job_key,
            requested_at=str(resources.get("sync_requested_at") or ""),
        ):
            raise AidpProvisionPending(
                "The governance activation cycle is still running.", "activation"
            )
        manifest.update(phase="steady", enabled=desired)
        self._write_module_manifest(workspace_key, manifest)

    def _reconcile_governance_steady(
        self, workspace_key: str, manifest: dict[str, Any], _operation_type: str
    ) -> None:
        shared_compute = self._shared_compute(workspace_key)
        _, changed = self._ensure_governance_job(
            workspace_key,
            str(shared_compute["key"]),
            desired_enabled=None,
            paused=not bool(manifest.get("enabled")),
        )
        if changed:
            self._write_module_manifest(workspace_key, manifest)
            raise AidpProvisionPending(
                "The governance workflow is entering steady state.", "steady"
            )
        manifest.update(status="active", phase="active")
        manifest["operation"]["phase"] = "complete"
        self._write_module_manifest(workspace_key, manifest)

    def _reconcile_governance_module(
        self, user_ocid: str, operation_id: str, operation_type: str
    ) -> dict[str, Any]:
        workspace_key = self._governance_workspace()
        manifest, complete = self._prepare_governance_reconciliation(
            workspace_key, operation_id, operation_type
        )
        if complete:
            return _module_payload(manifest)
        handlers = {
            "verify": self._reconcile_governance_verify,
            "control": self._reconcile_governance_control,
            "sync": self._reconcile_governance_sync,
            "agent": self._reconcile_governance_agent,
            "permissions": self._reconcile_governance_permissions,
            "activation": self._reconcile_governance_activation,
            "steady": self._reconcile_governance_steady,
        }
        while handler := handlers.get(str(manifest["phase"])):
            handler(workspace_key, manifest, operation_type)
        return _module_payload(manifest)

    async def install_governance_module(
        self,
        user_ocid: str,
        operation_id: str,
        *,
        role_membership_verified: bool = False,
    ) -> dict[str, Any]:
        if not role_membership_verified and not await self.is_platform_admin(user_ocid):
            raise AidpProvisionConflict("The selected user is not an AI_DATA_PLATFORM_ADMIN")
        async with self._locks.setdefault(GOVERNANCE_MODULE_ID, asyncio.Lock()):
            return await self._run_module_operation(
                self._reconcile_governance_module, user_ocid, operation_id, "install"
            )

    async def redeploy_governance_module(
        self,
        user_ocid: str,
        operation_id: str,
        *,
        role_membership_verified: bool = False,
    ) -> dict[str, Any]:
        if not role_membership_verified and not await self.is_platform_admin(user_ocid):
            raise AidpProvisionConflict("The selected user is not an AI_DATA_PLATFORM_ADMIN")
        async with self._locks.setdefault(GOVERNANCE_MODULE_ID, asyncio.Lock()):
            return await self._run_module_operation(
                self._reconcile_governance_module, user_ocid, operation_id, "redeploy"
            )

    def _record_module_error(self, operation_id: str, operation_type: str, error: BaseException) -> None:
        workspace_key = str(self._workspace()["key"])
        manifest = self._module_manifest(workspace_key)
        if manifest is None:
            return
        operation = manifest.get("operation") or {}
        if operation.get("type") != operation_type:
            return
        if operation.get("operation_id") != operation_id:
            return
        manifest["status"] = "error"
        manifest["error_code"] = hashlib.sha256(
            f"{operation_type}:{manifest.get('phase')}:{type(error).__name__}".encode("utf-8")
        ).hexdigest()[:16]
        operation["phase"] = "error"
        self._write_module_manifest(workspace_key, manifest)

    async def _run_module_operation(
        self, operation: Any, user_ocid: str, operation_id: str, operation_type: str
    ) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(operation, user_ocid, operation_id, operation_type)
        except (AidpProvisionPending, AidpProvisionConflict):
            raise
        except AidpProvisionError as exc:
            try:
                await asyncio.to_thread(self._record_module_error, operation_id, operation_type, exc)
            except AidpProvisionPending:
                pass
            raise
        except Exception as exc:
            try:
                await asyncio.to_thread(self._record_module_error, operation_id, operation_type, exc)
            except (AidpProvisionPending, AidpProvisionError):
                pass
            raise AidpProvisionError("The governance module operation failed closed; retry the same operation_id.") from None

    def _delete_governance_deployments(self, workspace_key: str, agent_key: str) -> None:
        path = f"/workspaces/{workspace_key}/agents/{agent_key}/deployments"
        for deployment in self._list(path, phase="cleanup"):
            key = str(deployment.get("key") or deployment.get("id") or "")
            if key:
                self._request("DELETE", f"{path}/{quote(key, safe='')}", allow_not_found=True, phase="cleanup")
        if self._list(path, phase="cleanup"):
            raise AidpProvisionPending("The governance Agent deployment deletion is still in progress.", "cleanup")

    def _delete_governance_compute(self, workspace_key: str) -> None:
        matches = [item for item in self._list(f"/workspaces/{workspace_key}/clusters", phase="cleanup") if self._resource_name(item) == GOVERNANCE_AGENT_COMPUTE_NAME]
        if len(matches) > 1:
            raise AidpProvisionError("AIDP has duplicate dedicated governance AI compute resources.")
        if matches:
            key = str(matches[0].get("key") or matches[0].get("id") or "")
            if key:
                self._request("DELETE", f"/workspaces/{workspace_key}/clusters/{quote(key, safe='')}", allow_not_found=True, phase="cleanup")
            raise AidpProvisionPending("The dedicated governance AI compute deletion is still in progress.", "cleanup")

    def _delete_governance_credential(self) -> None:
        matches = [item for item in self._list("/credentials", params={"displayName": GOVERNANCE_CREDENTIAL_NAME}, phase="cleanup") if self._resource_name(item) == GOVERNANCE_CREDENTIAL_NAME]
        if len(matches) > 1:
            raise AidpProvisionError("AIDP has duplicate governance credentials.")
        if matches:
            key = str(matches[0].get("key") or matches[0].get("id") or "")
            if key:
                self._request("DELETE", f"/credentials/{quote(key, safe='')}", allow_not_found=True, phase="cleanup")
            raise AidpProvisionPending("The governance credential deletion is still in progress.", "cleanup")

    def _delete_governance_tables(self) -> None:
        catalog = self._catalog(CATALOG_NAME, allow_missing=True)
        if catalog is None:
            return
        schemas = [item for item in self._list("/schemas", params={"catalogKey": str(catalog["key"])}, phase="cleanup") if self._resource_name(item) == "oci_artifacts"]
        if len(schemas) > 1:
            raise AidpProvisionError("AIDP has duplicate governance control schemas.")
        if schemas:
            schema_key = str(schemas[0].get("key") or "")
            tables = [item for item in self._schema_tables(str(catalog["key"]), schema_key) if self._resource_name(item) in GOVERNANCE_TABLES]
            for table in tables:
                key = str(table.get("key") or "")
                if key:
                    self._request("DELETE", f"/tables/{quote(key, safe='')}", allow_not_found=True, phase="cleanup")
            if tables:
                raise AidpProvisionPending("The governance Delta table deletion is still in progress.", "cleanup")

    def _delete_governance_prefixes(self) -> None:
        for table in GOVERNANCE_TABLES:
            prefix = f"oci_artifacts/{table}/"
            start: str | None = None
            while True:
                response = self.object_storage.list_objects(
                    self.settings.objectstorage_namespace, self.settings.artifacts_bucket_name, prefix=prefix, start=start
                )
                for item in response.data.objects:
                    self.object_storage.delete_object(self.settings.objectstorage_namespace, self.settings.artifacts_bucket_name, item.name)
                start = response.data.next_start_with
                if not start:
                    break
            response = self.object_storage.list_objects(
                self.settings.objectstorage_namespace, self.settings.artifacts_bucket_name, prefix=prefix, start=None
            )
            if response.data.objects:
                raise AidpProvisionPending("Governance Object Storage cleanup is still in progress.", "cleanup")

    @staticmethod
    def _completed_governance_deletion(operation_id: str) -> dict[str, Any]:
        result = _module_payload()
        result.update(
            operation_id=operation_id,
            operation_type="delete",
            phase="complete",
        )
        return result

    def _resume_governance_deletion(
        self, workspace_key: str, manifest: dict[str, Any], operation_id: str
    ) -> None:
        if manifest["status"] != "error":
            return
        current = manifest["operation"]
        if current.get("type") == "delete":
            if current.get("operation_id") != operation_id:
                raise AidpProvisionConflict(
                    "The failed governance deletion must resume with its original operation_id"
                )
            manifest["status"] = "deleting"
            current["phase"] = "resumed"
            manifest.pop("error_code", None)
            self._write_module_manifest(workspace_key, manifest)
            return
        if current.get("type") not in {"install", "redeploy"}:
            raise AidpProvisionConflict(
                "The failed governance operation cannot be replaced by deletion"
            )
        resources = manifest.get("resources") or {}
        manifest.update(
            status="deleting",
            phase="disable" if resources.get("workflow_key") else "resources",
        )
        manifest["operation"] = {
            "operation_id": operation_id,
            "type": "delete",
            "phase": "started",
        }
        manifest.pop("error_code", None)
        self._write_module_manifest(workspace_key, manifest)

    def _prepare_governance_deletion(
        self, workspace_key: str, manifest: dict[str, Any], operation_id: str
    ) -> None:
        self._resume_governance_deletion(workspace_key, manifest, operation_id)
        current = manifest["operation"]
        if manifest["status"] != "deleting":
            if manifest["status"] != "active":
                raise AidpProvisionConflict(
                    "Another global governance module operation is already in progress"
                )
            manifest.update(status="deleting", phase="disable")
            manifest["operation"] = {
                "operation_id": operation_id,
                "type": "delete",
                "phase": "started",
            }
            self._write_module_manifest(workspace_key, manifest)
            return
        if current.get("operation_id") != operation_id:
            raise AidpProvisionConflict(
                "Another global governance module operation is already in progress"
            )

    def _delete_governance_disable_phase(
        self, workspace_key: str, manifest: dict[str, Any]
    ) -> None:
        resources = manifest.setdefault("resources", {})
        if not resources.get("disable_requested_at"):
            shared_compute = self._shared_compute(workspace_key)
            disable_requested_at = datetime.now(timezone.utc).isoformat()
            job_key, _ = self._ensure_governance_job(
                workspace_key,
                str(shared_compute["key"]),
                desired_enabled=False,
                paused=False,
            )
            resources.update(
                workflow_key=job_key,
                disable_requested_at=disable_requested_at,
            )
            self._write_module_manifest(workspace_key, manifest)
            raise AidpProvisionPending(
                "The governance workflow is disabling before deletion.", "disable"
            )
        job_key = str(resources.get("workflow_key") or "")
        if not job_key or not self._successful_governance_sync(
            workspace_key,
            job_key,
            requested_at=str(resources.get("disable_requested_at") or ""),
        ):
            raise AidpProvisionPending(
                "The governance workflow is disabling before deletion.", "disable"
            )
        manifest.update(enabled=False, phase="pause")
        self._write_module_manifest(workspace_key, manifest)

    def _delete_governance_pause_phase(
        self, workspace_key: str, manifest: dict[str, Any]
    ) -> None:
        shared_compute = self._shared_compute(workspace_key)
        _, changed = self._ensure_governance_job(
            workspace_key,
            str(shared_compute["key"]),
            desired_enabled=False,
            paused=True,
        )
        if changed:
            self._write_module_manifest(workspace_key, manifest)
            raise AidpProvisionPending(
                "The disabled governance workflow is being paused.", "pause"
            )
        manifest["phase"] = "resources"
        self._write_module_manifest(workspace_key, manifest)

    def _delete_governance_resources_phase(
        self, workspace_key: str, manifest: dict[str, Any]
    ) -> None:
        resources = manifest.get("resources") or {}
        agent_key = str(resources.get("agent_key") or "")
        if agent_key:
            self._delete_governance_deployments(workspace_key, agent_key)
        self._cleanup_agent(workspace_key, GOVERNANCE_AGENT_NAME)
        self._delete_governance_compute(workspace_key)
        self._cleanup_lab_job(workspace_key, GOVERNANCE_JOB_NAME)
        self._delete_governance_credential()
        for path in (
            f"{MODULE_ROOT}/agent/governance_agent.py",
            f"{MODULE_ROOT}/agent/requirements.txt",
            f"{MODULE_ROOT}/agent/agent-manifest.json",
            f"{MODULE_ROOT}/data_governance_sync.ipynb",
        ):
            self._delete_workspace_path(
                workspace_key,
                path,
                "The protected governance source deletion is still in progress.",
            )
        manifest["phase"] = "tables"
        self._write_module_manifest(workspace_key, manifest)

    def _delete_governance_tables_phase(
        self, workspace_key: str, manifest: dict[str, Any]
    ) -> None:
        self._delete_governance_tables()
        self._delete_governance_prefixes()
        manifest["phase"] = "manifest"
        self._write_module_manifest(workspace_key, manifest)

    def _delete_governance_module(
        self, user_ocid: str, operation_id: str, operation_type: str = "delete"
    ) -> dict[str, Any]:
        if operation_type != "delete":
            raise ValueError("Invalid governance module operation")
        workspace_key = self._governance_workspace()
        manifest = self._module_manifest(workspace_key)
        if manifest is None:
            return self._completed_governance_deletion(operation_id)
        self._prepare_governance_deletion(workspace_key, manifest, operation_id)
        handlers = {
            "disable": self._delete_governance_disable_phase,
            "pause": self._delete_governance_pause_phase,
            "resources": self._delete_governance_resources_phase,
            "tables": self._delete_governance_tables_phase,
        }
        while handler := handlers.get(str(manifest["phase"])):
            handler(workspace_key, manifest)
        self._delete_workspace_path(
            workspace_key,
            MODULE_ROOT,
            "The governance module manifest and workspace deletion is still in progress.",
        )
        return self._completed_governance_deletion(operation_id)

    async def delete_governance_module(
        self,
        user_ocid: str,
        operation_id: str,
        *,
        role_membership_verified: bool = False,
    ) -> dict[str, Any]:
        if not role_membership_verified and not await self.is_platform_admin(user_ocid):
            raise AidpProvisionConflict("The selected user is not an AI_DATA_PLATFORM_ADMIN")
        async with self._locks.setdefault(GOVERNANCE_MODULE_ID, asyncio.Lock()):
            return await self._run_module_operation(
                self._delete_governance_module, user_ocid, operation_id, "delete"
            )

    def _healthcheck(self) -> None:
        workspace = self._workspace()
        self._shared_compute(str(workspace["key"]))
        self.object_storage.head_bucket(
            self.settings.objectstorage_namespace,
            self.settings.bucket_name,
        )

    async def healthcheck(self) -> None:
        await asyncio.to_thread(self._healthcheck)
