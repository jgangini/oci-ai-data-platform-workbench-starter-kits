"""Invocation-bound catalog reads signed by AIDP's native caller context.

The remote signer delegates signing to Oracle. No runtime API-key credential or
browser-supplied identity is accepted here. Server-side asset authorization still
requires native positive/denied acceptance; scope validation is not an ACL.
"""

from __future__ import annotations

from urllib.parse import quote, unquote
from uuid import UUID

from .code_context import _validate_source_read
from .lineage import LineageTransport
from .service_transport import runtime_signer


def _caller_identity(get_context):
    context = get_context()
    principal = getattr(context, "dh_user_principal", None)
    subject = getattr(context, "subject_id", None)
    session = getattr(context, "session_id", None)
    if (not isinstance(principal, str) or not principal.strip() or not isinstance(subject, str) or not subject.strip()
            or not isinstance(session, str) or not session.strip() or len(session) > 1024):
        raise PermissionError("A native AIDP caller context is required")
    return principal, subject, session


class CallerMetadataTransport(LineageTransport):
    """Trusted adapter dependencies are constructed inside one invocation."""

    def __init__(self, config, signer, session, *, get_context, allow_code_reads=False):
        if type(allow_code_reads) is not bool:
            raise ValueError("Code-source admission must be explicit")
        self.code_reads_enabled = allow_code_reads
        self._get_context = get_context
        self._identity = _caller_identity(get_context)
        super().__init__(config, signer, session)

    def __call__(self, method, path, *, params=None, payload=None):
        self.check_identity()
        if method == "GET" and payload is None:
            code_kind = self._admit_code_read(path, params) if self.code_reads_enabled else None
            if code_kind is not None:
                result = self._exchange(method, path, params=params, text=code_kind == "text")
                self.check_identity()
                return result
            self._admit_catalog_read(path, params)
        elif method != "POST" or path != "/actions/fetchLineage":
            raise ValueError("Consultation admits catalog and lineage reads only")
        return super().__call__(method, path, params=params, payload=payload)

    def _admit_code_read(self, path, params):
        if not isinstance(path, str):
            return None
        root = f"/workspaces/{self.workspace}"
        if path == root + "/jobs" and params == {"limit": "32"}:
            return "json"
        if params is not None:
            return None
        prefix = root + "/jobs/"
        if path.startswith(prefix):
            key = path[len(prefix):]
            try:
                if str(UUID(key)) == key:
                    return "json"
            except ValueError:
                pass
            raise ValueError("A canonical job identity is required")
        prefix = root + "/objects/"
        if path.startswith(prefix):
            _validate_source_read(path, prefix)
            return "text"
        return None

    def check_identity(self):
        if _caller_identity(self._get_context) != self._identity:
            raise PermissionError("The native caller invocation changed")

    def _admit_catalog_read(self, path, params):
        if path == "/tables":
            self._admit_catalog_page(params)
            return
        prefix = "/tables/"
        key = unquote(path[len(prefix):]) if isinstance(path, str) and path.startswith(prefix) else ""
        if (params not in (None, {}) or not key or len(key) > 1024
                or path != prefix + quote(key, safe="") or any(c in key for c in "/\\?#")
                or not any(key.startswith(s.schema_key + ".") for s in self.scopes)):
            raise ValueError("Table read is outside configured source schemas")

    def _admit_catalog_page(self, params):
        if not isinstance(params, dict) or set(params) - {"catalogKey", "schemaKey", "limit", "page"}:
            raise ValueError("An explicitly scoped catalog page is required")
        if not any(params.get("catalogKey") == s.catalog_key and params.get("schemaKey") == s.schema_key for s in self.scopes):
            raise ValueError("Catalog page is outside configured source schemas")
        limit = params.get("limit")
        if not isinstance(limit, str) or not limit.isascii() or not limit.isdecimal() or not 1 <= int(limit) <= 100:
            raise ValueError("Catalog page exceeds its bound")
        if "page" in params and (not isinstance(params["page"], str) or not 1 <= len(params["page"]) <= 8192):
            raise ValueError("Invalid catalog cursor")


def native_caller_transport(config, *, allow_code_reads=False):
    """Call inside flow_setup; never place the transport in a checkpoint."""
    import requests
    from aidputils.agents.auth.signer.custom_remote_signer import CustomRemoteSigner
    from aidputils.agents.auth.util import auth_utils

    _caller_identity(auth_utils.get_auth_context)
    return CallerMetadataTransport(config, CustomRemoteSigner(), requests.Session(),
        get_context=auth_utils.get_auth_context, allow_code_reads=allow_code_reads)


def native_central_transport(config):
    """Use the installed technical credential; caller context isolates sessions only.

    Source and catalog visibility follow the central installation identity, never
    the questioning user's resource ACL. No fallback to delegated signing occurs.
    """
    import aidputils
    import requests
    from aidputils.agents.auth.util import auth_utils

    _caller_identity(auth_utils.get_auth_context)
    signer = runtime_signer(config, aidputils.secrets.get)
    transport = CallerMetadataTransport(config, signer, requests.Session(),
        get_context=auth_utils.get_auth_context, allow_code_reads=True)
    transport.access_mode = "CENTRAL_METADATA"
    return transport
