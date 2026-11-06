#!/usr/bin/env python3
"""Additively reconcile AIDP Master Catalog resources after Terraform APPLY."""

from __future__ import annotations

import base64
import configparser
import hashlib
import json
import os
import re
import sys
import time
import uuid
from io import StringIO
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from oci._vendor import requests


API_VERSION = "20240831"
CATALOG_NAME = "oci_medallion"
DEVELOPER_ROLE_NAME = "AIDP_DEVELOPER"
LEGACY_DEVELOPER_ROLE_NAME = "AIDP_LAB_DEVELOPER"
PENDING_ROLE_NAME = "AIDP_LAB_PENDING"
SHARED_COMPUTE_NAME = "aidp_cluster_shared_compute"
BOOTSTRAP_OBJECT_NAME = ".bootstrap/operator-credentials.json"
BOOTSTRAP_VERSION = 3
BOOTSTRAP_READY = "AIDP_LAB_CREDENTIALS_V3_READY"
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
            request_headers.setdefault("opc-retry-token", str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"{self.deployment_id}:{method.upper()}:{path}:{object_type}:{payload_hash}"
                    + (f":{request_headers['path']}" if request_headers.get("path") else ""),
                )
            ))
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
    owner = getattr(api, "resource_owner", "")
    if owner:
        description = f"{create_payload.get('description', '')} [{owner}]".strip()
        create_payload = {**create_payload, "description": description}
        immutable_fields = {**immutable_fields, "description": description}
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
    principal_label: str = "deployment operator",
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
            f"AIDP Workbench did not authorize the {principal_label} after the "
            f"readiness window; last request: {last_not_ready}"
        ) from last_not_ready
    raise ReconcileError(f"OCI {principal_label} is not an AI_DATA_PLATFORM_ADMIN member")


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


def retire_legacy_developer_role(
    api: AidpApi,
    new_role_key: str,
    developer_group_ocid: str,
    expected_permissions: set[tuple[str, str, frozenset[str]]],
) -> bool:
    """Remove the old role only after the replacement is demonstrably equivalent."""
    assert_role_members_exact(
        api, new_role_key, DEVELOPER_ROLE_NAME, "GROUP", developer_group_ocid
    )
    assert_role_permissions_exact(
        api, new_role_key, DEVELOPER_ROLE_NAME, expected_permissions
    )
    legacy = exact_one(
        api.list_all("/roles", params={"displayName": LEGACY_DEVELOPER_ROLE_NAME}),
        LEGACY_DEVELOPER_ROLE_NAME,
        "legacy developer role",
    )
    if legacy is None:
        return False
    legacy_key = str(legacy.get("key") or "")
    if not legacy_key:
        raise ReconcileError("Legacy developer role has no key")
    assert_role_members_exact(
        api, legacy_key, LEGACY_DEVELOPER_ROLE_NAME, "GROUP", developer_group_ocid
    )
    assert_role_permissions_exact(
        api, legacy_key, LEGACY_DEVELOPER_ROLE_NAME, expected_permissions
    )
    api.request("DELETE", f"/roles/{quote(legacy_key, safe='')}")
    if exact_one(
        api.list_all("/roles", params={"displayName": LEGACY_DEVELOPER_ROLE_NAME}),
        LEGACY_DEVELOPER_ROLE_NAME,
        "legacy developer role",
    ) is not None:
        raise ReconcileError("Legacy developer role remained visible after deletion")
    return True


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
            {**item, "_listed_schema": schema.get("displayName")} for item in api.list_all(
                "/volumes",
                params={"catalogKey": catalog_key, "schemaKey": schema_key},
            )
        )
    external: list[dict[str, Any]] = []
    for volume in volumes:
        details = volume
        if volume.get("key") and not volume.get("storageLocation"):
            response = api.request("GET", f"/volumes/{volume['key']}").body
            details = {**response, "_listed_schema": volume.get("_listed_schema")} if isinstance(response, dict) else volume
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
            f"Fresh-only bootstrap found an unapproved external volume in {CATALOG_NAME}; no resources were deleted"
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


