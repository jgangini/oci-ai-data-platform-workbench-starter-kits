from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from .capture import query_lines
from .oci_provider import OciProvider, ProviderSelection, TextQuestion
from ..security import RateLimiter


Platform = Literal["x", "facebook", "instagram", "tiktok"]


class SourceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool | None = None
    mode: Literal["simulation", "real"] | None = None
    query: str | None = Field(default=None, max_length=5129)
    interval_minutes: int | None = Field(default=None, ge=1, le=1440, strict=True)
    secret_ref: str | None = Field(default=None, pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    bearer_token: SecretStr | None = Field(default=None, exclude=True)
    correlation_window_minutes: int | None = Field(default=None, ge=1, le=1440, strict=True)
    report_thresholds: dict[str, int] | None = None
    expected_revision: int | None = Field(default=None, ge=1, strict=True)

    @field_validator("report_thresholds", mode="before")
    @classmethod
    def thresholds(cls, value):
        if value is not None:
            from .source_rules import validate_rules
            validate_rules({"report_thresholds": value})
        return value

    @field_validator("query")
    @classmethod
    def independent_queries(cls, value):
        return "\n".join(query_lines(value)) if value is not None else None


class SimulationAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["start", "pause", "resume", "reset", "replay"]


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["pending", "validated", "rejected"]
    note: str = Field(default="", max_length=2000)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=2000)
    session_id: UUID
    version: str = Field(min_length=1, max_length=128)
    incident_id: str | None = Field(default=None, max_length=128)
    filters: dict[str, str] = Field(default_factory=dict)


def runtime_for(app):
    settings = app.state.settings
    if settings.local_development_mode:
        if not getattr(app.state, "prisma_runtime", None):
            from .local import LocalPrismaRuntime
            app.state.prisma_runtime = LocalPrismaRuntime(Path(settings.aidp_settings_file).parent)
        return app.state.prisma_runtime
    if not getattr(app.state, "prisma_cloud_runtime", None):
        from .cloud import CloudRuntime
        app.state.prisma_cloud_runtime = CloudRuntime(settings, app.state.aidp_factory)
    return app.state.prisma_cloud_runtime


async def run_local_prisma(app):
    """VM source producer only in OCI; classification/publication remain native AIDP work."""
    while True:
        try:
            await runtime_for(app).tick()
        except Exception as exc:
            logging.getLogger(__name__).error("PRISMA source producer failed (%s); retrying", type(exc).__name__)
        await asyncio.sleep(60)


