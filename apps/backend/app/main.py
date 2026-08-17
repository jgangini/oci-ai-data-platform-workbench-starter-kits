from __future__ import annotations

import asyncio
import logging
import re
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .aidp import (
    AidpClient,
    AidpProvisionConflict,
    AidpProvisionError,
    AidpProvisionPending,
    LocalAidpClient,
    UserMaterial,
)
from .config import Settings, SettingsStore
from .identity import IdentityClient, IdentityConflict, IdentityPending, IdentityRejected, LocalIdentityClient
from .lab_packs import available_lab_ids, public_lab_catalog
from .security import (
    RateLimiter,
    issue_session,
    load_or_create_session_key,
    opaque_rate_limit_key,
    verify_secret,
    verify_session,
)


COOKIE_NAME = "__Host-aidp_lab_admin"
LOCAL_COOKIE_NAME = "aidp_lab_admin"
CODE_PATTERN = re.compile(r"^[A-Z]{4}-[0-9]{4}$")
EMAIL_PATTERN = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$")
logger = logging.getLogger(__name__)
HEALTH_SUCCESS_TTL_SECONDS = 30
HEALTH_FAILURE_TTL_SECONDS = 5


class UserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=2, max_length=120)
    email: str = Field(min_length=5, max_length=254)
    lab_ids: list[str] = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = " ".join(value.split())
        if len(value) < 2:
            raise ValueError("Name is required")
        return value

    @field_validator("email")
    @classmethod
    def clean_email(cls, value: str) -> str:
        value = value.strip().lower()
        if not EMAIL_PATTERN.fullmatch(value):
            raise ValueError("Enter a valid email address")
        return value

    @field_validator("lab_ids")
    @classmethod
    def validate_lab_ids(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("Labs cannot be duplicated")
        supported = set(available_lab_ids())
        if any(value not in supported for value in values):
            raise ValueError("Choose only available labs")
        return values


class RegistrationRequest(UserRequest):
    code: str = Field(min_length=9, max_length=9)

    @field_validator("code")
    @classmethod
    def clean_code(cls, value: str) -> str:
        value = value.strip().upper()
        if not CODE_PATTERN.fullmatch(value):
            raise ValueError("Code must match AAAA-0000")
        return value


class AdminUserRequest(UserRequest):
    pass


class AdminLabRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lab_id: str

    @field_validator("lab_id")
    @classmethod
    def validate_lab_id(cls, value: str) -> str:
        if value not in available_lab_ids():
            raise ValueError("Choose an available lab")
        return value


class LabOperationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: UUID


class SettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    aidp_url: str | None = Field(default=None, min_length=1, max_length=2_048)
    registration_code: str | None = Field(default=None, min_length=1, max_length=9)

    @field_validator("registration_code")
    @classmethod
    def clean_registration_code(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().upper()
        if not CODE_PATTERN.fullmatch(value):
            raise ValueError("Code must match AAAA-0000")
        return value


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


def _material_payload(
    materials: UserMaterial | tuple[UserMaterial, ...],
    email: str,
    aidp_url: str,
) -> dict[str, Any]:
    values = (materials,) if isinstance(materials, UserMaterial) else materials
    content: dict[str, Any] = {
        "status": "active",
        "email": email,
        "participant_key": values[0].participant_key,
        "participant_code": values[0].participant_code,
        "labs": [
            {
                "lab_id": material.lab_id,
                "pack_version": material.pack_version,
                "phase": material.phase,
                "workspace_path": material.workspace_path,
                "job_name": material.job_name,
            }
            for material in values
        ],
        "aidp_url": aidp_url,
    }
    if not aidp_url:
        content["message"] = "Your account is ready. Ask the lab administrator to configure the Workbench URL."
    return content


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    cookie_name = COOKIE_NAME if settings.cookie_secure else LOCAL_COOKIE_NAME

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        for client in (app.state.identity_client, app.state.aidp_client):
            if client is not None:
                await client.close()

    app = FastAPI(title="OCI AIDP Lab", version="2.0.1", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.settings = settings
    app.state.settings_store = SettingsStore(settings)
    app.state.session_key = load_or_create_session_key(settings.session_secret_file)
    app.state.register_limiter = RateLimiter(5, 60)
    app.state.invalid_code_limiter = RateLimiter(5, 60)
    app.state.login_limiter = RateLimiter(5, 60)
    app.state.identity_client = None
    app.state.aidp_client = None
    app.state.health_lock = asyncio.Lock()
    app.state.health_expires_at = 0.0
    app.state.health_error = False
    app.state.health_failures = ""

    def default_factory() -> IdentityClient | LocalIdentityClient:
        if app.state.identity_client is None:
            app.state.identity_client = LocalIdentityClient(settings) if settings.local_development_mode else IdentityClient(settings)
        return app.state.identity_client

    app.state.identity_factory = default_factory

    def default_aidp_factory() -> AidpClient | LocalAidpClient:
        if app.state.aidp_client is None:
            app.state.aidp_client = LocalAidpClient(settings) if settings.local_development_mode else AidpClient(settings)
        return app.state.aidp_client

    app.state.aidp_factory = default_aidp_factory

    async def reset_health_client(component: str) -> None:
        attribute = f"{component}_client"
        client = getattr(app.state, attribute, None)
        if client is None:
            return
        try:
            await client.close()
        except Exception:
            logger.warning("Failed to close unhealthy %s client", component)
        finally:
            setattr(app.state, attribute, None)

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Callable):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store"
        return response

    def client_ip(request: Request) -> str:
        forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
        return forwarded or (request.client.host if request.client else "unknown")

    def require_identity() -> None:
        if not settings.identity_ready() or not app.state.settings_store.get_registration_code_hash():
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Registration is not configured")

    def require_registration_ready() -> None:
        require_identity()
        if not settings.aidp_ready():
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "AIDP workspace provisioning is not configured")

    def require_admin(request: Request) -> str:
        username = verify_session(request.cookies.get(cookie_name, ""), app.state.session_key)
        if username != settings.admin_username:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Administrator session required")
        return username

    async def provision_user(name: str, email: str, lab_ids: list[str]) -> JSONResponse:
        try:
            identity = app.state.identity_factory()
            result = await identity.prepare_registration(name, email)
        except IdentityConflict as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        except IdentityRejected as exc:
            logger.warning("Identity Domains rejected a lab registration: %s", exc)
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Identity Domains rejected this registration request",
            ) from exc
        except IdentityPending as exc:
            return JSONResponse(
                status_code=202,
                content={"status": "pending", "phase": "identity", "message": str(exc)},
            )
        aidp = app.state.aidp_factory()

        async def restore_existing_access() -> None:
            if not result.was_developer:
                return
            try:
                await identity.activate_registration(result.user_id)
            except IdentityPending as exc:
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "Prior developer access is still being restored",
                ) from exc

        if result.status != "created":
            try:
                current = await aidp.list_user_labs([result.user_ocid])
            except (AidpProvisionPending, AidpProvisionError) as exc:
                await restore_existing_access()
                raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
            assigned = {material.lab_id for material in current.get(result.user_ocid, [])}
            requested = set(lab_ids)
            if (assigned or result.was_developer) and assigned != requested:
                await restore_existing_access()
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "Existing lab assignments can only be changed by an administrator",
                )
        participant_code = app.state.settings_store.participant_code(result.email)
        try:
            material = await aidp.provision_user(
                result.user_ocid, email, lab_ids, participant_code
            )
        except AidpProvisionPending as exc:
            await restore_existing_access()
            return JSONResponse(
                status_code=202,
                content={"status": "pending", "phase": exc.phase, "message": str(exc)},
            )
        except AidpProvisionConflict as exc:
            await restore_existing_access()
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        except AidpProvisionError as exc:
            await restore_existing_access()
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
        try:
            await identity.activate_registration(result.user_id)
        except IdentityPending as exc:
            return JSONResponse(
                status_code=202,
                content={"status": "pending", "phase": "permissions", "message": str(exc)},
            )
        content = _material_payload(
            material,
            result.email,
            app.state.settings_store.get_workbench_url(),
        )
        return JSONResponse(status_code=201 if result.status == "created" else 200, content=content)

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        if not settings.identity_ready() or not settings.aidp_ready():
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Lab services are unavailable")
        async with app.state.health_lock:
            now = time.monotonic()
            if now >= app.state.health_expires_at:
                checks = (
                    ("identity", app.state.identity_factory().healthcheck()),
                    ("aidp", app.state.aidp_factory().healthcheck()),
                )
                results = await asyncio.gather(
                    *(check for _, check in checks), return_exceptions=True
                )
                failures = [
                    (component, result)
                    for (component, _), result in zip(checks, results, strict=True)
                    if isinstance(result, BaseException)
                ]
                app.state.health_error = bool(failures)
                app.state.health_failures = ",".join(
                    f"{component}:{type(failure).__name__}"
                    for component, failure in failures
                )
                app.state.health_expires_at = now + (
                    HEALTH_FAILURE_TTL_SECONDS if failures else HEALTH_SUCCESS_TTL_SECONDS
                )
                for component, failure in failures:
                    logger.warning(
                        "Lab health probe failed for %s (%s)",
                        component,
                        type(failure).__name__,
                    )
                    await reset_health_client(component)
            if app.state.health_error:
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "Lab control services are unavailable",
                    headers={"X-Lab-Health-Failures": app.state.health_failures},
                )
        return {"status": "ok"}

    @app.get("/api/config")
    @app.get("/api/public/config")
    async def public_config() -> dict[str, Any]:
        return {
            "lab_name": "OCI AI Data Platform Lab",
            "registration_code_pattern": "AAAA-0000",
            "labs": public_lab_catalog(),
        }

    @app.post("/api/register")
    async def register(payload: RegistrationRequest, request: Request) -> JSONResponse:
        require_registration_ready()
        source_ip = client_ip(request)
        invalid_retry_after = app.state.invalid_code_limiter.retry_after(source_ip)
        if invalid_retry_after:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "Too many invalid registration codes",
                headers={"Retry-After": str(invalid_retry_after)},
            )
        if not verify_secret(payload.code, app.state.settings_store.get_registration_code_hash()):
            invalid_retry_after = app.state.invalid_code_limiter.consume(source_ip)
            if invalid_retry_after:
                raise HTTPException(
                    status.HTTP_429_TOO_MANY_REQUESTS,
                    "Too many invalid registration codes",
                    headers={"Retry-After": str(invalid_retry_after)},
                )
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid registration code")
        participant_key = opaque_rate_limit_key(app.state.session_key, payload.email)
        retry_after = app.state.register_limiter.consume(participant_key)
        if retry_after:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "Registration reconciliation is temporarily rate limited",
                headers={"Retry-After": str(retry_after)},
            )
        return await provision_user(payload.name, payload.email, payload.lab_ids)

    @app.post("/api/admin/login", status_code=204)
    async def admin_login(payload: LoginRequest, request: Request) -> Response:
        if not app.state.login_limiter.allow(client_ip(request)):
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many login attempts")
        valid_user = payload.username == settings.admin_username
        valid_password = verify_secret(payload.password, settings.admin_password_hash)
        if not (valid_user and valid_password):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid administrator credentials")
        response = Response(status_code=204)
        response.set_cookie(
            cookie_name,
            issue_session(app.state.session_key, settings.admin_username),
            max_age=28_800,
            secure=settings.cookie_secure,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return response

    @app.post("/api/admin/logout", status_code=204)
    async def admin_logout() -> Response:
        response = Response(status_code=204)
        response.delete_cookie(cookie_name, path="/", secure=settings.cookie_secure, httponly=True, samesite="strict")
        return response

    @app.get("/api/admin/session")
    async def admin_session(username: str = Depends(require_admin)) -> dict[str, str]:
        return {"username": username}

    @app.get("/api/admin/settings")
    async def admin_settings(_admin: str = Depends(require_admin)) -> dict[str, str | bool]:
        return app.state.settings_store.get_admin_settings()

    @app.put("/api/admin/settings")
    async def update_admin_settings(payload: SettingsRequest, _admin: str = Depends(require_admin)) -> dict[str, str | bool]:
        try:
            return app.state.settings_store.update(payload.aidp_url, payload.registration_code)
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    @app.get("/api/admin/users")
    async def admin_users(_admin: str = Depends(require_admin)) -> dict[str, list[dict]]:
        require_identity()
        client = app.state.identity_factory()
        users = [dict(user) for user in await client.list_lab_users()]
        user_ocids = [
            str(user["ocid"])
            for user in users
            if user.get("managed") and str(user.get("ocid") or "").startswith("ocid1.user.")
        ]
        assigned_labs: dict[str, list[UserMaterial]] = {}
        if settings.aidp_ready() and user_ocids:
            try:
                assigned_labs = await app.state.aidp_factory().list_user_labs(user_ocids)
            except (AidpProvisionPending, AidpProvisionError) as exc:
                logger.warning("AIDP participant lab inventory is unavailable (%s)", type(exc).__name__)
        for user in users:
            materials = assigned_labs.get(str(user.get("ocid") or ""), [])
            user["participant_code"] = materials[0].participant_code if materials else None
            user["labs"] = [
                {
                    "lab_id": material.lab_id,
                    "pack_version": material.pack_version,
                    "phase": material.phase,
                    "workspace_path": material.workspace_path,
                    "job_name": material.job_name,
                }
                for material in materials
            ]
            user.pop("ocid", None)
        return {"users": users}

    @app.post("/api/admin/users")
    async def admin_create_user(payload: AdminUserRequest, _admin: str = Depends(require_admin)) -> JSONResponse:
        require_identity()
        if not settings.aidp_ready():
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "AIDP workspace provisioning is not configured")
        return await provision_user(payload.name, payload.email, payload.lab_ids)

    @app.post("/api/admin/users/{user_id}/labs")
    async def admin_add_lab(
        user_id: str,
        payload: AdminLabRequest,
        _admin: str = Depends(require_admin),
    ) -> JSONResponse:
        require_identity()
        if not settings.aidp_ready():
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "AIDP workspace provisioning is not configured")
        identity = app.state.identity_factory()
        try:
            user = await identity.get_lab_user(user_id)
            if user is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Lab user not found")
            material = await app.state.aidp_factory().add_lab(
                user["ocid"], user["email"], payload.lab_id
            )
        except IdentityConflict as exc:
            raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
        except IdentityPending as exc:
            return JSONResponse(
                status_code=202,
                content={
                    "status": "pending",
                    "phase": "permissions",
                    "message": str(exc),
                },
            )
        except AidpProvisionPending as exc:
            return JSONResponse(
                status_code=202,
                content={
                    "status": "pending",
                    "phase": exc.phase,
                    "message": str(exc),
                },
            )
        except AidpProvisionConflict as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        except AidpProvisionError as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
        content = _material_payload(
            material,
            user["email"],
            app.state.settings_store.get_workbench_url(),
        )
        content["message"] = "The lab was added successfully."
        return JSONResponse(content=content)

    @app.post("/api/admin/users/{user_id}/labs/{lab_id}/redeploy")
    async def admin_redeploy_lab(
        user_id: str,
        lab_id: str,
        payload: LabOperationRequest,
        _admin: str = Depends(require_admin),
    ) -> JSONResponse:
        require_registration_ready()
        identity = app.state.identity_factory()
        try:
            user = await identity.get_lab_user(user_id)
            if user is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Lab user not found")
            material = await app.state.aidp_factory().redeploy_lab(
                user["ocid"], user["email"], lab_id, str(payload.operation_id)
            )
        except IdentityConflict as exc:
            raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
        except AidpProvisionPending as exc:
            return JSONResponse(status_code=202, content={
                "status": "pending", "phase": exc.phase,
                "operation_id": str(payload.operation_id), "message": str(exc),
            })
        except AidpProvisionConflict as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        except (AidpProvisionError, ValueError) as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
        content = _material_payload(
            material, user["email"], app.state.settings_store.get_workbench_url()
        )
        content.update(operation_id=str(payload.operation_id), message="The lab was redeployed successfully.")
        return JSONResponse(content=content)

    @app.delete("/api/admin/users/{user_id}/labs/{lab_id}")
    async def admin_delete_lab(
        user_id: str,
        lab_id: str,
        operation_id: UUID,
        _admin: str = Depends(require_admin),
    ) -> JSONResponse:
        require_registration_ready()
        identity = app.state.identity_factory()
        try:
            user = await identity.get_lab_user(user_id)
            if user is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Lab user not found")
            await app.state.aidp_factory().delete_lab(
                user["ocid"], lab_id, str(operation_id)
            )
        except IdentityConflict as exc:
            raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
        except AidpProvisionPending as exc:
            return JSONResponse(status_code=202, content={
                "status": "pending", "phase": exc.phase,
                "operation_id": str(operation_id), "message": str(exc),
            })
        except AidpProvisionConflict as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        except AidpProvisionError as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
        return JSONResponse(content={
            "status": "active", "operation_id": str(operation_id),
            "message": "The lab was removed successfully.",
        })

    @app.delete("/api/admin/users/{user_id}", status_code=204)
    async def admin_delete_user(user_id: str, _admin: str = Depends(require_admin)) -> Response:
        require_identity()
        if not settings.aidp_ready():
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "AIDP workspace provisioning is not configured")
        client = app.state.identity_factory()
        try:
            user = await client.get_lab_user(user_id)
            if user is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Lab user not found")
            await app.state.aidp_factory().cleanup_user(user["ocid"])
            deleted = await client.delete_lab_user(user_id)
        except IdentityConflict as exc:
            raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
        except AidpProvisionPending as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, f"AIDP cleanup is still in progress. Retry deletion shortly. {exc}") from exc
        except AidpProvisionError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, f"AIDP cleanup failed before Identity Domains deletion. {exc}") from exc
        if not deleted:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Lab user not found")
        return Response(status_code=204)

    return app


app = create_app()
