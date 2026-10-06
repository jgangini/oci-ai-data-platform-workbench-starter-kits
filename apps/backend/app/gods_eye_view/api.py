from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from .capture import query_lines
from .oci_provider import OciProvider, ProviderSelection, TextQuestion
from .oci_voice import OciVoice, VoiceSelection, VoiceTurn
from ..security import RateLimiter


Platform = Literal["x", "facebook", "instagram", "tiktok"]


class SourceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool | None = None
    mode: Literal["Synthetic", "simulation", "real"] | None = None
    query: str | None = Field(default=None, max_length=1000)
    interval_minutes: int | None = Field(default=None, ge=1, le=1440, strict=True)
    synthetic_batch_max: int | None = Field(default=None, ge=1, le=100, strict=True)
    secret_ref: str | None = Field(default=None, pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    bearer_token: SecretStr | None = Field(default=None, exclude=True)
    correlation_window_minutes: int | None = Field(default=None, ge=1, le=1440, strict=True)
    report_thresholds: dict[str, int] | None = None
    expected_revision: int | None = Field(default=None, ge=1, strict=True)

    @field_validator("mode")
    @classmethod
    def persisted_mode(cls, value):
        from .core import canonical_mode
        return canonical_mode(value)

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


class CaptureScheduleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_at: str = Field(min_length=1, max_length=64)
    interval_minutes: int = Field(ge=1, le=1440, strict=True)
    expected_revision: int = Field(ge=1, strict=True)


class SyntheticReset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: UUID
    confirm: bool = Field(strict=True)

    @field_validator("confirm")
    @classmethod
    def confirmed(cls, value):
        if not value:
            raise ValueError("Confirm deletion of Synthetic data")
        return value


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["pending", "validated", "rejected"]
    note: str = Field(default="", max_length=1000)
    expected_evidence_ids: list[Annotated[str, Field(min_length=1, max_length=200)]] | None = Field(default=None, max_length=10000)
    lat: float | None = Field(default=None, strict=True, allow_inf_nan=False, ge=-90, le=90)
    lon: float | None = Field(default=None, strict=True, allow_inf_nan=False, ge=-180, le=180)

    @model_validator(mode="after")
    def coordinates_together(self):
        if self.model_fields_set & {"lat", "lon"} and (self.lat is None or self.lon is None):
            raise ValueError("Latitude and longitude must be provided together as numbers")
        return self


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=2000)
    session_id: UUID
    version: str = Field(min_length=1, max_length=128)
    incident_id: str | None = Field(default=None, max_length=128)
    sensor_id: str | None = Field(default=None, max_length=100)
    filters: dict[str, str] = Field(default_factory=dict)


class SensorUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["Synthetic"] | None = None
    expected_revision: int = Field(ge=1, strict=True)
    interval_minutes: int | None = Field(default=None, ge=1, le=60, strict=True)
    sensor_count: int | None = Field(default=None, ge=100, le=5000, strict=True)
    families: list[Literal["river_level", "rainfall", "temperature", "soil_moisture", "wind_speed"]] | None = Field(default=None, min_length=1, max_length=5)


SensorType = Literal["river_level", "rainfall", "temperature", "soil_moisture", "wind_speed"]


class SensorFamilyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["Synthetic"] | None = None
    expected_revision: int = Field(ge=1, strict=True)
    interval_minutes: int | None = Field(default=None, ge=1, le=60, strict=True)
    sensor_count: int | None = Field(default=None, ge=1, le=5000, strict=True)


class SensorLocationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lat: float = Field(strict=True, allow_inf_nan=False, ge=-4.3, le=13.6)
    lon: float = Field(strict=True, allow_inf_nan=False, ge=-81.8, le=-66.7)
    expected_lat: float = Field(strict=True, allow_inf_nan=False, ge=-4.3, le=13.6)
    expected_lon: float = Field(strict=True, allow_inf_nan=False, ge=-81.8, le=-66.7)


def runtime_for(app):
    settings = app.state.settings
    if settings.gods_eye_view_local_mode:
        if not getattr(app.state, "gods_eye_view_runtime", None):
            from .local import LocalGodsEyeViewRuntime
            app.state.gods_eye_view_runtime = LocalGodsEyeViewRuntime(Path(settings.aidp_settings_file).parent)
        return app.state.gods_eye_view_runtime
    if not getattr(app.state, "gods_eye_view_cloud_runtime", None):
        from .cloud import CloudRuntime
        app.state.gods_eye_view_cloud_runtime = CloudRuntime(settings, app.state.gods_eye_view_aidp_factory)
    return app.state.gods_eye_view_cloud_runtime


