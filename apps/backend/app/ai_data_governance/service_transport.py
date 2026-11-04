"""Read-only service metadata transport for the scheduled inventory task.

The configured runtime credential is not the interactive user's identity. This
adapter is never exposed as an assistant tool or used to answer arbitrary SQL.
"""

from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import unquote


def runtime_signer(config: dict, secret_get):
    import oci

    expected = config.get("identity_sha256")
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("An explicit runtime identity binding is required")
    try:
        values = {key: secret_get(name=config["credential_name"], key=key)
                  for key in ("tenancy", "user", "fingerprint", "region")}
        if any(not isinstance(value, str) or not value for value in values.values()):
            raise ValueError("Incomplete runtime identity")
        identity = hashlib.sha256(json.dumps([values[key] for key in ("tenancy", "user", "fingerprint")],
                                             separators=(",", ":")).encode()).hexdigest()
        if identity != expected or values["region"] != config["region"]:
            raise ValueError("Runtime identity differs from the installation binding")
        private_key = secret_get(name=config["credential_name"], key="private_key")
        if not isinstance(private_key, str) or not private_key:
            raise ValueError("Missing runtime credential")
        return oci.signer.Signer(tenancy=values["tenancy"], user=values["user"],
            fingerprint=values["fingerprint"], private_key_file_location=None, private_key_content=private_key)
    except Exception:
        raise RuntimeError("The bound Governance runtime credential is unavailable") from None


def runtime_sdk_config(config: dict, signer, secret_get):
    """Build the OCI SDK config in memory after binding the existing API signer."""
    identity_fields = getattr(signer, "api_key", "").split("/")
    if len(identity_fields) != 3:
        raise ValueError("The existing runtime API-key signer is required")
    observed = hashlib.sha256(json.dumps(identity_fields, separators=(",", ":")).encode()).hexdigest()
    if observed != config["identity_sha256"]:
        raise ValueError("SDK identity differs from the installation")
    tenancy, user, fingerprint = identity_fields
    client_config = {"region": config["region"], "tenancy": tenancy, "user": user, "fingerprint": fingerprint,
        "key_content": secret_get(name=config["credential_name"], key="private_key")}
    return client_config


def runtime_object_storage(config: dict, signer, secret_get):
    """OCI API-key clients validate the complete config even with a supplied signer."""
    import oci

    client_config = runtime_sdk_config(config, signer, secret_get)
    try:
        return oci.object_storage.ObjectStorageClient(client_config, signer=signer,
            timeout=(10, 60), retry_strategy=oci.retry.NoneRetryStrategy())
    except Exception:
        raise RuntimeError("The bound Governance Object Storage client is unavailable") from None


class MetadataTransport:
    """Only the installation's platform and metadata/control-run GET endpoints."""

    def __init__(self, config: dict, signer, session):
        region = config["region"]
        platform = config["platform_id"]
        workspace = config["workspace_key"]
        if not re.fullmatch(r"[a-z]+-[a-z]+-\d+", region) or not re.fullmatch(
                r"ocid1\.aidataplatform\.oc1\." + re.escape(region) + r"\.[a-z0-9]+", platform):
            raise ValueError("Invalid AIDP installation binding")
        if not re.fullmatch(r"[a-f0-9-]{36}", workspace):
            raise ValueError("Invalid workspace binding")
        self.base = f"https://datalake.{region}.oci.oraclecloud.com/20260430/aiDataPlatforms/{platform}"
        self.workspace = workspace
        self.signer = signer
        self.session = session

    def __call__(self, method: str, path: str, *, params=None):
        prefixes = [f"/workspaces/{self.workspace}/{kind}/" for kind in ("jobRuns", "taskRuns")]
        table_key = unquote(path[len("/tables/"):]) if path.startswith("/tables/") else ""
        table_path = re.fullmatch(r"/tables/[^/?#]+", path) and not any(c in table_key for c in "/\\?#")
        allowed = path in {"/roles", "/catalogs", "/schemas", "/tables"} or table_path or any(
            path.startswith(prefix) and re.fullmatch(r"[a-f0-9-]{36}", path[len(prefix):]) for prefix in prefixes)
        if method != "GET" or not allowed:
            raise ValueError("The inventory transport admits metadata GET operations only")
        return self._exchange(method, path, params=params)

    def _exchange(self, method: str, path: str, *, params=None, payload=None, text=False):
        try:
            options = {"json": payload} if payload is not None else {}
            with self.session.request(method, self.base + path, auth=self.signer, params=params, **options,
                    headers={"Accept": "*/*" if text else "application/json"}, timeout=(10, 60),
                    allow_redirects=False, stream=True) as response:
                if not 200 <= response.status_code < 300:
                    raise RuntimeError("Metadata request was rejected")
                parts, size = [], 0
                for part in response.iter_content(chunk_size=65536):
                    size += len(part)
                    if size > 16 * 1024 * 1024:
                        raise ValueError("Metadata response exceeds its bound")
                    parts.append(part)
                body = b"".join(parts)
                return (body.decode('utf-8') if text else json.loads(body)), dict(response.headers)
        except Exception:
            raise RuntimeError("Governance metadata request failed") from None
