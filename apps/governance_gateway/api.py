from __future__ import annotations

import hashlib
import os
import re
import time
from threading import Lock
from typing import Any, Literal, Protocol

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, Field

from .auth import AuthenticationError, OidcAuthenticator, OidcSettings
from .catalog import AidpCatalogClient, CatalogSyncError, CatalogSynchronizer
from .jdbc import AidpJdbcRuntime
from .policy import ColumnRef, ForbiddenColumns, GovernanceError, PolicyAction, Principal, effective_action
from .service import GovernanceService
from .store import JdbcControlStore
from .tokenization import KmsSettings, OciKmsCipher, TokenizationError, VaultTokenizer


class Authenticator(Protocol):
    def authenticate(self, authorization: str | None) -> Principal: ...


class PrincipalDirectory(Protocol):
    def principals(self) -> list[dict[str, str]]: ...


class QueryRequest(BaseModel):
    parameters: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class ClassificationRequest(BaseModel):
    classification: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")
    sensitivity: Literal["UNCLASSIFIED", "PUBLIC", "INTERNAL", "CONFIDENTIAL", "RESTRICTED", "CRITICAL"]
    owner: str = Field(min_length=1, max_length=255)
    review_state: Literal["UNREVIEWED", "IN_REVIEW", "APPROVED", "REJECTED"]


class AccessPolicyRequest(BaseModel):
    object_id: str = Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
    principal_type: Literal["USER", "GROUP", "ROLE"]
    principal_name: str = Field(min_length=1, max_length=255, pattern=r"^[^\x00-\x1f\x7f]+$")
    action: Literal["ALLOW", "DENY", "NULL", "MASK", "TOKENIZE"]
    priority: int = Field(default=0, ge=-1000, le=1000)
    enabled: bool = True


class TokenizeRequest(BaseModel):
    value: str = Field(max_length=16_384)


class SqlRequest(BaseModel):
    statement: str = Field(min_length=1, max_length=100_000)
    parameters: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class SqlAccessRequest(BaseModel):
    principal_type: Literal["USER", "GROUP", "ROLE"]
    principal_name: str = Field(min_length=1, max_length=255, pattern=r"^[^\x00-\x1f\x7f]+$")
    enabled: bool = True


class Readiness:
    def __init__(self, initialize: Any | None = None) -> None:
        self._initialize = initialize
        self._lock = Lock()
        self._ready = initialize is None
        self._retry_after = 0.0

    def ensure(self) -> bool:
        if self._ready:
            return True
        with self._lock:
            if self._ready:
                return True
            if time.monotonic() < self._retry_after:
                return False
            try:
                self._initialize()
            except Exception:
                self._retry_after = time.monotonic() + 30
                return False
            self._ready = True
            return True


def _principal_dependency(authenticator: Authenticator) -> Any:
    def principal(authorization: str | None = Header(default=None)) -> Principal:
        try:
            return authenticator.authenticate(authorization)
        except AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=str(exc), headers={"WWW-Authenticate": "Bearer"}) from exc

    return principal


def _require_ready(readiness: Readiness) -> None:
    if not readiness.ensure():
        raise HTTPException(status_code=503, detail="The governed AIDP connection is not ready.")


def _require_admin(identity: Principal) -> None:
    if not identity.is_admin:
        raise HTTPException(status_code=403, detail="AI_DATA_PLATFORM_ADMIN is required.")


def _require_admin_ready(identity: Principal, readiness: Readiness) -> None:
    _require_admin(identity)
    _require_ready(readiness)


def _principal_hash(identity: Principal) -> str:
    return hashlib.sha256(identity.subject.encode("utf-8")).hexdigest()


def _require_existing_principal(
    principal_directory: PrincipalDirectory | None, principal_type: str, principal_name: str
) -> None:
    if principal_directory is None:
        raise HTTPException(status_code=503, detail="The AIDP principal directory is not configured.")
    try:
        existing = principal_directory.principals()
    except CatalogSyncError as exc:
        raise HTTPException(status_code=503, detail="The AIDP principal directory is unavailable.") from exc
    if not any(
        item.get("principal_type") == principal_type and item.get("principal_name") == principal_name
        for item in existing
    ):
        raise HTTPException(status_code=422, detail="Select an existing principal visible through AIDP roles.")


