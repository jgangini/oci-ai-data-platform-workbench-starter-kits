#!/usr/bin/env python3
"""Additively reconcile AIDP Master Catalog resources after Terraform APPLY."""

from __future__ import annotations

import base64
import configparser
import hashlib
import json
import os
import re
import secrets
import string
import sys
import tempfile
import time
import uuid
import zipfile
from io import BytesIO, StringIO
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from oci._vendor import requests


API_VERSION = "20240831"
GOVERNANCE_API_VERSION = "20260430"
CATALOG_NAME = "aidp_lab"
DEVELOPER_ROLE_NAME = "AIDP_LAB_DEVELOPER"
PENDING_ROLE_NAME = "AIDP_LAB_PENDING"
SHARED_COMPUTE_NAME = "aidp_cluster_shared_compute"
BOOTSTRAP_OBJECT_NAME = ".bootstrap/operator-credentials.json"
BOOTSTRAP_VERSION = 2
BOOTSTRAP_READY = "AIDP_LAB_CREDENTIALS_V2_READY"
DATABASE_OPERATOR = "AIDP_LAB_OPERATOR"
GOVERNANCE_CREDENTIAL_NAME = "AidpGovernanceOperator"
LAYERS = ("landing", "bronze", "silver", "gold")
RESOURCE_WAIT_ATTEMPTS = 120
POST_APPLY_BUDGET_SECONDS = 3300
_post_apply_deadline = 0.0
PUBLIC_KEY_SCRIPT = (
    "attempt=0; while [ \"$attempt\" -lt 120 ]; do "
    "if [ -x /usr/local/sbin/aidp-lab-bootstrap-public-key ]; then "
    "exec sudo /usr/local/sbin/aidp-lab-bootstrap-public-key; fi; "
    "attempt=$((attempt + 1)); sleep 5; done; exit 1"
)


class ReconcileError(RuntimeError):
    pass


class ApiRequestError(ReconcileError):
    def __init__(self, method: str, path: str, status_code: int, request_id: str) -> None:
        super().__init__(f"AIDP {method} {path} failed with {status_code}; opc-request-id={request_id}")
        self.status_code = status_code


def _sleep(seconds: float) -> None:
    # ponytail: the hook is a single process; one process-wide deadline keeps every retry below Deploy Studio's cap.
    if _post_apply_deadline and time.monotonic() + seconds >= _post_apply_deadline:
        raise ReconcileError("Post-apply reconciliation reached its safe execution deadline")
    time.sleep(seconds)


def read_json_env(name: str) -> dict[str, Any]:
    path = os.environ.get(name)
    if not path:
        raise ReconcileError(f"{name} is required")
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_result(path: str, result: dict[str, Any]) -> None:
    target = Path(path)
    target.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        target.chmod(0o600)
    except OSError:
        pass


@dataclass(slots=True)
class ApiResponse:
    status_code: int
    body: Any
    headers: dict[str, str]