def require_existing_platform_isolation(api: AidpApi, workspace_key: str) -> None:
    """Refuse unrelated resources before assigning permissions or writing content."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", api.deployment_id):
        raise ReconcileError("A stable deployment identity is required for platform reuse")
    owner = f"starter-kits:{api.deployment_id}"
    catalogs = api.list_all("/catalogs", params={"displayName": CATALOG_NAME})
    targets = [
        (catalogs, {CATALOG_NAME}),
        (api.list_all(f"/workspaces/{workspace_key}/clusters"), {SHARED_COMPUTE_NAME}),
        (api.list_all("/roles"), {DEVELOPER_ROLE_NAME, PENDING_ROLE_NAME, LEGACY_DEVELOPER_ROLE_NAME}),
    ]
    for resources, names in targets:
        for resource in resources:
            if resource.get("displayName") in names and not str(resource.get("description", "")).endswith(f"[{owner}]"):
                raise ReconcileError(f"Existing AIDP resource {resource.get('displayName')} is not owned by this deployment; no adoption is allowed")
    folder = workspace_object(api, workspace_key, "/Workspace/medallon")
    if folder is not None and not any(item.get("displayName") == CATALOG_NAME for item in catalogs):
        raise ReconcileError("Existing kit workspace folder has no owned catalog; refusing adoption")
    api.resource_owner = owner


def selected_existing_workspace(api: AidpApi, key: str) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", key):
        raise ReconcileError("Select a valid existing workspace key")
    matches = [item for item in api.list_all("/workspaces") if item.get("key") == key]
    if len(matches) != 1:
        raise ReconcileError("Selected workspace does not belong to the selected AIDP or is inaccessible")
    workspace = matches[0]
    if str(workspace.get("lifecycleState") or "").upper() != "ACTIVE":
        raise ReconcileError("Selected workspace must be ACTIVE")
    if not re.fullmatch(r"[A-Za-z0-9 _.-]{1,128}", str(workspace.get("displayName") or "")):
        raise ReconcileError("Selected workspace has an unsupported runtime display name")
    return workspace


def reconcile(api: AidpApi, outputs: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    events: list[str] = []
    assert_operator_platform_admin(api, str(outputs["operator_user_ocid"]))
    events.append("Deployment operator AI_DATA_PLATFORM_ADMIN membership verified")
    if outputs.get("aidp_mode") == "existing":
        workspace = selected_existing_workspace(api, str(outputs.get("existing_workspace_key") or ""))
        if workspace["displayName"] != outputs.get("existing_workspace_name"):
            raise ReconcileError("Selected workspace changed after preflight; validate deployment again")
        require_existing_platform_isolation(api, str(workspace["key"]))
    else:
        workspace = wait_for_existing_active(
            api, "/workspaces", "workspace", str(outputs["default_workspace_name"]),
            {"type": "DEFAULT"},
        )

    catalog, created = ensure_resource(
        api,
        "/catalogs",
        "catalog",
        CATALOG_NAME,
        {
            "displayName": CATALOG_NAME,
            "description": "Medallion catalog for participant starter-kit data",
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
        api, catalog_key, namespace, bucket,
    )
    events.append(f"Fresh-only catalog verified: zero legacy schemas; {external_volume_count} external volumes")
    workspace_key = str(workspace["key"])
    shared_compute, compute_created = ensure_resource(
        api,
        f"/workspaces/{workspace_key}/clusters",
        "shared compute",
        SHARED_COMPUTE_NAME,
        {
            "type": "USER",
            "displayName": SHARED_COMPUTE_NAME,
            "description": "Shared Spark compute for participant lab workflows",
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
    legacy_retired = retire_legacy_developer_role(
        api,
        role_keys[DEVELOPER_ROLE_NAME],
        str(outputs["developer_group_ocid"]),
        expected_permissions[DEVELOPER_ROLE_NAME],
    )
    events.append(
        f"Legacy role {LEGACY_DEVELOPER_ROLE_NAME} "
        f"{'retired' if legacy_retired else 'not present'} after equivalence validation"
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


def wait_for_application(application_url: str, *, attempts: int = 60, verify_tls: bool = False) -> None:
    if not application_url.startswith("https://"):
        raise ReconcileError("application_url must use HTTPS")
    health_url = f"{application_url.rstrip('/')}/api/health"
    session = requests.Session()
    for _ in range(attempts):
        try:
            response = session.get(health_url, timeout=(5, 10), verify=verify_tls)
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
    envelope = encrypt_bootstrap_credentials(
        public_key,
        config_text,
        key_text,
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
        config_path = os.environ["DEPLOY_STUDIO_OCI_CONFIG"]
        key_path = os.environ["DEPLOY_STUDIO_OCI_KEY"]
        outputs = context["terraform_outputs"]
        oci_config = load_oci_config(config_path, key_path)
        signer = build_signer(oci_config)
        import oci

        object_storage = oci.object_storage.ObjectStorageClient(oci_config, signer=signer)
        messages = describe_object_prefixes()
        api = AidpApi(context["region"], outputs["ai_data_platform_id"], signer, context["deployment_id"])
        reconciled, reconcile_messages = reconcile(api, outputs)
        messages.extend(reconcile_messages)
        key_text = Path(key_path).read_text(encoding="utf-8")
        aidp_url = resolve_workbench_url(outputs, oci_config, signer)
        if not aidp_url:
            raise ReconcileError("AIDP Workbench direct URL is not published yet")
        bootstrap_uploaded = deliver_operator_credentials(
            oci,
            oci_config,
            signer,
            outputs,
            str(context["region"]),
            object_storage,
            render_runtime_oci_config(oci_config),
            key_text,
        )
        if bootstrap_uploaded:
            messages.append(
                "Encrypted OCI operator credentials delivered to the VM"
            )
            wait_for_bootstrap_consumed(oci, object_storage, outputs)
            bootstrap_uploaded = False
            messages.append("Registration VM consumed and deleted the encrypted bootstrap object")
        else:
            messages.append("Registration VM already has the validated operator credential bootstrap")
        wait_for_application(str(outputs["application_url"]), verify_tls=outputs.get("public_ip_tls_enabled") is True)
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