def _require_scope(identity: Principal, scope: str) -> None:
    if scope not in identity.scopes:
        raise HTTPException(status_code=403, detail=f"The {scope} OAuth scope is required.")


def _register_base_routes(app: FastAPI, readiness: Readiness) -> None:
    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": "2.1.2"}

    @app.get("/readyz")
    def ready() -> dict[str, str]:
        _require_ready(readiness)
        return {"status": "ready"}

    @app.get("/metrics", include_in_schema=False)
    def metrics() -> str:
        return "aidp_governance_gateway_up 1\n"


def _register_execution_routes(
    app: FastAPI, service: GovernanceService, readiness: Readiness, principal: Any
) -> None:
    @app.post("/v1/queries/{query_id}:execute")
    def execute(query_id: str, request: QueryRequest, identity: Principal = Depends(principal)) -> dict[str, Any]:
        _require_ready(readiness)
        try:
            result = service.execute_registered(query_id, identity, dict(request.parameters))
        except ForbiddenColumns as exc:
            raise HTTPException(status_code=403, detail={"message": str(exc), "columns": exc.columns}) from exc
        except GovernanceError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return {
            "columns": result.columns,
            "rows": result.rows,
            "elapsed_ms": result.elapsed_ms,
            "affected_columns": result.affected_columns,
        }

    @app.get("/v1/queries/{query_id}:explain")
    def explain(query_id: str, identity: Principal = Depends(principal)) -> dict[str, Any]:
        _require_ready(readiness)
        try:
            governed = service.explain(query_id, identity)
        except ForbiddenColumns as exc:
            raise HTTPException(status_code=403, detail={"message": str(exc), "columns": exc.columns}) from exc
        except GovernanceError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return {"query_id": query_id, "decision": "ALLOW", "affected_columns": governed.affected_columns}

    @app.post("/v1/sql:execute")
    def execute_free_sql(request: SqlRequest, identity: Principal = Depends(principal)) -> dict[str, Any]:
        _require_ready(readiness)
        try:
            result = service.execute_free_sql(request.statement, identity, dict(request.parameters))
        except ForbiddenColumns as exc:
            raise HTTPException(status_code=403, detail={"message": str(exc), "columns": exc.columns}) from exc
        except GovernanceError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return {
            "columns": result.columns, "rows": result.rows, "elapsed_ms": result.elapsed_ms,
            "affected_columns": result.affected_columns, "mode": "FREE_SQL",
            "warning": "Direct access outside the gateway can bypass these controls.",
        }


def _register_discovery_routes(
    app: FastAPI, service: GovernanceService, readiness: Readiness, principal: Any
) -> None:
    @app.get("/v1/catalog")
    def visible_catalog(identity: Principal = Depends(principal)) -> dict[str, Any]:
        _require_ready(readiness)
        policies = service.store.policies()
        items: list[dict[str, Any]] = []
        for record in service.store.catalog_records():
            column = ColumnRef(
                str(record["catalog"]), str(record["schema"]), str(record["table"]), str(record["column"]),
            )
            action = effective_action(column, identity, policies)
            if action == PolicyAction.DENY or bool(record.get("deleted")):
                continue
            items.append({
                "catalog": column.catalog, "schema": column.schema, "table": column.table,
                "column": column.column, "data_type": str(record.get("data_type") or ""), "action": action.value,
            })
        return {"items": items}

    @app.get("/v1/queries")
    def available_queries(identity: Principal = Depends(principal)) -> dict[str, Any]:
        _require_ready(readiness)
        items: list[dict[str, Any]] = []
        for item in service.store.registered_queries():
            try:
                service.explain(str(item["query_id"]), identity)
            except GovernanceError:
                continue
            items.append(item)
        return {"items": items, "free_sql": service.store.can_use_free_sql(identity)}