class AidpApi:
    def __init__(
        self,
        region: str,
        platform_id: str,
        signer: Any,
        deployment_id: str,
        *,
        api_version: str = API_VERSION,
        resource_segment: str = "dataLakes",
    ) -> None:
        self.base = (
            f"https://datalake.{region}.oci.oraclecloud.com/{api_version}/"
            f"{resource_segment}/{platform_id}"
        )
        self.signer = signer
        self.deployment_id = deployment_id
        self.session = requests.Session()

    def _send(
        self,
        method: str,
        path: str,
        request_headers: dict[str, str],
        payload: dict[str, Any] | None,
        data: bytes | None,
        params: dict[str, Any] | None,
    ) -> Any:
        for attempt in range(5):
            try:
                response = self.session.request(
                    method,
                    f"{self.base}{path}",
                    auth=self.signer,
                    headers=request_headers,
                    params=params,
                    json=payload if data is None else None,
                    data=data,
                    timeout=(10, 60),
                )
            except requests.exceptions.RequestException as exc:
                if attempt == 4:
                    raise ReconcileError(f"AIDP {method} {path} failed after network retries") from exc
                _sleep(min(2**attempt, 15))
                continue
            if response.status_code not in {429, 500, 502, 503, 504} or attempt == 4:
                return response
            retry_after = response.headers.get("retry-after")
            delay = min(30, int(retry_after)) if retry_after and retry_after.isdigit() else min(2**attempt, 15)
            _sleep(delay)
        raise ReconcileError(f"AIDP {method} {path} exhausted retries")

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> ApiResponse:
        request_headers = {"Accept": "application/json", **(headers or {})}
        if payload is not None:
            request_headers["Content-Type"] = "application/json"
        if method.upper() == "POST":
            content = data if data is not None else json.dumps(
                payload or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
            payload_hash = hashlib.sha256(content).hexdigest()
            object_type = str(request_headers.get("type") or path.strip("/").split("/", 1)[0] or "root")
            request_headers["opc-retry-token"] = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"{self.deployment_id}:{method.upper()}:{path}:{object_type}:{payload_hash}",
                )
            )
        response = self._send(method, path, request_headers, payload, data, params)
        body: Any = None
        if response.content:
            try:
                body = response.json()
            except ValueError:
                body = response.content
        if response.status_code >= 400:
            raise ApiRequestError(method, path, response.status_code, response.headers.get("opc-request-id", "unavailable"))
        return ApiResponse(response.status_code, body, {key.lower(): value for key, value in response.headers.items()})

    def list_all(self, path: str, *, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page: str | None = None
        while True:
            query = dict(params or {})
            if page:
                query["page"] = page
            response = self.request("GET", path, params=query)
            body = response.body
            if isinstance(body, list):
                items.extend(body)
            elif isinstance(body, dict):
                items.extend(body.get("items") or body.get("Items") or [])
            page = response.headers.get("opc-next-page")
            if not page:
                return items


def exact_one(items: list[dict[str, Any]], name: str, kind: str) -> dict[str, Any] | None:
    matches = [item for item in items if item.get("displayName") == name]
    if len(matches) > 1:
        raise ReconcileError(f"Ambiguous {kind}: multiple resources named {name}")
    return matches[0] if matches else None


def ensure_governance_operator_credential(
    api: AidpApi,
    config: dict[str, Any],
    key_text: str,
    region: str,
) -> bool:
    missing = [
        name
        for name in ("tenancy", "user", "fingerprint")
        if not str(config.get(name) or "")
    ]
    if missing or not region or not key_text.strip():
        raise ReconcileError("The uploaded OCI operator credential is incomplete")
    payload = {
        "displayName": GOVERNANCE_CREDENTIAL_NAME,
        "credentialDescription": (
            "Shared OCI API credential for participant governance agents in this trial deployment"
        ),
        "type": "SECRET_TOKEN",
        "credentialDetails": {
            "credentialType": "SECRET_TOKEN",
            "secretTokenPair": [
                {"secretKey": "tenancy", "secretValue": str(config["tenancy"])},
                {"secretKey": "user", "secretValue": str(config["user"])},
                {"secretKey": "fingerprint", "secretValue": str(config["fingerprint"])},
                {"secretKey": "region", "secretValue": region},
                {"secretKey": "private_key", "secretValue": key_text},
            ],
        },
    }
    current = exact_one(
        api.list_all("/credentials", params={"displayName": GOVERNANCE_CREDENTIAL_NAME}),
        GOVERNANCE_CREDENTIAL_NAME,
        "governance operator credential",
    )
    if current is None:
        api.request("POST", "/credentials", payload=payload)
        return True
    credential_type = str(current.get("type") or current.get("credentialType") or "")
    if credential_type != "SECRET_TOKEN":
        raise ReconcileError(
            "Existing governance operator credential has incompatible type"
        )
    credential_key = str(current.get("key") or current.get("id") or "")
    if not credential_key:
        raise ReconcileError("The governance operator credential has no identifier")
    api.request("PUT", f"/credentials/{quote(credential_key, safe='')}", payload=payload)
    return False


def assert_fields(resource: dict[str, Any], expected: dict[str, Any], kind: str) -> None:
    if not isinstance(resource, dict):
        raise ReconcileError(f"AIDP returned no {kind} details")
    mismatches = [key for key, value in expected.items() if resource.get(key) != value]
    if mismatches:
        raise ReconcileError(f"Existing {kind} has incompatible fields: {', '.join(mismatches)}")


def is_active_or_raise(resource: dict[str, Any], kind: str) -> bool:
    state = str(resource.get("lifecycleState") or resource.get("state") or "").upper()
    if state == "ACTIVE" or (kind == "shared compute" and state == "STOPPED"):
        return True
    if state in {"FAILED", "DELETING", "DELETED", "DELETE_FAILED", "CANCELED", "CANCELLED"} or state.endswith(
        "FAILED"
    ):
        raise ReconcileError(f"{kind} {resource.get('displayName', 'unknown')} entered terminal state {state}")
    return False


def ensure_resource(
    api: AidpApi,
    path: str,
    kind: str,
    name: str,
    create_payload: dict[str, Any],
    immutable_fields: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
    wait_for_active: bool = False,
    attempts: int = RESOURCE_WAIT_ATTEMPTS,
) -> tuple[dict[str, Any], bool]:
    query = {"displayName": name, **(filters or {})}
    current = exact_one(api.list_all(path, params=query), name, kind)
    if current:
        if not wait_for_active or is_active_or_raise(current, kind):
            assert_fields(current, immutable_fields, kind)
            return current, False
        created = False
    else:
        created = True
        try:
            api.request("POST", path, payload=create_payload)
        except ApiRequestError as exc:
            if exc.status_code != 409:
                raise
            created = False
    for _ in range(attempts):
        current = exact_one(api.list_all(path, params=query), name, kind)
        if current:
            if not wait_for_active or is_active_or_raise(current, kind):
                assert_fields(current, immutable_fields, kind)
                return current, created
        _sleep(5)
    target = "ACTIVE state" if wait_for_active else "visibility"
    raise ReconcileError(f"Timed out waiting for {kind} {name} {target}")


def wait_for_existing_active(
    api: AidpApi,
    path: str,
    kind: str,
    name: str,
    immutable_fields: dict[str, Any],
    *,
    attempts: int = RESOURCE_WAIT_ATTEMPTS,
) -> dict[str, Any]:
    query = {"displayName": name}
    for _ in range(attempts):
        current = exact_one(api.list_all(path, params=query), name, kind)
        if current and is_active_or_raise(current, kind):
            assert_fields(current, immutable_fields, kind)
            return current
        _sleep(5)
    raise ReconcileError(f"Timed out waiting for existing {kind} {name} ACTIVE state")


def role_has_member(
    api: AidpApi,
    role_key: str,
    principal_type: str,
    principal_id: str,
) -> bool:
    body = api.request("GET", f"/roles/{role_key}").body
    assignees = (body.get("assignees") or []) if isinstance(body, dict) else []
    return any(
        isinstance(item, dict)
        and str(item.get("type", "")).upper() == principal_type.upper()
        and item.get("target") == principal_id
        for item in assignees
    )


def role_has_group(api: AidpApi, role_key: str, group_ocid: str) -> bool:
    return role_has_member(api, role_key, "GROUP", group_ocid)


def assert_role_members_exact(
    api: AidpApi,
    role_key: str,
    role_name: str,
    principal_type: str,
    principal_id: str,
) -> None:
    body = api.request("GET", f"/roles/{role_key}").body
    assignees = (body.get("assignees") or []) if isinstance(body, dict) else []
    actual = {
        (str(item.get("type", "")).upper(), str(item.get("target", "")))
        for item in assignees
        if isinstance(item, dict)
    }
    if actual != {(principal_type.upper(), principal_id)}:
        raise ReconcileError(
            f"Role {role_name} has unexpected members; remove the broader assignments before retrying"
        )


def assert_operator_platform_admin(
    api: AidpApi,
    operator_user_ocid: str,
    *,
    attempts: int = RESOURCE_WAIT_ATTEMPTS,
) -> None:
    last_not_ready: ApiRequestError | None = None
    for attempt in range(attempts):
        try:
            role = exact_one(
                api.list_all("/roles", params={"displayName": "AI_DATA_PLATFORM_ADMIN"}),
                "AI_DATA_PLATFORM_ADMIN",
                "role",
            )
            last_not_ready = None
        except ApiRequestError as exc:
            if exc.status_code != 404:
                raise
            # ponytail: ACTIVE can precede Workbench RBAC visibility; this bounded wait
            # still remains under the hook's process-wide safety deadline.
            last_not_ready = exc
            role = None
        if role and role_has_member(api, str(role.get("key") or ""), "USER", operator_user_ocid):
            return
        if attempt + 1 < attempts:
            _sleep(5)
    if last_not_ready:
        raise ReconcileError(
            "AIDP Workbench did not authorize the deployment operator after the "
            f"readiness window; last request: {last_not_ready}"
        ) from last_not_ready
    raise ReconcileError("OCI deployment operator is not an AI_DATA_PLATFORM_ADMIN member")


def _admin_permission_is_assigned(
    matches: list[dict[str, Any]], role_name: str
) -> bool:
    observed = set().union(
        *(set(item.get("granteePermissions") or []) for item in matches)
    )
    if not observed.issubset({"READ", "SELECT", "USE", "ADMIN"}):
        raise ReconcileError(
            f"Role {role_name} has a conflicting direct permission; remove the broader grant before retrying"
        )
    return "ADMIN" in observed


def permission_is_assigned(
    api: AidpApi,
    inspect_path: str,
    role_name: str,
    permission: str,
    inheritable: bool | None = None,
) -> bool:
    matches: list[dict[str, Any]] = []
    for item in api.list_all(inspect_path):
        if not isinstance(item, dict) or str(item.get("granteeType", "")).upper() != "ROLE":
            continue
        if role_name not in {item.get("grantee"), item.get("granteeName")}:
            continue
        matches.append(item)
    expected_permissions = {permission}
    if permission == "ADMIN":
        return _admin_permission_is_assigned(matches, role_name)
    if any(
        set(item.get("granteePermissions") or []) != expected_permissions
        or (inheritable is not None and item.get("isPermissionsInheritable") is not inheritable)
        for item in matches
    ) or len(matches) > 1:
        raise ReconcileError(
            f"Role {role_name} has a conflicting direct permission; remove the broader grant before retrying"
        )
    return len(matches) == 1


def assert_role_permissions_exact(
    api: AidpApi,
    role_key: str,
    role_name: str,
    expected: set[tuple[str, str, frozenset[str]]],
) -> None:
    actual: list[tuple[str, str, frozenset[str]]] = []
    for item in api.list_all(
        f"/roles/{role_key}/permissions", params={"permissionScope": "DIRECT"}
    ):
        details = item.get("permissionsWithResourceDetails") if isinstance(item, dict) else None
        if not isinstance(details, dict):
            raise ReconcileError(f"Role {role_name} returned an invalid permission record")
        actual.append(
            (
                str(details.get("resourceType") or "").upper(),
                str(details.get("resourceKey") or ""),
                frozenset(details.get("permissions") or []),
            )
        )
    if len(actual) != len(expected) or set(actual) != expected:
        raise ReconcileError(
            f"Role {role_name} has unexpected direct permissions; remove the broader grants before retrying"
        )


def ensure_action(
    api: AidpApi,
    method: str,
    action_path: str,
    payload: dict[str, Any],
    is_applied: Callable[[], bool],
    *,
    attempts: int = 12,
) -> bool:
    if is_applied():
        return False
    try:
        api.request(method, action_path, payload=payload)
    except ApiRequestError as exc:
        if exc.status_code != 409:
            raise
    for _ in range(attempts):
        if is_applied():
            return True
        _sleep(5)
    raise ReconcileError(f"AIDP action {action_path} did not converge to the requested values")


def load_oci_config(config_path: str, key_path: str) -> dict[str, Any]:
    import oci
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    parser = configparser.ConfigParser(interpolation=None, strict=True)
    try:
        loaded = parser.read(config_path, encoding="utf-8")
    except (OSError, configparser.Error) as exc:
        raise ReconcileError("OCI config could not be parsed") from exc
    if not loaded or not parser.defaults():
        raise ReconcileError("OCI config is missing the DEFAULT profile")
    config = dict(parser["DEFAULT"])
    if config.get("pass_phrase"):
        raise ReconcileError("OCI API private key must be an unencrypted RSA PEM")
    config["key_file"] = key_path
    required = ("tenancy", "user", "fingerprint")
    missing = [name for name in required if not config.get(name)]
    if missing:
        raise ReconcileError(f"OCI config is missing required fields: {', '.join(missing)}")
    try:
        private_key = serialization.load_pem_private_key(Path(key_path).read_bytes(), password=None)
    except (OSError, ValueError, TypeError) as exc:
        raise ReconcileError("OCI API private key must be an unencrypted RSA PEM") from exc
    if not isinstance(private_key, rsa.RSAPrivateKey):
        raise ReconcileError("OCI API private key must be an unencrypted RSA PEM")
    actual_fingerprint = hashlib.md5(
        private_key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ),
        usedforsecurity=False,
    ).hexdigest()
    configured_fingerprint = str(config["fingerprint"]).replace(":", "").lower()
    if configured_fingerprint != actual_fingerprint:
        raise ReconcileError("OCI API private key does not match the configured fingerprint")
    try:
        oci.config.validate_config(config)
    except Exception as exc:
        raise ReconcileError("OCI config could not be validated with the supplied private key") from exc
    return config


