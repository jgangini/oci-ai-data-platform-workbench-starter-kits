"""Idempotent AIDP provisioning for isolated lab participants."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import threading
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from oci._vendor import requests

from .autonomous import AutonomousGovernanceClient, AutonomousProvisionError, ParticipantDatabase
from .config import Settings
from .governance import agent_source, database_names, external_catalog_name
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


API_VERSION = "20240831"
SHARED_COMPUTE_NAME = "aidp_lab_shared_compute"
AGENT_COMPUTE_NAME = "aidp_agent_shared_compute"
LEGACY_CATALOG_NAME = "aidp_lab"
LAYOUT_VERSION = 4
CONTROL_ROOT = f"{WORKSPACE_ROOT}/.control"
LEGACY_WORKSPACE_ROOT = "/Workspace/lab-users"
LEGACY_LAB_IDS = frozenset({"banking", "telecommunications", "retail", "healthcare"})


def participant_catalog_name(key: str) -> str:
    if re.fullmatch(r"u[1-9][0-9]*", key) is None or int(key[1:]) < 101:
        raise ValueError("A participant key starting at u101 is required")
    return f"{key}_aidp_lab"


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

    def __init__(self, _: Settings) -> None:
        self.users: dict[str, dict[str, UserMaterial]] = {}
        self._operations: dict[tuple[str, str], tuple[str, UserMaterial]] = {}
        # ponytail: process-local locks are sufficient for the single-process development adapter.
        self._locks: dict[str, asyncio.Lock] = {}

    async def close(self) -> None:
        return None

    async def healthcheck(self) -> None:
        return None

    @staticmethod
    def _material(user_ocid: str, email: str, lab_id: str, participant_code: int) -> UserMaterial:
        pack = load_lab_pack(lab_id)
        key = participant_key(participant_code)
        resource_name = (
            str(pack.agent["name_template"]).format(participant_key=key)
            if pack.kind == "governance_agent"
            else f"wf_{key}_{lab_id}"
        )
        return UserMaterial(
            email,
            lab_id,
            key,
            workspace_root(key, lab_id, email),
            resource_name,
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
            f"dataLakes/{settings.aidp_platform_id}"
        )
        import oci

        self._oci = oci
        config = oci.config.from_file(settings.oci_config_file, "DEFAULT")
        self.signer = oci.signer.Signer(
            tenancy=config["tenancy"],
            user=config["user"],
            fingerprint=config["fingerprint"],
            private_key_file_location=config["key_file"],
            pass_phrase=config.get("pass_phrase"),
        )
        self.object_storage = oci.object_storage.ObjectStorageClient(config)
        self.governance_database = AutonomousGovernanceClient(
            settings.autonomous_runtime_file
        )
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
            return [item for item in body if isinstance(item, dict)]
        if isinstance(body, dict):
            values = body.get("items") or body.get("Items") or []
            return [item for item in values if isinstance(item, dict)]
        return []

    def _list(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
        phase: str = "content",
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page: str | None = None
        while True:
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

    def _catalog(self, name: str, *, allow_missing: bool = False) -> dict[str, Any] | None:
        catalogs = [
            item
            for item in self._list("/catalogs", phase="schemas")
            if (item.get("displayName") or item.get("name")) == name
        ]
        if not catalogs and allow_missing:
            return None
        if len(catalogs) != 1:
            raise AidpProvisionPending(f"The {name} catalog is not ready yet. Retry shortly.", "schemas")
        return self._require_operational_state(
            catalogs[0], {"ACTIVE"}, "catalog", "schemas"
        )

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

    def _ensure_external_catalog(
        self,
        participant_key: str,
        database: ParticipantDatabase,
    ) -> tuple[dict[str, Any], bool]:
        name = external_catalog_name(participant_key)
        current = self._catalog(name, allow_missing=True)
        if current is not None:
            return current, False
        self._request(
            "POST",
            "/catalogs",
            payload={
                "displayName": name,
                "description": f"Read-only Autonomous governance catalog for {participant_key}",
                "catalogType": "EXTERNAL",
                "sourceType": "ADW",
                "connectionDetails": {
                    "connectionProperties": {
                        "ADW_WALLET_CONTENT_BASE64": base64.b64encode(
                            database.wallet_zip
                        ).decode("ascii"),
                        "ADW_WALLET_PASSWORD": database.wallet_password,
                        "ADW_USERNAME": database.reader,
                        "ADW_PASSWORD": database.reader_password,
                        "ADW_TNS_ALIAS": database.dsn,
                    }
                },
            },
            phase="database",
        )
        published = self._catalog(name, allow_missing=True)
        if published is None:
            raise AidpProvisionPending(
                f"AIDP has not published external catalog {name} yet.", "database"
            )
        return published, True

    def _cleanup_external_catalog(self, participant_key: str, state: dict[str, Any]) -> None:
        name = str(state.get("external_catalog_name") or external_catalog_name(participant_key))
        catalog = self._catalog(name, allow_missing=True)
        if catalog is None:
            return
        catalog_key = str(catalog.get("key") or "")
        if not catalog_key:
            raise AidpProvisionPending(
                "The participant external catalog identifier is not ready.", "cleanup"
            )
        self._request(
            "DELETE",
            f"/catalogs/{catalog_key}",
            allow_not_found=True,
            phase="cleanup",
        )
        if self._catalog(name, allow_missing=True) is not None:
            raise AidpProvisionPending(
                "Participant external catalog deletion is still in progress.", "cleanup"
            )

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

    def _ensure_agent_compute(self, workspace_key: str) -> tuple[dict[str, Any], bool]:
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
        if len(current) > 1:
            raise AidpProvisionError(f"AIDP has duplicate AI compute named {AGENT_COMPUTE_NAME}.")
        if not current:
            self._request(
                "POST",
                f"/workspaces/{workspace_key}/clusters",
                payload={
                    "type": "AI_COMPUTE",
                    "displayName": AGENT_COMPUTE_NAME,
                    "description": "Shared least-privilege compute for participant governance agents",
                    "driverConfig": {
                        "driverShapeConfig": {"ocpus": 1, "memoryInGBs": 16, "gpus": 0}
                    },
                },
                phase="workspace",
            )
            current = matches()
            if len(current) != 1:
                raise AidpProvisionPending(
                    "AIDP has not published the shared AI compute yet.", "workspace"
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
        *,
        repair_drift: bool,
    ) -> tuple[str, bool]:
        entry_path = f"{root}/governance_agent.py"
        dependencies_path = f"{root}/requirements.txt"
        changed = self._upload_file(
            workspace_key, entry_path, source, repair_drift=repair_drift
        )
        changed = self._upload_file(
            workspace_key,
            dependencies_path,
            b"# AIDP provides aidputils, LangGraph and OCI runtime libraries.\n",
            repair_drift=repair_drift,
        ) or changed
        agents = self._agents(workspace_key, name)
        if len(agents) > 1:
            raise AidpProvisionError(f"AIDP has duplicate agents named {name}.")
        if not agents:
            self._request(
                "POST",
                f"/workspaces/{workspace_key}/agents",
                payload={
                    "displayName": name,
                    "description": "Participant-editable data governance agent with predefined read-only tools",
                    "pathInfo": root,
                    "type": "CODE",
                    "entryFilePath": entry_path,
                    "dependenciesFilePath": dependencies_path,
                    "computeKey": compute_key,
                },
                phase="content",
            )
            agents = self._agents(workspace_key, name)
            if len(agents) != 1:
                raise AidpProvisionPending(
                    "AIDP has not published the participant Agent yet.", "content"
                )
            changed = True
        agent_key = str(agents[0].get("key") or agents[0].get("id") or "")
        if not agent_key:
            raise AidpProvisionPending(
                "AIDP has not published the participant Agent identifier yet.", "content"
            )
        return agent_key, changed

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
        relative_path = workspace_path.removeprefix(f"{WORKSPACE_ROOT}/")
        parts = relative_path.split("/")
        if (
            manifest.get("layout_version") != 2
            or manifest.get("participant_key") != key
            or not workspace_path.startswith(f"{WORKSPACE_ROOT}/")
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
            manifest.get("layout_version") not in {3, LAYOUT_VERSION}
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
            pattern = rf"{re.escape(WORKSPACE_ROOT)}/(?!\.control(?:/|$))[^/]+/{re.escape(lab_id)}"
            return re.fullmatch(pattern, str(state.get("workspace_path") or "")) is not None
        if email is None and re.fullmatch(r"u[1-9][0-9]*", key):
            pattern = rf"{re.escape(WORKSPACE_ROOT)}/{re.escape(key)}_[^/]+/{re.escape(lab_id)}"
            return re.fullmatch(pattern, str(state.get("workspace_path") or "")) is not None
        return str(state.get("workspace_path") or "") == workspace_root(key, lab_id, email)

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
                "cleanup", "workspace", "database", "schemas", "content", "permissions"
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
            "agent": None,
            "external_catalog": None,
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
                "agent": None,
                "external_catalog": None,
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
            resource_name = (
                str(pack.agent["name_template"]).format(participant_key=key)
                if pack.kind == "governance_agent"
                else f"wf_{key}_{lab_id}"
            )
            labs[lab_id] = {
                "pack_version": pack.pack_version,
                "pack_hash": pack.pack_sha256,
                "workspace_path": workspace_root(key, lab_id, normalized_email if existing.get("owner_key") else None),
                "job_name": resource_name,
                "catalog_name": participant_catalog_name(key) if pack.kind == "data_pipeline" else "",
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
            and self._job_tasks_match(details.get("tasks"), payload["tasks"], compute_key)
            and self._job_compute_matches(details.get("jobClusters"), compute_key)
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
                for name in ("name", "path", "description", "maxConcurrentRuns")
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

    def _ensure_permissions(
        self,
        workspace_key: str,
        user_ocid: str,
        participant_root: str,
        job_key: str,
        catalog_key: str,
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
        changed = self._ensure_permission(
            f"/catalogs/{catalog_key}",
            "assignCatalogPermissionDetails",
            user_ocid,
            "SELECT",
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

    def _provision_agent(
        self,
        user_ocid: str,
        email: str,
        manifest: dict[str, Any],
        state: dict[str, Any],
        pack: LabPack,
    ) -> UserMaterial:
        owner_key = participant_owner_key(user_ocid)
        key = self._manifest_participant_key(manifest, owner_key)
        participant_code = manifest.get("participant_code")
        workspace_key = str(self._workspace()["key"])
        root = str(state["workspace_path"])
        participant_root = root.rsplit("/", 1)[0]
        agent_name = str(pack.agent["name_template"]).format(participant_key=key)
        if state.get("phase") == "active":
            return UserMaterial(
                email,
                pack.lab_id,
                key,
                root,
                agent_name,
                str(state.get("pack_version") or pack.pack_version),
                participant_code=participant_code if isinstance(participant_code, int) else None,
            )

        external_catalog = manifest.get("external_catalog")
        external_catalog_key = (
            str(external_catalog.get("key") or "")
            if isinstance(external_catalog, dict)
            else ""
        )
        if not external_catalog_key:
            if not self.governance_database.ready():
                state["phase"] = "database"
                self._write_manifest(workspace_key, owner_key, manifest)
                raise AidpProvisionPending(
                    "The Autonomous wallet and EXECUTE-only operator are not ready on the registration VM.",
                    "database",
                )
            try:
                participant_database = self.governance_database.ensure_participant(key)
            except AutonomousProvisionError as exc:
                raise AidpProvisionError(str(exc)) from exc
            catalog, catalog_changed = self._ensure_external_catalog(
                key, participant_database
            )
            external_catalog_key = str(catalog.get("key") or "")
            if not external_catalog_key:
                raise AidpProvisionPending(
                    "The participant external catalog has no published identifier yet.",
                    "database",
                )
            external_catalog = {
                "key": external_catalog_key,
                "name": external_catalog_name(key),
                "database_schema": participant_database.owner,
            }
            manifest["external_catalog"] = external_catalog
            state.update(
                phase="database",
                external_catalog_key=external_catalog_key,
                external_catalog_name=external_catalog["name"],
            )
            self._write_manifest(workspace_key, owner_key, manifest)
            if catalog_changed:
                raise AidpProvisionPending(
                    "The participant Autonomous schemas and external catalog are ready; Agent content is next.",
                    "database",
                )
        if not all(
            (
                self.settings.agent_model_id,
                self.settings.aidp_region,
                self.settings.compartment_id,
            )
        ):
            raise AidpProvisionError("The selected Agent model and regional runtime are incomplete.")

        layout_changed = self._ensure_workspace_layout(
            workspace_key, (WORKSPACE_ROOT, CONTROL_ROOT, participant_root, root)
        )
        compute, compute_changed = self._ensure_agent_compute(workspace_key)
        self._pending_after_change(
            layout_changed or compute_changed,
            False,
            workspace_key,
            manifest,
            pack.lab_id,
            "content",
            "The Agent workspace and shared AI compute are ready; content is next.",
        )
        database_schema = str(
            external_catalog.get("database_schema")
            if isinstance(external_catalog, dict)
            else database_names(key)[0]
        )
        source = agent_source(
            model_id=self.settings.agent_model_id,
            region=self.settings.aidp_region,
            compartment_id=self.settings.compartment_id,
            external_catalog_key=external_catalog_key,
            database_schema=database_schema,
        )
        agent_key, content_changed = self._ensure_agent(
            workspace_key,
            str(compute["key"]),
            agent_name,
            root,
            source,
            repair_drift=True,
        )
        state.update(agent_key=agent_key, compute_key=str(compute["key"]), job_name=agent_name)
        self._pending_after_change(
            content_changed,
            False,
            workspace_key,
            manifest,
            pack.lab_id,
            "permissions",
            "The participant Agent is ready; permissions are next.",
        )
        permissions_changed = self._ensure_permission(
            f"/workspaces/{workspace_key}/clusters/{compute['key']}",
            "assignClusterPermissionDetails",
            user_ocid,
            "USE",
        )
        permissions_changed = self._ensure_permission(
            f"/workspaces/{workspace_key}/agents/{agent_key}",
            "assignAgentPermissionDetails",
            user_ocid,
            "MANAGE",
        ) or permissions_changed
        if permissions_changed:
            raise AidpProvisionPending(
                "The participant Agent permissions were applied; final verification is next.",
                "permissions",
            )
        self._advance_lab_manifest(workspace_key, manifest, pack.lab_id, "active")
        manifest["agent"] = {
            "key": agent_key,
            "name": agent_name,
            "compute_key": str(compute["key"]),
            "model_id": self.settings.agent_model_id,
        }
        self._write_manifest(workspace_key, owner_key, manifest)
        return UserMaterial(
            email,
            pack.lab_id,
            key,
            root,
            agent_name,
            pack.pack_version,
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
        if pack.kind == "governance_agent":
            return self._provision_agent(user_ocid, email, manifest, state, pack)
        previous_phase = str(state.get("phase") or "workspace")
        was_active = previous_phase == "active"
        root = str(state["workspace_path"])
        participant_root = root.rsplit("/", 1)[0]
        job_name = str(state["job_name"])
        if was_active:
            return UserMaterial(
                email,
                lab_id,
                key,
                root,
                job_name,
                str(state.get("pack_version") or "legacy-v2"),
                participant_code=participant_code if isinstance(participant_code, int) else None,
            )
        repair_drift = True

        compute_key = str(self._shared_compute(workspace_key)["key"])
        workspace_changed = self._ensure_workspace_layout(
            workspace_key,
            (participant_root, root, f"{root}/source"),
        ) or workspace_changed
        self._pending_after_change(
            workspace_changed,
            was_active,
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
            was_active,
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
            was_active,
            workspace_key,
            manifest,
            lab_id,
            "permissions",
            "Participant content is ready; permissions are next.",
        )

        permissions_changed = self._ensure_permissions(
            workspace_key,
            user_ocid,
            participant_root,
            job_key,
            catalog_key,
        )
        if permissions_changed and was_active:
            raise AidpProvisionPending(
                "Participant permissions were repaired; final verification is next.",
                "permissions",
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
            resource_name = (
                str(pack.agent["name_template"]).format(participant_key=key)
                if pack.kind == "governance_agent"
                else f"wf_{key}_{lab_id}"
            )
            state.update(
                pack_version=pack.pack_version,
                pack_hash=pack.pack_sha256,
                workspace_path=workspace_root(
                    key, lab_id, str(manifest.get("participant_email") or "") or None
                ),
                job_name=resource_name,
                catalog_name=(participant_catalog_name(key) if pack.kind == "data_pipeline" else ""),
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
        name = participant_catalog_name(key)
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
        pack = load_lab_pack(lab_id)
        if pack.kind == "governance_agent":
            self._cleanup_agent(
                workspace_key,
                str(state.get("job_name") or f"{key}_agent_data_governance"),
            )
            self._cleanup_external_catalog(key, state)
            try:
                self.governance_database.drop_participant(key)
            except AutonomousProvisionError as exc:
                raise AidpProvisionError(str(exc)) from exc
            if not preserve_workspace:
                self._delete_workspace_path(
                    workspace_key,
                    workspace_path,
                    "Agent workspace deletion is still in progress.",
                )
            return
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
                participant_roots.add(str(state["workspace_path"]).rsplit("/", 1)[0])
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

    def _healthcheck(self) -> None:
        workspace = self._workspace()
        self._shared_compute(str(workspace["key"]))
        self.object_storage.head_bucket(
            self.settings.objectstorage_namespace,
            self.settings.bucket_name,
        )

    async def healthcheck(self) -> None:
        await asyncio.to_thread(self._healthcheck)