def _register_catalog_admin_routes(
    app: FastAPI,
    service: GovernanceService,
    readiness: Readiness,
    principal: Any,
    catalog_sync: CatalogSynchronizer | None,
) -> None:
    @app.get("/v1/admin/status")
    def admin_status(identity: Principal = Depends(principal)) -> dict[str, Any]:
        _require_admin(identity)
        ready = readiness.ensure()
        return {
            "status": "ready" if ready else "initializing",
            "control_schema": os.getenv("GOVERNANCE_CONTROL_SCHEMA", "oci_control"),
            "catalog_sync": service.store.sync_status() if ready else {"status": "NOT_READY"},
        }

    @app.post("/v1/admin/catalog:sync")
    def synchronize_catalog(identity: Principal = Depends(principal)) -> dict[str, Any]:
        _require_admin(identity)
        if catalog_sync is None:
            raise HTTPException(status_code=503, detail="Master Catalog synchronization is not configured.")
        try:
            return catalog_sync.synchronize()
        except CatalogSyncError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/v1/admin/catalog")
    def admin_catalog(identity: Principal = Depends(principal)) -> dict[str, Any]:
        _require_admin_ready(identity, readiness)
        return {"items": service.store.catalog_records()}

    @app.put("/v1/admin/catalog/{object_id}/classification", status_code=204)
    def update_classification(
        object_id: str, request: ClassificationRequest, identity: Principal = Depends(principal)
    ) -> Response:
        _require_admin_ready(identity, readiness)
        try:
            service.store.update_classification(
                object_id, request.classification, request.sensitivity, request.owner,
                request.review_state, _principal_hash(identity),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return Response(status_code=204)


def _register_policy_routes(
    app: FastAPI,
    service: GovernanceService,
    readiness: Readiness,
    principal: Any,
    principal_directory: PrincipalDirectory | None,
) -> None:
    def save_policy(
        policy_id: str | None, request: AccessPolicyRequest, identity: Principal
    ) -> dict[str, Any]:
        _require_admin_ready(identity, readiness)
        _require_existing_principal(principal_directory, request.principal_type, request.principal_name)
        try:
            return service.store.save_access_policy(
                policy_id, request.object_id, request.principal_type, request.principal_name,
                request.action, request.priority, request.enabled, _principal_hash(identity),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/v1/admin/policies")
    def list_policies(
        object_id: str | None = None, identity: Principal = Depends(principal)
    ) -> dict[str, Any]:
        _require_admin_ready(identity, readiness)
        return {"items": service.store.list_access_policies(object_id)}

    @app.get("/v1/admin/principals")
    def list_principals(identity: Principal = Depends(principal)) -> dict[str, Any]:
        _require_admin_ready(identity, readiness)
        if principal_directory is None:
            raise HTTPException(status_code=503, detail="The AIDP principal directory is not configured.")
        try:
            return {"items": principal_directory.principals()}
        except CatalogSyncError as exc:
            raise HTTPException(status_code=503, detail="The AIDP principal directory is unavailable.") from exc

    @app.post("/v1/admin/policies", status_code=201)
    def create_policy(request: AccessPolicyRequest, identity: Principal = Depends(principal)) -> dict[str, Any]:
        return save_policy(None, request, identity)

    @app.put("/v1/admin/policies/{policy_id}")
    def update_policy(
        policy_id: str, request: AccessPolicyRequest, identity: Principal = Depends(principal)
    ) -> dict[str, Any]:
        return save_policy(policy_id, request, identity)

    @app.delete("/v1/admin/policies/{policy_id}", status_code=204)
    def delete_policy(policy_id: str, identity: Principal = Depends(principal)) -> Response:
        _require_admin_ready(identity, readiness)
        try:
            service.store.disable_access_policy(policy_id, _principal_hash(identity))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return Response(status_code=204)


def _register_sql_access_routes(
    app: FastAPI,
    service: GovernanceService,
    readiness: Readiness,
    principal: Any,
    principal_directory: PrincipalDirectory | None,
) -> None:
    def save_sql_access(
        grant_id: str | None, request: SqlAccessRequest, identity: Principal
    ) -> dict[str, Any]:
        _require_admin_ready(identity, readiness)
        _require_existing_principal(principal_directory, request.principal_type, request.principal_name)
        try:
            return service.store.save_sql_access(
                grant_id, request.principal_type, request.principal_name, request.enabled, _principal_hash(identity),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/v1/admin/sql-access")
    def list_sql_access(identity: Principal = Depends(principal)) -> dict[str, Any]:
        _require_admin_ready(identity, readiness)
        return {"items": service.store.list_sql_access()}

    @app.post("/v1/admin/sql-access", status_code=201)
    def create_sql_access(request: SqlAccessRequest, identity: Principal = Depends(principal)) -> dict[str, Any]:
        return save_sql_access(None, request, identity)

    @app.put("/v1/admin/sql-access/{grant_id}")
    def update_sql_access(
        grant_id: str, request: SqlAccessRequest, identity: Principal = Depends(principal)
    ) -> dict[str, Any]:
        return save_sql_access(grant_id, request, identity)

    @app.delete("/v1/admin/sql-access/{grant_id}", status_code=204)
    def delete_sql_access(grant_id: str, identity: Principal = Depends(principal)) -> Response:
        _require_admin_ready(identity, readiness)
        try:
            service.store.disable_sql_access(grant_id, _principal_hash(identity))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return Response(status_code=204)


def _register_token_routes(
    app: FastAPI, principal: Any, tokenizer: VaultTokenizer | None
) -> None:
    @app.post("/v1/tokens")
    def tokenize_value(request: TokenizeRequest, identity: Principal = Depends(principal)) -> dict[str, str]:
        _require_scope(identity, "governance.tokenize")
        if tokenizer is None:
            raise HTTPException(status_code=503, detail="Tokenization is not configured.")
        try:
            return {"token": tokenizer.tokenize(request.value, identity)}
        except TokenizationError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None

    @app.post("/v1/tokens/{token_id}:reveal")
    def detokenize_value(token_id: str, identity: Principal = Depends(principal)) -> dict[str, str]:
        _require_scope(identity, "governance.detokenize")
        if not re.fullmatch(r"aidptok_v1_[0-9a-f]{32}", token_id):
            raise HTTPException(status_code=404, detail="The token does not exist.")
        if tokenizer is None:
            raise HTTPException(status_code=503, detail="Tokenization is not configured.")
        try:
            return {"value": tokenizer.detokenize(token_id, identity)}
        except KeyError:
            raise HTTPException(status_code=404, detail="The token does not exist.") from None
        except TokenizationError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None


def create_app(
    service: GovernanceService,
    authenticator: Authenticator,
    initialize: Any | None = None,
    catalog_sync: CatalogSynchronizer | None = None,
    principal_directory: PrincipalDirectory | None = None,
    tokenizer: VaultTokenizer | None = None,
) -> FastAPI:
    app = FastAPI(
        title="AI Data Governance Gateway",
        version="2.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    readiness = Readiness(initialize)
    principal = _principal_dependency(authenticator)
    _register_base_routes(app, readiness)
    _register_execution_routes(app, service, readiness, principal)
    _register_discovery_routes(app, service, readiness, principal)
    _register_catalog_admin_routes(app, service, readiness, principal, catalog_sync)
    _register_policy_routes(app, service, readiness, principal, principal_directory)
    _register_sql_access_routes(app, service, readiness, principal, principal_directory)
    _register_token_routes(app, principal, tokenizer)
    return app


def production_app() -> FastAPI:
    issuer = os.environ.get("GOVERNANCE_OIDC_ISSUER", "")
    audience = os.environ.get("GOVERNANCE_OIDC_AUDIENCE", "")
    authority = os.environ.get("GOVERNANCE_OIDC_AUTHORITY", "")
    authenticator = OidcAuthenticator(OidcSettings(issuer, audience, authority))
    runtime = AidpJdbcRuntime()
    store = JdbcControlStore(runtime.connect, os.environ.get("GOVERNANCE_CONTROL_CATALOG", "aidp_lab"))
    tokenizer = VaultTokenizer(store, OciKmsCipher(KmsSettings.from_environment()))
    service = GovernanceService(store, runtime.execute, tokenizer.tokenize)
    catalog_client = AidpCatalogClient.from_environment()
    catalog_sync = CatalogSynchronizer(catalog_client, store)

    def initialize() -> None:
        store.initialize()
        catalog_sync.synchronize()

    return create_app(service, authenticator, initialize, catalog_sync, catalog_client, tokenizer)