def render_runtime_oci_config(config: dict[str, Any]) -> str:
    parser = configparser.ConfigParser(interpolation=None)
    parser["DEFAULT"] = {
        name: str(config[name])
        for name in ("tenancy", "user", "fingerprint", "region")
    }
    parser["DEFAULT"]["key_file"] = "/etc/aidp-lab/oci/key.pem"
    rendered = StringIO()
    parser.write(rendered, space_around_delimiters=False)
    return rendered.getvalue()


def build_signer(config: dict[str, Any]) -> Any:
    import oci

    return oci.signer.Signer(
        tenancy=config["tenancy"],
        user=config["user"],
        fingerprint=config["fingerprint"],
        private_key_file_location=config["key_file"],
        pass_phrase=config.get("pass_phrase"),
    )


def describe_object_prefixes() -> list[str]:
    # ponytail: OCI prefixes are virtual; real workload objects create them on first write.
    return [f"Object Storage prefix {index:02d}_{layer}/ is virtual" for index, layer in enumerate(LAYERS, start=1)]


def workspace_object(api: AidpApi, workspace_key: str, path: str) -> ApiResponse | None:
    try:
        return api.request(
            "GET",
            f"/workspaces/{workspace_key}/objects/{quote(path, safe='')}",
            headers={"Accept": "*/*"},
        )
    except ApiRequestError as exc:
        if exc.status_code == 404:
            return None
        raise


def workspace_object_key(response: ApiResponse | None, path: str) -> str:
    if response is None:
        return ""
    body = response.body if isinstance(response.body, dict) else {}
    headers = {str(name).casefold(): value for name, value in response.headers.items()}
    key = headers.get("object-key") or body.get("key") or body.get("objectKey")
    if key:
        return str(key)
    object_path = (
        headers.get("folder")
        or headers.get("path")
        or body.get("path")
    )
    if object_path and str(object_path) != path:
        raise ReconcileError(f"AIDP workspace object {path} returned a mismatched path")
    if not object_path:
        raise ReconcileError(f"AIDP workspace object {path} has no object key")
    return str(object_path)


def ensure_workspace_folder(api: AidpApi, workspace_key: str, path: str) -> tuple[str, bool]:
    current = workspace_object(api, workspace_key, path)
    if current is not None:
        return workspace_object_key(current, path), False
    try:
        api.request(
            "POST",
            f"/workspaces/{workspace_key}/objects",
            data=b"",
            headers={
                "Accept": "*/*",
                "Content-Type": "application/octet-stream",
                "path": path,
                "type": "FOLDER",
                "is-overwrite": "false",
            },
        )
    except ApiRequestError as exc:
        if exc.status_code != 409:
            raise
    current = workspace_object(api, workspace_key, path)
    if current is None:
        raise ReconcileError(f"AIDP workspace folder {path} was not published")
    return workspace_object_key(current, path), True


def ensure_role(
    api: AidpApi,
    name: str,
    description: str,
    principal_type: str,
    principal_id: str,
) -> tuple[str, bool, bool]:
    role, created = ensure_resource(
        api,
        "/roles",
        "role",
        name,
        {"displayName": name, "description": description},
        {},
        filters={"displayName": name},
    )
    role_key = str(role["key"])
    member_added = ensure_action(
        api,
        "POST",
        f"/roles/{role_key}/actions/addMember",
        {"assignees": [{"type": principal_type, "target": principal_id}]},
        lambda: role_has_member(api, role_key, principal_type, principal_id),
    )
    return role_key, created, member_added


def ensure_role_permission(
    api: AidpApi,
    resource_path: str,
    assignment_key: str,
    role_name: str,
    permission: str,
    *,
    method: str = "POST",
    inheritable: bool | None = None,
) -> bool:
    assignment: dict[str, Any] = {
        "assignees": {"type": "ROLE", "targets": [role_name]},
        "permissions": [permission],
    }
    if inheritable is not None:
        assignment["isPermissionsInheritable"] = inheritable
    return ensure_action(
        api,
        method,
        f"{resource_path}/actions/managePermission",
        {assignment_key: assignment},
        lambda: permission_is_assigned(
            api,
            f"{resource_path}/permissions",
            role_name,
            permission,
            inheritable,
        ),
    )


def assert_fresh_catalog(
    api: AidpApi,
    catalog_key: str,
    namespace: str,
    bucket: str,
) -> tuple[int, int]:
    schemas = api.list_all("/schemas", params={"catalogKey": catalog_key})
    global_schemas = [
        item for item in schemas if item.get("displayName") in set(LAYERS)
    ]
    if global_schemas:
        names = ", ".join(sorted(str(item.get("displayName")) for item in global_schemas))
        raise ReconcileError(
            f"Fresh-only bootstrap found legacy global schemas: {names}; remove them explicitly before retrying"
        )
    volumes: list[dict[str, Any]] = []
    for schema in schemas:
        schema_key = str(schema.get("key") or "")
        if not schema_key:
            raise ReconcileError("AIDP schema has no key while checking legacy external volumes")
        volumes.extend(
            api.list_all(
                "/volumes",
                params={"catalogKey": catalog_key, "schemaKey": schema_key},
            )
        )
    external: list[dict[str, Any]] = []
    for volume in volumes:
        details = volume
        if volume.get("key") and not volume.get("storageLocation"):
            response = api.request("GET", f"/volumes/{volume['key']}").body
            details = response if isinstance(response, dict) else volume
        if str(details.get("volumeType") or "").upper() == "EXTERNAL":
            external.append(details)
    expected_locations = {
        f"oci://{bucket}@{namespace}/{index:02d}_{layer}/"
        for index, layer in enumerate(LAYERS, start=1)
    }
    overlapping = [
        item for item in external if item.get("storageLocation") in expected_locations
    ]
    if overlapping:
        names = ", ".join(sorted(str(item.get("displayName") or item.get("key")) for item in overlapping))
        raise ReconcileError(
            f"Fresh-only bootstrap found legacy external volumes overlapping medallion paths: {names}; no resources were deleted"
        )
    if external:
        raise ReconcileError(
            "Fresh-only bootstrap requires zero external volumes in aidp_lab; no resources were deleted"
        )
    return len(global_schemas), len(external)