def mount_prisma(app, require_admin, require_viewer=None):
    router = APIRouter(dependencies=[Depends(require_admin)])
    viewer = APIRouter(dependencies=[Depends(require_viewer or require_admin)])
    oci_limiter = RateLimiter(20, 60)

    async def provider_call(method, *args):
        if not getattr(app.state, "oci_text_provider", None):
            app.state.oci_text_provider = OciProvider(app.state.settings, runtime_for(app), app.state.aidp_factory)
        return await app.state.oci_text_provider.invoke(method, *args)

    def limit_oci(principal):
        # ponytail: per-process limits bound demo inference; use a shared limiter if the API is replicated.
        retry = oci_limiter.consume(principal)
        if retry:
            raise HTTPException(429, {"code": "oci_request_limit", "message": "Too many OCI assistant requests; try again shortly"},
                                headers={"Retry-After": str(retry)})

    @router.get("/api/admin/prisma/oci-provider")
    async def oci_admin_status():
        return {**await provider_call("status"), "can_configure": True}

    @router.put("/api/admin/prisma/oci-provider")
    async def oci_save(payload: ProviderSelection):
        return {**await provider_call("save", payload.model_id), "can_configure": True}

    @router.get("/api/admin/prisma/oci-provider/models")
    async def oci_models(cursor: str | None = Query(default=None, max_length=4096)):
        return await provider_call("models", cursor)

    @router.post("/api/admin/prisma/oci-provider/test")
    async def oci_test(principal: str = Depends(require_admin)):
        limit_oci(principal)
        return {**await provider_call("test"), "can_configure": True}

    @viewer.get("/api/prisma/oci-provider")
    async def oci_status(principal: str = Depends(require_viewer or require_admin)):
        return {**await provider_call("status"), "can_configure": principal == app.state.settings.admin_username}

    @viewer.post("/api/prisma/oci-chat")
    async def oci_chat(payload: TextQuestion, principal: str = Depends(require_viewer or require_admin)):
        limit_oci(principal)
        return await provider_call("chat", payload)

    async def module_status(deploy=False):
        from .module import TerritorialModule
        if not getattr(app.state, "territorial_module", None):
            app.state.territorial_module = TerritorialModule(app.state.settings, runtime_for(app))
        return await app.state.territorial_module.status(deploy)

    @router.get("/api/admin/prisma/module")
    async def module():
        return await module_status()

    @router.post("/api/admin/prisma/module/deploy")
    async def deploy_module():
        return await module_status(True)

    async def invoke(method, *args):
        try:
            return await getattr(runtime_for(app), method)(*args)
        except KeyError as exc:
            raise HTTPException(404, "Territorial Control record not found") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("/api/admin/prisma/sources")
    async def sources():
        return await invoke("sources")

    @router.put("/api/admin/prisma/sources/{platform}")
    async def update_source(platform: Platform, payload: SourceUpdate):
        values = payload.model_dump(exclude_none=True)
        if payload.bearer_token is not None:
            values["bearer_token"] = payload.bearer_token.get_secret_value()
        return await invoke("update_source", platform, values)

    @router.post("/api/admin/prisma/sources/{platform}/test")
    async def test_source(platform: Platform):
        return await invoke("test_source", platform)

    @router.post("/api/admin/prisma/sources/{platform}/run")
    async def run_source(platform: Platform):
        return await invoke("run_source", platform)

    @router.post("/api/admin/prisma/sources/{platform}/pause")
    async def pause_source(platform: Platform):
        return await invoke("pause_source", platform)

    @router.get("/api/admin/prisma/posts")
    async def posts(platform: Platform, limit: int = Query(default=20, ge=1, le=100),
                    cursor: str | None = Query(default=None, max_length=1024)):
        from .posts import cursor_values, page_result
        before, maximum = cursor_values(cursor, platform, app.state.session_key)
        page = await invoke("posts", platform, limit, before, maximum)
        return page_result(page, platform, app.state.session_key)

    @router.get("/api/admin/prisma/media/{fixture_id}/{filename}")
    async def fixture_media(fixture_id: str, filename: str):
        from .corpus import media_file
        try:
            path, metadata = media_file(fixture_id, filename)
        except (ValueError, KeyError, FileNotFoundError):
            raise HTTPException(404, "Media not found") from None
        return FileResponse(path, media_type=metadata["mime_type"], headers={
            "Content-Security-Policy": "default-src 'none'; sandbox", "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=300"})

    @router.post("/api/admin/prisma/simulation")
    async def simulation(payload: SimulationAction):
        return await invoke("simulation", payload.action)

    @viewer.get("/api/prisma/snapshot")
    async def snapshot():
        return await invoke("snapshot")

    @router.post("/api/prisma/incidents/{incident_id}/review")
    async def review(incident_id: str, payload: ReviewRequest):
        return await invoke("review", incident_id, payload.status, payload.note)

    @router.post("/api/admin/prisma/chat")
    async def chat(payload: ChatRequest, request: Request):
        if app.state.settings.local_development_mode:
            raise HTTPException(503, "Local fixture chat is available only in God's Eye View")
        return await runtime_for(app).chat(payload.model_dump(mode="json"), request.headers.get("cookie", ""), app.state.session_key)

    app.include_router(router)
    app.include_router(viewer)