async def run_local_gods_eye_view(app):
    """VM source producer only in OCI; classification/publication remain native AIDP work."""
    while True:
        try:
            runtime = runtime_for(app)
            if app.state.settings.portal_managed_modules and not app.state.settings.gods_eye_view_local_mode:
                from .installation import ModuleInstallation
                state = await asyncio.to_thread(ModuleInstallation(app.state.settings, runtime.aidp_factory, None).read)
                if not state.get("enabled"):
                    await asyncio.sleep(60)
                    continue
            await runtime.tick()
        except Exception as exc:
            logging.getLogger(__name__).error("Gods Eye View source producer failed (%s); retrying", type(exc).__name__)
        await asyncio.sleep(60)


def mount_gods_eye_view(app, require_admin, require_viewer=None):
    from .parameters import mount_parameters
    mount_parameters(app, require_admin)
    router = APIRouter(dependencies=[Depends(require_admin)])
    viewer = APIRouter(dependencies=[Depends(require_viewer or require_admin)])
    oci_limiter = RateLimiter(20, 60)

    async def provider_call(method, *args):
        if not getattr(app.state, "oci_text_provider", None):
            app.state.oci_text_provider = OciProvider(app.state.settings, runtime_for(app), app.state.gods_eye_view_aidp_factory)
        return await app.state.oci_text_provider.invoke(method, *args)

    def limit_oci(principal):
        # ponytail: per-process limits bound demo inference; use a shared limiter if the API is replicated.
        retry = oci_limiter.consume(principal)
        if retry:
            raise HTTPException(429, {"code": "oci_request_limit", "message": "Too many OCI assistant requests; try again shortly"},
                                headers={"Retry-After": str(retry)})

    @router.get("/api/admin/gods-eye-view/oci-provider")
    async def oci_admin_status():
        return {**await provider_call("status"), "can_configure": True}

    @router.put("/api/admin/gods-eye-view/oci-provider")
    async def oci_save(payload: ProviderSelection):
        return {**await provider_call("save", payload.model_id), "can_configure": True}

    @router.get("/api/admin/gods-eye-view/oci-provider/models")
    async def oci_models(cursor: str | None = Query(default=None, max_length=4096)):
        return await provider_call("models", cursor)

    @router.post("/api/admin/gods-eye-view/oci-provider/test")
    async def oci_test(principal: str = Depends(require_admin)):
        limit_oci(principal)
        return {**await provider_call("test"), "can_configure": True}

    @viewer.get("/api/gods-eye-view/oci-provider")
    async def oci_status(principal: str = Depends(require_viewer or require_admin)):
        return {**await provider_call("status"), "can_configure": principal == app.state.settings.admin_username}

    @viewer.post("/api/gods-eye-view/oci-chat")
    async def oci_chat(payload: TextQuestion, principal: str = Depends(require_viewer or require_admin)):
        limit_oci(principal)
        return await provider_call("chat", payload)

    def voice_provider():
        if not getattr(app.state, "oci_voice_provider", None):
            app.state.oci_voice_provider = OciVoice(app.state.settings, runtime_for(app), app.state.gods_eye_view_aidp_factory)
        return app.state.oci_voice_provider

    @router.get("/api/admin/gods-eye-view/oci-voice")
    async def voice_admin_status():
        return {**await voice_provider().invoke("status"), "can_configure": True}

    @viewer.get("/api/gods-eye-view/oci-voice")
    async def voice_status(principal: str = Depends(require_viewer or require_admin)):
        return {**await voice_provider().invoke("status"), "can_configure": principal == app.state.settings.admin_username}

    @router.put("/api/admin/gods-eye-view/oci-voice")
    async def voice_save(payload: VoiceSelection):
        return {**await voice_provider().invoke("save", payload.model_id, payload.voice), "can_configure": True}

    @router.get("/api/admin/gods-eye-view/oci-voice/models")
    async def voice_models(cursor: str | None = Query(default=None, max_length=4096)):
        return await voice_provider().invoke("models", cursor)

    @router.post("/api/admin/gods-eye-view/oci-voice/test")
    async def voice_test(request: Request, payload: VoiceTurn | None = None, principal: str = Depends(require_admin)):
        limit_oci(principal)
        return await voice_provider().turn(payload, request.is_disconnected, test=True)

    @viewer.post("/api/gods-eye-view/oci-voice/turn")
    async def voice_turn(request: Request, payload: VoiceTurn, principal: str = Depends(require_viewer or require_admin)):
        limit_oci(principal)
        result = await voice_provider().turn(payload, request.is_disconnected)
        # Older viewer VMs use the same authenticated route but retain their layer identifier.
        if request.url.path.startswith(("/api/prisma/", "/api/territorial/")):
            result = {**result, "actions": [
                {**action, "arguments": {**action["arguments"], "layerId": "territorial-events"}}
                if action.get("name") == "set_layer_visibility" and action.get("arguments", {}).get("layerId") == "gods-eye-view-events"
                else action for action in result.get("actions", [])
            ]}
        return result

    async def module_status(deploy=False):
        from .module import GodsEyeViewModule
        if not getattr(app.state, "gods_eye_view_module", None):
            app.state.gods_eye_view_module = GodsEyeViewModule(app.state.settings, runtime_for(app))
        return await app.state.gods_eye_view_module.status(deploy)

    @router.get("/api/admin/gods-eye-view/module")
    async def module():
        return await module_status()

    @router.post("/api/admin/gods-eye-view/module/deploy")
    async def deploy_module():
        return await module_status(True)

    async def invoke(method, *args):
        try:
            return await getattr(runtime_for(app), method)(*args)
        except KeyError as exc:
            raise HTTPException(404, "Gods Eye View record not found") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("/api/admin/gods-eye-view/sources")
    async def sources():
        return await invoke("sources")

    @router.put("/api/admin/gods-eye-view/social-schedule")
    async def social_schedule(payload: CaptureScheduleUpdate):
        return await invoke("save_capture_schedule", "social", payload.model_dump())

    @router.put("/api/admin/gods-eye-view/sensor-schedule")
    async def sensor_schedule(payload: CaptureScheduleUpdate):
        return await invoke("save_capture_schedule", "sensor", payload.model_dump())

    @viewer.get("/api/gods-eye-view/capture-status")
    async def capture_status(kind: Literal["social", "sensors"] = "social"):
        return await invoke("capture_status", kind)

    @router.get("/api/admin/gods-eye-view/sensors")
    async def sensors():
        return await invoke("sensors")

    @router.get("/api/admin/gods-eye-view/sensors/readings")
    async def sensor_readings(family: SensorType | None = None, q: str = Query(default="", max_length=200),
                             status: Literal["normal", "warning", "critical"] | None = None,
                             order: Literal["asc", "desc"] = "desc", page: int = Query(default=1, ge=1),
                             limit: int = Query(default=20, ge=1, le=100)):
        from .sensors import readings_page
        return readings_page(await invoke("snapshot"), family=family, query=q, status=status, order=order, page=page, limit=limit)

    @router.put("/api/admin/gods-eye-view/sensors")
    async def update_sensors(payload: SensorUpdate):
        return await invoke("update_sensors", payload.model_dump(exclude_none=True))

    @router.post("/api/admin/gods-eye-view/sensors/run")
    async def run_sensors():
        return await invoke("control_sensors", True)

    @router.post("/api/admin/gods-eye-view/sensors/pause")
    async def pause_sensors():
        return await invoke("control_sensors", False)

    @router.get("/api/admin/gods-eye-view/sensors/reset")
    async def sensors_reset_status():
        return await invoke("sensor_reset_status", "all")

    @router.post("/api/admin/gods-eye-view/sensors/reset")
    async def reset_all_sensors(payload: SyntheticReset):
        return await invoke("reset_sensors", "all", str(payload.operation_id))

    @router.put("/api/admin/gods-eye-view/sensors/{sensor_type}")
    async def update_sensor_family(sensor_type: SensorType, payload: SensorFamilyUpdate):
        return await invoke("update_sensors", payload.model_dump(exclude_none=True), sensor_type)

    @router.post("/api/admin/gods-eye-view/sensors/{sensor_type}/run")
    async def run_sensor_family(sensor_type: SensorType):
        return await invoke("control_sensors", True, sensor_type)

    @router.post("/api/admin/gods-eye-view/sensors/{sensor_type}/pause")
    async def pause_sensor_family(sensor_type: SensorType):
        return await invoke("control_sensors", False, sensor_type)

    @router.get("/api/admin/gods-eye-view/sensors/{sensor_type}/reset")
    async def sensor_reset_status(sensor_type: SensorType):
        return await invoke("sensor_reset_status", sensor_type)

    @router.post("/api/admin/gods-eye-view/sensors/{sensor_type}/reset")
    async def reset_sensor_family(sensor_type: SensorType, payload: SyntheticReset):
        return await invoke("reset_sensors", sensor_type, str(payload.operation_id))

    @router.put("/api/admin/gods-eye-view/sources/{platform}")
    async def update_source(platform: Platform, payload: SourceUpdate):
        values = payload.model_dump(exclude_none=True)
        if payload.bearer_token is not None:
            values["bearer_token"] = payload.bearer_token.get_secret_value()
        return await invoke("update_source", platform, values)

    @router.post("/api/admin/gods-eye-view/sources/{platform}/test")
    async def test_source(platform: Platform):
        return await invoke("test_source", platform)

    @router.post("/api/admin/gods-eye-view/sources/{platform}/run")
    async def run_source(platform: Platform):
        return await invoke("run_source", platform)

    @router.post("/api/admin/gods-eye-view/sources/{platform}/pause")
    async def pause_source(platform: Platform):
        return await invoke("pause_source", platform)

    @router.get("/api/admin/gods-eye-view/posts")
    async def posts(platform: Platform | None = None, limit: int = Query(default=20, ge=1, le=100),
                    cursor: str | None = Query(default=None, max_length=2048),
                    sort: Literal["captured_at", "published_at"] = "captured_at", order: Literal["asc", "desc"] = "desc",
                    q: str = Query(default="", max_length=200)):
        from .posts import cursor_values, page_result, search_page
        before, maximum = cursor_values(cursor, platform, app.state.session_key, q=q, sort=sort, order=order)
        page = await search_page(lambda size, before, maximum: invoke("posts", platform, size, before, maximum),
                                 limit, before, maximum, q=q, sort=sort, order=order,
                                 read_ordered=lambda size, before, maximum, sort, order: invoke("ordered_posts", platform, size, before, maximum, sort, order))
        return page_result(page, platform, app.state.session_key, q=q, sort=sort, order=order)

    @router.get("/api/admin/gods-eye-view/media/{fixture_id}/{filename}")
    @viewer.get("/api/gods-eye-view/media/{fixture_id}/{filename}")
    async def fixture_media(fixture_id: str, filename: str, dataset_version: str = "bogota-v1"):
        from .corpus import media_file
        try:
            path, metadata = media_file(fixture_id, filename, dataset_version)
        except (ValueError, KeyError, FileNotFoundError):
            raise HTTPException(404, "Media not found") from None
        return FileResponse(path, media_type=metadata["mime_type"], headers={
            "Content-Security-Policy": "default-src 'none'; sandbox", "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store"})

    @router.post("/api/admin/gods-eye-view/simulation")
    async def simulation(payload: SimulationAction):
        return await invoke("simulation", payload.action)

    @router.get("/api/admin/gods-eye-view/synthetic/reset")
    async def synthetic_reset_status():
        return await invoke("synthetic_reset_status")

    @router.post("/api/admin/gods-eye-view/synthetic/reset")
    async def reset_synthetic(payload: SyntheticReset):
        return await invoke("reset_synthetic", str(payload.operation_id))

    @viewer.get("/api/gods-eye-view/snapshot")
    async def snapshot():
        return await invoke("snapshot")

    @router.post("/api/gods-eye-view/incidents/{incident_id}/review")
    async def review(incident_id: str, payload: ReviewRequest):
        return await invoke("review", incident_id, payload.status, payload.note, payload.expected_evidence_ids, payload.lat, payload.lon)

    @router.post("/api/gods-eye-view/sensors/{sensor_id}/location")
    async def sensor_location(sensor_id: str, payload: SensorLocationUpdate):
        import re
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", sensor_id):
            raise HTTPException(422, "Invalid sensor identifier")
        return await invoke("sensor_location", sensor_id, payload.model_dump())

    @router.post("/api/admin/gods-eye-view/chat")
    async def chat(payload: ChatRequest, request: Request):
        if app.state.settings.gods_eye_view_local_mode:
            raise HTTPException(503, "Local fixture chat is available only in God's Eye View")
        return await runtime_for(app).chat(payload.model_dump(mode="json"), request.headers.get("cookie", ""), app.state.session_key)

    for group in (router, viewer):
        # Rebuild aliases through FastAPI so validation and authorization stay identical.
        for route in tuple(group.routes):
            for legacy in ("territorial", "prisma"):
                app.add_api_route(route.path.replace("/gods-eye-view/", f"/{legacy}/", 1), route.endpoint,
                                  methods=route.methods, dependencies=route.dependencies,
                                  response_model=route.response_model, response_class=route.response_class,
                                  status_code=route.status_code, include_in_schema=False)
        app.include_router(group)