def parse_public_key_output(text: str) -> str:
    if text.strip() == BOOTSTRAP_READY:
        return BOOTSTRAP_READY
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    public_key = "\n".join(lines) + "\n"
    if "PRIVATE KEY" in public_key or not public_key.startswith("-----BEGIN PUBLIC KEY-----"):
        raise ReconcileError("Run Command did not return a public-only PEM")
    if not public_key.rstrip().endswith("-----END PUBLIC KEY-----"):
        raise ReconcileError("Run Command returned an incomplete public key")
    return public_key


def fetch_bootstrap_public_key(
    client: Any,
    oci_module: Any,
    compartment_id: str,
    instance_id: str,
    *,
    attempts: int = 150,
    create_attempts: int = 60,
) -> str:
    models = oci_module.compute_instance_agent.models
    details = models.CreateInstanceAgentCommandDetails(
        compartment_id=compartment_id,
        execution_time_out_in_seconds=660,
        display_name="aidp-lab-bootstrap-public-key",
        target=models.InstanceAgentCommandTarget(instance_id=instance_id),
        content=models.InstanceAgentCommandContent(
            source=models.InstanceAgentCommandSourceViaTextDetails(text=PUBLIC_KEY_SCRIPT),
            output=models.InstanceAgentCommandOutputViaTextDetails(),
        ),
    )
    command = None
    for attempt in range(create_attempts):
        try:
            command = client.create_instance_agent_command(details).data
            break
        except oci_module.exceptions.ServiceError as exc:
            if exc.status not in {403, 404, 409, 429, 500, 502, 503, 504} or attempt + 1 == create_attempts:
                raise ReconcileError(f"Compute Run Command submission failed with OCI {exc.status}") from exc
            _sleep(5)
    if command is None:
        raise ReconcileError("Compute Run Command submission did not complete")
    command_id = str(getattr(command, "id", "") or "")
    if not command_id:
        raise ReconcileError("Compute Run Command did not return a command OCID")
    terminal = {"FAILED", "TIMED_OUT", "CANCELED"}
    for _ in range(attempts):
        try:
            execution = client.get_instance_agent_command_execution(command_id, instance_id).data
        except oci_module.exceptions.ServiceError as exc:
            if exc.status == 404:
                _sleep(5)
                continue
            raise ReconcileError(f"Compute Run Command status failed with OCI {exc.status}") from exc
        state = str(getattr(execution, "lifecycle_state", "") or "").upper()
        if state == "SUCCEEDED":
            content = getattr(execution, "content", None)
            return parse_public_key_output(str(getattr(content, "text", "") or ""))
        if state in terminal:
            raise ReconcileError(f"Compute Run Command failed with state {state}")
        _sleep(5)
    raise ReconcileError("Timed out waiting for the VM bootstrap state Run Command")


def encrypt_bootstrap_credentials(
    public_key: str,
    config_text: str,
    key_text: str,
    wallet: bytes,
    wallet_password: str,
    operator_username: str,
    operator_password: str,
    dsn: str,
) -> bytes:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        recipient = serialization.load_pem_public_key(public_key.encode("ascii"))
    except (UnicodeEncodeError, ValueError, TypeError) as exc:
        raise ReconcileError("VM bootstrap public key is invalid") from exc
    if not isinstance(recipient, rsa.RSAPublicKey):
        raise ReconcileError("VM bootstrap public key must be RSA")
    data_key = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(12)
    plaintext = json.dumps(
        {
            "config_text": config_text,
            "key_text": key_text,
            "wallet_zip_b64": base64.b64encode(wallet).decode("ascii"),
            "wallet_password": wallet_password,
            "operator_username": operator_username,
            "operator_password": operator_password,
            "dsn": dsn,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    ciphertext = AESGCM(data_key).encrypt(nonce, plaintext, None)
    wrapped_key = recipient.encrypt(
        data_key,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    envelope = {
        "schema_version": BOOTSTRAP_VERSION,
        "wrapped_key_b64": base64.b64encode(wrapped_key).decode("ascii"),
        "nonce_b64": base64.b64encode(nonce).decode("ascii"),
        "ciphertext_b64": base64.b64encode(ciphertext).decode("ascii"),
    }
    return (json.dumps(envelope, separators=(",", ":")) + "\n").encode("utf-8")


def reconcile(api: AidpApi, outputs: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    events: list[str] = []
    assert_operator_platform_admin(api, str(outputs["operator_user_ocid"]))
    events.append("Deployment operator AI_DATA_PLATFORM_ADMIN membership verified")
    workspace_name = str(outputs["default_workspace_name"])
    workspace = wait_for_existing_active(
        api,
        "/workspaces",
        "workspace",
        workspace_name,
        {"type": "DEFAULT"},
    )

    catalog, created = ensure_resource(
        api,
        "/catalogs",
        "catalog",
        CATALOG_NAME,
        {
            "displayName": CATALOG_NAME,
            "description": "Legacy compatibility catalog; participant data uses private catalogs",
            "catalogType": "INTERNAL",
        },
        {"catalogType": "INTERNAL"},
        wait_for_active=True,
    )
    events.append(f"Catalog {CATALOG_NAME} {'created' if created else 'reused'}")
    catalog_key = str(catalog["key"])
    namespace = str(outputs["objectstorage_namespace"])
    bucket = str(outputs["bucket_name"])
    global_schema_count, external_volume_count = assert_fresh_catalog(
        api, catalog_key, namespace, bucket
    )
    events.append("Fresh-only catalog verified: zero legacy schemas and zero external volumes")
    workspace_key = str(workspace["key"])
    shared_compute, compute_created = ensure_resource(
        api,
        f"/workspaces/{workspace_key}/clusters",
        "shared compute",
        SHARED_COMPUTE_NAME,
        {
            "type": "USER",
            "displayName": SHARED_COMPUTE_NAME,
            "description": "Shared Spark compute for lab workflows and governed Agent SQL tools",
            "driverConfig": {
                "driverShape": "amd.generic",
                "driverShapeConfig": {"ocpus": 2, "memoryInGBs": 32},
            },
            "workerConfig": {
                "workerShape": "amd.generic",
                "workerShapeConfig": {"ocpus": 2, "memoryInGBs": 32},
                "minWorkerCount": 1,
                "maxWorkerCount": 10,
            },
            "clusterRuntimeConfig": {
                "type": "SPARK",
                "sparkVersion": "3.5.0",
                "sparkAdvancedConfigurations": {"spark.aidp.lineage.enabled": "true"},
                "sparkEnvVariables": {},
                "initScripts": [],
            },
        },
        {},
        wait_for_active=True,
    )
    events.append(f"Shared compute {SHARED_COMPUTE_NAME} {'created' if compute_created else 'reused'}")
    compute_key = str(shared_compute["key"])
    root_object_key, root_created = ensure_workspace_folder(
        api, workspace_key, "/Workspace/medallon"
    )
    events.append(
        f"Workspace root /Workspace/medallon {'created' if root_created else 'reused'}"
    )

    role_specs = (
        (
            DEVELOPER_ROLE_NAME,
            "AIDP lab developer",
            str(outputs["developer_group_ocid"]),
        ),
        (
            PENDING_ROLE_NAME,
            "AIDP lab pending participant",
            str(outputs["pending_group_ocid"]),
        ),
    )
    role_keys: dict[str, str] = {}
    for role_name, description, group_ocid in role_specs:
        role_key, role_created, member_added = ensure_role(
            api, role_name, description, "GROUP", group_ocid
        )
        role_keys[role_name] = role_key
        events.append(
            f"Role {role_name} {'created' if role_created else 'reused'}; "
            f"group {'added' if member_added else 'already assigned'}"
        )

    for role_name in (DEVELOPER_ROLE_NAME, PENDING_ROLE_NAME):
        ensure_role_permission(
            api,
            f"/workspaces/{workspace_key}",
            "assignWorkspacePermissionDetails",
            role_name,
            "USER",
        )
    ensure_role_permission(
        api,
        f"/workspaces/{workspace_key}/clusters/{compute_key}",
        "assignClusterPermissionDetails",
        DEVELOPER_ROLE_NAME,
        "USE",
    )
    expected_permissions = {
        DEVELOPER_ROLE_NAME: {
            ("WORKSPACE", str(workspace["displayName"]), frozenset({"USER"})),
            (
                "CLUSTER",
                f"{workspace['displayName']}/{SHARED_COMPUTE_NAME}",
                frozenset({"USE"}),
            ),
        },
        PENDING_ROLE_NAME: {
            ("WORKSPACE", str(workspace["displayName"]), frozenset({"USER"})),
        },
    }
    for role_name, _, group_ocid in role_specs:
        assert_role_members_exact(
            api, role_keys[role_name], role_name, "GROUP", group_ocid
        )
        assert_role_permissions_exact(
            api, role_keys[role_name], role_name, expected_permissions[role_name]
        )
    events.append("AIDP developer and pending RBAC verified; operator retains platform administration")

    return (
        {
            "workspace_key": workspace_key,
            "shared_compute_key": compute_key,
            "shared_compute_name": SHARED_COMPUTE_NAME,
            "catalog_key": catalog_key,
            "catalog_name": CATALOG_NAME,
            "role_keys": role_keys,
            "root_object_key": root_object_key,
            "global_schema_count": global_schema_count,
            "external_volume_count": external_volume_count,
        },
        events,
    )


def workbench_url(outputs: dict[str, Any]) -> str:
    direct_url = str(outputs.get("aidp_workbench_url") or "").strip()
    if direct_url.startswith("https://") and ".datalake.oci.oraclecloud.com" in direct_url:
        return direct_url
    endpoint = str(
        outputs.get("aidp_web_socket_endpoint")
        or outputs.get("aidp_alias_endpoint")
        or ""
    ).strip()
    tenancy = str(outputs.get("tenancy_name") or "").strip()
    domain = str(outputs.get("identity_domain_name") or "Default").strip()
    if not endpoint or not tenancy:
        return ""
    host = endpoint.split("://", 1)[-1].split("/", 1)[0]
    if not host:
        return ""
    if not host.endswith(".datalake.oci.oraclecloud.com"):
        host = f"{host}.datalake.oci.oraclecloud.com"
    return f"https://{host}#?tenant={tenancy}&domain={domain}"


def aidp_alias_endpoint(alias_key: str, region: str) -> str:
    if not alias_key:
        return ""
    import oci

    region_key = next(
        (
            short_name
            for short_name, region_name in oci.regions.REGIONS_SHORT_NAMES.items()
            if region_name == region
        ),
        "",
    )
    if not region_key:
        raise ReconcileError(f"OCI SDK has no short region key for {region}")
    return alias_key if alias_key.endswith(region_key) else f"{alias_key}{region_key}"


def wait_for_application(application_url: str, *, attempts: int = 60) -> None:
    if not application_url.startswith("https://"):
        raise ReconcileError("application_url must use HTTPS")
    health_url = f"{application_url.rstrip('/')}/api/health"
    session = requests.Session()
    for _ in range(attempts):
        try:
            response = session.get(health_url, timeout=(5, 10), verify=False)
            if response.status_code == 200 and response.json().get("status") == "ok":
                return
        except (requests.exceptions.RequestException, ValueError):
            pass
        _sleep(5)
    raise ReconcileError("Registration application did not become healthy over HTTPS")


def resolve_workbench_url(outputs: dict[str, Any], config: dict[str, Any], signer: Any) -> str:
    direct_url = workbench_url(outputs)
    if direct_url:
        return direct_url
    try:
        import oci

        platform = oci.ai_data_platform.AiDataPlatformClient(config, signer=signer).get_ai_data_platform(
            str(outputs["ai_data_platform_id"])
        ).data
        enriched_outputs = {
            **outputs,
            "aidp_web_socket_endpoint": getattr(platform, "web_socket_endpoint", ""),
            "aidp_alias_endpoint": aidp_alias_endpoint(
                str(getattr(platform, "alias_key", "") or ""),
                str(config["region"]),
            ),
        }
        return workbench_url(enriched_outputs)
    except Exception:
        # ponytail: an endpoint can appear after the Workbench is active; Settings remains the admin fallback.
        return ""


def deliver_operator_credentials(
    oci_module: Any,
    config: dict[str, Any],
    signer: Any,
    outputs: dict[str, Any],
    region: str,
    object_storage: Any,
    config_text: str,
    key_text: str,
    wallet: bytes,
    wallet_password: str,
    admin_password: str,
) -> bool:
    if str(outputs["operator_user_ocid"]) != str(config.get("user") or ""):
        raise ReconcileError("Terraform operator_user_ocid does not match the uploaded OCI config")
    run_config = {**config, "region": region}
    run_client = oci_module.compute_instance_agent.ComputeInstanceAgentClient(
        run_config, signer=signer
    )
    public_key = fetch_bootstrap_public_key(
        run_client,
        oci_module,
        str(outputs["compartment_ocid"]),
        str(outputs["instance_id"]),
    )
    if public_key == BOOTSTRAP_READY:
        delete_bootstrap_object(oci_module, object_storage, outputs)
        return False
    database_operator = bootstrap_autonomous_governance(
        wallet,
        wallet_password,
        admin_password,
    )
    operator_username, operator_password, dsn = database_operator
    envelope = encrypt_bootstrap_credentials(
        public_key,
        config_text,
        key_text,
        wallet,
        wallet_password,
        operator_username,
        operator_password,
        dsn,
    )
    delete_bootstrap_object(oci_module, object_storage, outputs)
    try:
        object_storage.put_object(
            str(outputs["objectstorage_namespace"]),
            str(outputs["bucket_name"]),
            BOOTSTRAP_OBJECT_NAME,
            envelope,
            content_type="application/json",
            if_none_match="*",
        )
    except oci_module.exceptions.ServiceError as exc:
        raise ReconcileError(f"Encrypted VM credential delivery failed with OCI {exc.status}") from exc
    return True


def delete_bootstrap_object(oci_module: Any, object_storage: Any, outputs: dict[str, Any]) -> None:
    try:
        object_storage.delete_object(
            str(outputs["objectstorage_namespace"]),
            str(outputs["bucket_name"]),
            BOOTSTRAP_OBJECT_NAME,
        )
    except oci_module.exceptions.ServiceError as exc:
        if exc.status != 404:
            raise ReconcileError(f"Encrypted VM credential cleanup failed with OCI {exc.status}") from exc


def wait_for_bootstrap_consumed(
    oci_module: Any,
    object_storage: Any,
    outputs: dict[str, Any],
    *,
    attempts: int = 120,
) -> None:
    for _ in range(attempts):
        try:
            object_storage.head_object(
                str(outputs["objectstorage_namespace"]),
                str(outputs["bucket_name"]),
                BOOTSTRAP_OBJECT_NAME,
            )
        except oci_module.exceptions.ServiceError as exc:
            if exc.status == 404:
                return
            raise ReconcileError(f"Encrypted VM credential status failed with OCI {exc.status}") from exc
        _sleep(5)
    raise ReconcileError("Timed out waiting for the VM to consume encrypted OCI credentials")


def build_success_result(
    context: dict[str, Any], reconciled: dict[str, Any], messages: list[str], aidp_url: str = ""
) -> dict[str, Any]:
    resources = {**reconciled, "aidp_workbench_url": aidp_url}
    summary = {
        "schema_version": 2,
        "deployment_id": context["deployment_id"],
        "source": context["source"],
        "resources": resources,
    }
    return {
        "events": [{"level": "info", "message": message} for message in messages],
        "artifacts": [
            {
                "name": "aidp_lab_summary.json",
                "content_type": "application/json",
                "content_b64": base64.b64encode(
                    (json.dumps(summary, indent=2, sort_keys=True) + "\n").encode()
                ).decode(),
            }
        ],
        "outputs": {
            "aidp_workbench_url": aidp_url,
            "aidp_catalog_name": str(reconciled.get("catalog_name") or CATALOG_NAME),
            "aidp_shared_compute_name": str(
                reconciled.get("shared_compute_name") or SHARED_COMPUTE_NAME
            ),
            "aidp_runtime_ready": bool(reconciled.get("runtime_ready")),
            "aidp_external_volume_count": int(
                reconciled.get("external_volume_count") or 0
            ),
        },
    }


def _wait_for_autonomous_available(database: Any, database_id: str, *, attempts: int = 120) -> None:
    item = database.get_autonomous_database(database_id).data
    state = str(getattr(item, "lifecycle_state", "") or "").upper()
    if state == "STOPPED":
        database.start_autonomous_database(database_id)
    elif state != "AVAILABLE":
        raise ReconcileError(f"Autonomous database is {state or 'not available'}")
    for _ in range(attempts):
        state = str(
            getattr(database.get_autonomous_database(database_id).data, "lifecycle_state", "") or ""
        ).upper()
        if state == "AVAILABLE":
            return
        if state in {"FAILED", "TERMINATED", "TERMINATING", "UNAVAILABLE"}:
            raise ReconcileError(f"Autonomous database entered terminal state {state}")
        _sleep(10)
    raise ReconcileError("Timed out waiting for Autonomous database availability")


def _response_bytes(response: Any) -> bytes:
    data = getattr(response, "data", None)
    if isinstance(data, bytes):
        return data
    content = getattr(data, "content", None)
    if isinstance(content, bytes):
        return content
    raw = getattr(data, "raw", None)
    if raw is not None and callable(getattr(raw, "read", None)):
        value = raw.read()
        if isinstance(value, bytes):
            return value
    raise ReconcileError("Autonomous wallet response did not contain bytes")


def _validate_wallet(wallet: bytes) -> bytes:
    if len(wallet) > 10 * 1024 * 1024:
        raise ReconcileError("Autonomous wallet exceeds the 10 MiB safety limit")
    try:
        with zipfile.ZipFile(BytesIO(wallet)) as archive:
            names = archive.namelist()
            if not names or any(
                name.startswith(("/", "\\")) or ".." in Path(name.replace("\\", "/")).parts
                for name in names
            ):
                raise ReconcileError("Autonomous wallet archive contains an unsafe path")
            if any(item.file_size > 5 * 1024 * 1024 for item in archive.infolist()):
                raise ReconcileError("Autonomous wallet archive contains an oversized entry")
    except zipfile.BadZipFile as exc:
        raise ReconcileError("Autonomous wallet archive is invalid") from exc
    return wallet


def prepare_autonomous_wallet(
    oci_module: Any,
    database: Any,
    database_id: str,
    database_mode: str,
    wallet_password: str,
) -> bytes:
    if database_mode == "existing":
        wallet_path = os.environ.get("DEPLOY_STUDIO_ADB_WALLET", "")
        if not wallet_path:
            raise ReconcileError("Existing Autonomous database mode requires the uploaded wallet")
        return _validate_wallet(Path(wallet_path).read_bytes())
    details = oci_module.database.models.GenerateAutonomousDatabaseWalletDetails(
        password=wallet_password
    )
    response = database.generate_autonomous_database_wallet(database_id, details)
    return _validate_wallet(_response_bytes(response))


def ensure_ai_features(
    oci_module: Any,
    config: dict[str, Any],
    signer: Any,
    platform_id: str,
    database_id: str,
    admin_password: str,
    *,
    attempts: int = 180,
) -> bool:
    client = oci_module.ai_data_platform.AiDataPlatformClient(config, signer=signer)
    platform = client.get_ai_data_platform(platform_id).data
    if bool(
        getattr(platform, "is_ai_feature_enabled", False)
        or getattr(platform, "ai_feature_enabled", False)
    ):
        return False
    details = oci_module.ai_data_platform.models.EnableAiFeatureDetails(
        vector_db_id=database_id,
        vector_db_admin_cred=admin_password,
    )
    try:
        response = client.enable_ai_feature(platform_id, details)
    except oci_module.exceptions.ServiceError as exc:
        if exc.status == 409:
            return False
        raise ReconcileError(f"AIDP AI feature enablement failed with OCI {exc.status}") from exc
    work_request_id = str((getattr(response, "headers", None) or {}).get("opc-work-request-id") or "")
    if not work_request_id:
        return True
    for _ in range(attempts):
        work_request = client.get_work_request(work_request_id).data
        status = str(getattr(work_request, "status", "") or "").upper()
        if status == "SUCCEEDED":
            return True
        if status in {"FAILED", "CANCELED", "CANCELING"}:
            raise ReconcileError(f"AIDP AI feature work request ended in {status}")
        _sleep(10)
    raise ReconcileError("Timed out enabling AIDP AI features")


GOVERNANCE_PACKAGE_SPEC = """
CREATE OR REPLACE PACKAGE AIDP_LAB_GOVERNANCE AUTHID DEFINER AS
  PROCEDURE ENSURE_PARTICIPANT(
    P_PARTICIPANT_KEY IN VARCHAR2,
    P_OWNER_PASSWORD IN VARCHAR2,
    P_READER_PASSWORD IN VARCHAR2
  );
  PROCEDURE PUT_METRIC(
    P_PARTICIPANT_KEY IN VARCHAR2,
    P_LAB_ID IN VARCHAR2,
    P_METRIC_NAME IN VARCHAR2,
    P_METRIC_VALUE IN VARCHAR2
  );
  PROCEDURE PUT_LINEAGE(
    P_PARTICIPANT_KEY IN VARCHAR2,
    P_LAB_ID IN VARCHAR2,
    P_LINEAGE_LEVEL IN VARCHAR2,
    P_RELATION_PATH IN VARCHAR2
  );
  PROCEDURE DROP_PARTICIPANT(P_PARTICIPANT_KEY IN VARCHAR2);
END AIDP_LAB_GOVERNANCE;
""".strip()

GOVERNANCE_PACKAGE_BODY = """
CREATE OR REPLACE PACKAGE BODY AIDP_LAB_GOVERNANCE AS
  FUNCTION VALIDATED_STEM(P_PARTICIPANT_KEY IN VARCHAR2) RETURN VARCHAR2 IS
    L_KEY VARCHAR2(64) := LOWER(TRIM(P_PARTICIPANT_KEY));
    L_NUMBER NUMBER;
  BEGIN
    IF L_KEY != TRIM(P_PARTICIPANT_KEY) OR NOT REGEXP_LIKE(L_KEY, '^u[0-9]+$', 'c') THEN
      RAISE_APPLICATION_ERROR(-20001, 'Invalid participant key');
    END IF;
    L_NUMBER := TO_NUMBER(SUBSTR(L_KEY, 2));
    IF L_NUMBER < 101 THEN
      RAISE_APPLICATION_ERROR(-20001, 'Invalid participant key');
    END IF;
    RETURN DBMS_ASSERT.SIMPLE_SQL_NAME(UPPER(L_KEY));
  END;

  FUNCTION QUOTED_PASSWORD(P_PASSWORD IN VARCHAR2) RETURN VARCHAR2 IS
  BEGIN
    IF NOT REGEXP_LIKE(P_PASSWORD, '^[A-Za-z0-9]{24,64}$', 'c') THEN
      RAISE_APPLICATION_ERROR(-20002, 'Invalid generated password');
    END IF;
    RETURN '"' || P_PASSWORD || '"';
  END;

  FUNCTION USER_EXISTS(P_USERNAME IN VARCHAR2) RETURN BOOLEAN IS
    L_COUNT NUMBER;
  BEGIN
    SELECT COUNT(*) INTO L_COUNT FROM ALL_USERS WHERE USERNAME = P_USERNAME;
    RETURN L_COUNT = 1;
  END;

  PROCEDURE ENSURE_USER(P_USERNAME IN VARCHAR2, P_PASSWORD IN VARCHAR2, P_QUOTA IN BOOLEAN) IS
  BEGIN
    IF USER_EXISTS(P_USERNAME) THEN
      BEGIN
        EXECUTE IMMEDIATE 'ALTER USER ' || P_USERNAME || ' IDENTIFIED BY ' || QUOTED_PASSWORD(P_PASSWORD) || ' ACCOUNT UNLOCK';
      EXCEPTION
        WHEN OTHERS THEN
          IF SQLCODE != -28007 THEN
            RAISE;
          END IF;
      END;
    ELSE
      EXECUTE IMMEDIATE 'CREATE USER ' || P_USERNAME || ' IDENTIFIED BY ' || QUOTED_PASSWORD(P_PASSWORD) ||
        CASE WHEN P_QUOTA THEN ' DEFAULT TABLESPACE DATA QUOTA 100M ON DATA' ELSE '' END;
    END IF;
    EXECUTE IMMEDIATE 'GRANT CREATE SESSION TO ' || P_USERNAME;
  END;

  PROCEDURE ENSURE_TABLES(P_OWNER IN VARCHAR2, P_READER IN VARCHAR2) IS
    L_COUNT NUMBER;
  BEGIN
    SELECT COUNT(*) INTO L_COUNT FROM ALL_TABLES WHERE OWNER = P_OWNER AND TABLE_NAME = 'LAB_METRICS';
    IF L_COUNT = 0 THEN
      EXECUTE IMMEDIATE 'CREATE TABLE ' || P_OWNER || '.LAB_METRICS (' ||
        'LAB_ID VARCHAR2(64) NOT NULL, METRIC_NAME VARCHAR2(128) NOT NULL, ' ||
        'METRIC_VALUE VARCHAR2(4000), UPDATED_AT TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL, ' ||
        'PRIMARY KEY (LAB_ID, METRIC_NAME))';
    END IF;
    SELECT COUNT(*) INTO L_COUNT FROM ALL_TABLES WHERE OWNER = P_OWNER AND TABLE_NAME = 'LINEAGE_RELATIONS';
    IF L_COUNT = 0 THEN
      EXECUTE IMMEDIATE 'CREATE TABLE ' || P_OWNER || '.LINEAGE_RELATIONS (' ||
        'LAB_ID VARCHAR2(64) NOT NULL, LINEAGE_LEVEL VARCHAR2(16) NOT NULL, ' ||
        'RELATION_PATH VARCHAR2(4000) NOT NULL, UPDATED_AT TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL)';
    END IF;
    EXECUTE IMMEDIATE 'GRANT SELECT ON ' || P_OWNER || '.LAB_METRICS TO ' || P_READER;
    EXECUTE IMMEDIATE 'GRANT SELECT ON ' || P_OWNER || '.LINEAGE_RELATIONS TO ' || P_READER;
    EXECUTE IMMEDIATE 'GRANT READ, WRITE ON DIRECTORY DATA_PUMP_DIR TO ' || P_READER;
  END;

  PROCEDURE ENSURE_PARTICIPANT(
    P_PARTICIPANT_KEY IN VARCHAR2,
    P_OWNER_PASSWORD IN VARCHAR2,
    P_READER_PASSWORD IN VARCHAR2
  ) IS
    L_STEM VARCHAR2(64) := VALIDATED_STEM(P_PARTICIPANT_KEY);
    L_OWNER VARCHAR2(128) := DBMS_ASSERT.SIMPLE_SQL_NAME(L_STEM || '_AGENT');
    L_READER VARCHAR2(128) := DBMS_ASSERT.SIMPLE_SQL_NAME(L_STEM || '_AGENT_RO');
  BEGIN
    ENSURE_USER(L_OWNER, P_OWNER_PASSWORD, TRUE);
    ENSURE_USER(L_READER, P_READER_PASSWORD, FALSE);
    ENSURE_TABLES(L_OWNER, L_READER);
  END;

  PROCEDURE PUT_METRIC(
    P_PARTICIPANT_KEY IN VARCHAR2,
    P_LAB_ID IN VARCHAR2,
    P_METRIC_NAME IN VARCHAR2,
    P_METRIC_VALUE IN VARCHAR2
  ) IS
    L_OWNER VARCHAR2(128) := DBMS_ASSERT.SIMPLE_SQL_NAME(VALIDATED_STEM(P_PARTICIPANT_KEY) || '_AGENT');
  BEGIN
    IF P_LAB_ID IS NULL OR LENGTH(P_LAB_ID) > 64 OR P_METRIC_NAME IS NULL OR
       LENGTH(P_METRIC_NAME) > 128 OR LENGTH(P_METRIC_VALUE) > 4000 THEN
      RAISE_APPLICATION_ERROR(-20003, 'Invalid governance metric');
    END IF;
    EXECUTE IMMEDIATE 'MERGE INTO ' || L_OWNER || '.LAB_METRICS T USING (' ||
      'SELECT :1 LAB_ID, :2 METRIC_NAME, :3 METRIC_VALUE FROM DUAL) S ' ||
      'ON (T.LAB_ID = S.LAB_ID AND T.METRIC_NAME = S.METRIC_NAME) ' ||
      'WHEN MATCHED THEN UPDATE SET T.METRIC_VALUE = S.METRIC_VALUE, T.UPDATED_AT = SYSTIMESTAMP ' ||
      'WHEN NOT MATCHED THEN INSERT (LAB_ID, METRIC_NAME, METRIC_VALUE, UPDATED_AT) ' ||
      'VALUES (S.LAB_ID, S.METRIC_NAME, S.METRIC_VALUE, SYSTIMESTAMP)'
      USING P_LAB_ID, P_METRIC_NAME, P_METRIC_VALUE;
  END;

  PROCEDURE PUT_LINEAGE(
    P_PARTICIPANT_KEY IN VARCHAR2,
    P_LAB_ID IN VARCHAR2,
    P_LINEAGE_LEVEL IN VARCHAR2,
    P_RELATION_PATH IN VARCHAR2
  ) IS
    L_OWNER VARCHAR2(128) := DBMS_ASSERT.SIMPLE_SQL_NAME(VALIDATED_STEM(P_PARTICIPANT_KEY) || '_AGENT');
  BEGIN
    IF P_LAB_ID IS NULL OR LENGTH(P_LAB_ID) > 64 OR
       P_LINEAGE_LEVEL NOT IN ('ENTITY', 'COLUMN') OR
       P_RELATION_PATH IS NULL OR LENGTH(P_RELATION_PATH) > 4000 THEN
      RAISE_APPLICATION_ERROR(-20004, 'Invalid governance lineage');
    END IF;
    EXECUTE IMMEDIATE 'MERGE INTO ' || L_OWNER || '.LINEAGE_RELATIONS T USING (' ||
      'SELECT :1 LAB_ID, :2 LINEAGE_LEVEL, :3 RELATION_PATH FROM DUAL) S ' ||
      'ON (T.LAB_ID = S.LAB_ID AND T.LINEAGE_LEVEL = S.LINEAGE_LEVEL AND T.RELATION_PATH = S.RELATION_PATH) ' ||
      'WHEN MATCHED THEN UPDATE SET T.UPDATED_AT = SYSTIMESTAMP ' ||
      'WHEN NOT MATCHED THEN INSERT (LAB_ID, LINEAGE_LEVEL, RELATION_PATH, UPDATED_AT) ' ||
      'VALUES (S.LAB_ID, S.LINEAGE_LEVEL, S.RELATION_PATH, SYSTIMESTAMP)'
      USING P_LAB_ID, P_LINEAGE_LEVEL, P_RELATION_PATH;
  END;

  PROCEDURE DROP_PARTICIPANT(P_PARTICIPANT_KEY IN VARCHAR2) IS
    L_STEM VARCHAR2(64) := VALIDATED_STEM(P_PARTICIPANT_KEY);
    L_OWNER VARCHAR2(128) := DBMS_ASSERT.SIMPLE_SQL_NAME(L_STEM || '_AGENT');
    L_READER VARCHAR2(128) := DBMS_ASSERT.SIMPLE_SQL_NAME(L_STEM || '_AGENT_RO');
  BEGIN
    IF USER_EXISTS(L_READER) THEN
      EXECUTE IMMEDIATE 'DROP USER ' || L_READER || ' CASCADE';
    END IF;
    IF USER_EXISTS(L_OWNER) THEN
      EXECUTE IMMEDIATE 'DROP USER ' || L_OWNER || ' CASCADE';
    END IF;
  END;
END AIDP_LAB_GOVERNANCE;
""".strip()


def _generated_database_password() -> str:
    alphabet = string.ascii_letters + string.digits
    while True:
        value = "".join(secrets.choice(alphabet) for _ in range(32))
        if any(c.islower() for c in value) and any(c.isupper() for c in value) and any(c.isdigit() for c in value):
            return value


def _wallet_dsn(wallet_dir: Path) -> str:
    aliases = re.findall(
        r"(?mi)^\s*([A-Za-z][A-Za-z0-9_.-]*)\s*=",
        (wallet_dir / "tnsnames.ora").read_text(encoding="utf-8"),
    )
    if not aliases:
        raise ReconcileError("Autonomous wallet has no TNS aliases")
    return next((alias for alias in aliases if alias.lower().endswith("_low")), aliases[0])


def bootstrap_autonomous_governance(
    wallet: bytes,
    wallet_password: str,
    admin_password: str,
) -> tuple[str, str, str]:
    """Install the allowlisted definer package, then return a rotated EXECUTE-only login."""
    import oracledb

    operator_password = _generated_database_password()
    with tempfile.TemporaryDirectory(prefix="aidp-wallet-") as temporary:
        wallet_dir = Path(temporary)
        with zipfile.ZipFile(BytesIO(_validate_wallet(wallet))) as archive:
            archive.extractall(wallet_dir)
        dsn = _wallet_dsn(wallet_dir)
        try:
            with oracledb.connect(
                user="ADMIN",
                password=admin_password,
                dsn=dsn,
                config_dir=str(wallet_dir),
                wallet_location=str(wallet_dir),
                wallet_password=wallet_password,
            ) as connection:
                cursor = connection.cursor()
                cursor.execute(GOVERNANCE_PACKAGE_SPEC)
                cursor.execute(GOVERNANCE_PACKAGE_BODY)
                cursor.execute(
                    "SELECT COUNT(*) FROM ALL_USERS WHERE USERNAME = :username",
                    username=DATABASE_OPERATOR,
                )
                exists = int(cursor.fetchone()[0]) == 1
                quoted_password = f'"{operator_password}"'
                cursor.execute(
                    ("ALTER USER " if exists else "CREATE USER ")
                    + DATABASE_OPERATOR
                    + " IDENTIFIED BY "
                    + quoted_password
                    + (" ACCOUNT UNLOCK" if exists else "")
                )
                cursor.execute(f"GRANT CREATE SESSION TO {DATABASE_OPERATOR}")
                cursor.execute(
                    f"GRANT EXECUTE ON ADMIN.AIDP_LAB_GOVERNANCE TO {DATABASE_OPERATOR}"
                )
                connection.commit()
        except Exception as exc:
            raise ReconcileError(
                "Autonomous governance package bootstrap failed; ADMIN credentials were not persisted"
            ) from exc
    return DATABASE_OPERATOR, operator_password, dsn


def main() -> int:
    global _post_apply_deadline
    output_path = os.environ.get("DEPLOY_STUDIO_OUTPUT")
    if not output_path:
        print("DEPLOY_STUDIO_OUTPUT is required", file=sys.stderr)
        return 2
    _post_apply_deadline = time.monotonic() + POST_APPLY_BUDGET_SECONDS
    bootstrap_uploaded = False
    try:
        context = read_json_env("DEPLOY_STUDIO_CONTEXT")
        secret_document = read_json_env("DEPLOY_STUDIO_SECRETS")
        secret_inputs = secret_document.get("inputs")
        if not isinstance(secret_inputs, dict):
            raise ReconcileError("Deploy Studio secrets have an invalid shape")
        admin_password = str(secret_inputs.get("autonomous_database_admin_password") or "")
        wallet_password = str(secret_inputs.get("autonomous_database_wallet_password") or "")
        if len(admin_password) < 12 or len(wallet_password) < 12:
            raise ReconcileError("Autonomous database secrets are missing or invalid")
        config_path = os.environ["DEPLOY_STUDIO_OCI_CONFIG"]
        key_path = os.environ["DEPLOY_STUDIO_OCI_KEY"]
        outputs = context["terraform_outputs"]
        oci_config = load_oci_config(config_path, key_path)
        signer = build_signer(oci_config)
        import oci

        object_storage = oci.object_storage.ObjectStorageClient(oci_config, signer=signer)
        database = oci.database.DatabaseClient(oci_config, signer=signer)
        messages = describe_object_prefixes()
        api = AidpApi(context["region"], outputs["ai_data_platform_id"], signer, context["deployment_id"])
        reconciled, reconcile_messages = reconcile(api, outputs)
        messages.extend(reconcile_messages)
        key_text = Path(key_path).read_text(encoding="utf-8")
        credential_api = AidpApi(
            context["region"],
            outputs["ai_data_platform_id"],
            signer,
            context["deployment_id"],
            api_version=GOVERNANCE_API_VERSION,
            resource_segment="aiDataPlatforms",
        )
        credential_created = ensure_governance_operator_credential(
            credential_api,
            oci_config,
            key_text,
            str(context["region"]),
        )
        messages.append(
            "AIDP governance operator credential created"
            if credential_created
            else "AIDP governance operator credential rotated"
        )
        aidp_url = resolve_workbench_url(outputs, oci_config, signer)
        if not aidp_url:
            raise ReconcileError("AIDP Workbench direct URL is not published yet")
        database_id = str(outputs["autonomous_database_id"])
        _wait_for_autonomous_available(database, database_id)
        wallet = prepare_autonomous_wallet(
            oci,
            database,
            database_id,
            str(outputs["autonomous_database_mode"]),
            wallet_password,
        )
        ai_enabled = ensure_ai_features(
            oci,
            oci_config,
            signer,
            str(outputs["ai_data_platform_id"]),
            database_id,
            admin_password,
        )
        messages.append(
            "AIDP AI features enabled with the deployment Autonomous database"
            if ai_enabled
            else "AIDP AI features already enabled"
        )
        bootstrap_uploaded = deliver_operator_credentials(
            oci,
            oci_config,
            signer,
            outputs,
            str(context["region"]),
            object_storage,
            render_runtime_oci_config(oci_config),
            key_text,
            wallet,
            wallet_password,
            admin_password,
        )
        if bootstrap_uploaded:
            messages.append(
                "Autonomous governance package installed; encrypted wallet and EXECUTE-only operator delivered to the VM"
            )
            wait_for_bootstrap_consumed(oci, object_storage, outputs)
            bootstrap_uploaded = False
            messages.append("Registration VM consumed and deleted the encrypted bootstrap object")
        else:
            messages.append("Registration VM already has the validated Autonomous bootstrap v2 runtime")
        wait_for_application(str(outputs["application_url"]))
        messages.append("Registration application is healthy over HTTPS")
        reconciled["runtime_ready"] = True
        write_result(output_path, build_success_result(context, reconciled, messages, aidp_url))
        return 0
    except (KeyError, OSError, ValueError, ReconcileError) as exc:
        if bootstrap_uploaded:
            try:
                delete_bootstrap_object(oci, object_storage, outputs)
            except Exception as cleanup_exc:
                exc = ReconcileError(
                    f"{exc}; encrypted bootstrap cleanup failed: {type(cleanup_exc).__name__}"
                )
        write_result(output_path, {"events": [{"level": "error", "message": str(exc)}], "artifacts": [], "outputs": {}})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
