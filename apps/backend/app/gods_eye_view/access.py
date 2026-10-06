"""Local participant access uses the normal signed session and explicit project grants."""
from fastapi import Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from ..security import issue_session, verify_session


class ParticipantLogin(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=256)


class ProjectGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


def mount_access(app, require_admin, cookie_name):
    settings = app.state.settings

    async def require_viewer(request: Request):
        subject = verify_session(request.cookies.get(cookie_name, ""), app.state.session_key)
        if subject == settings.admin_username:
            return subject
        if settings.local_development_mode and subject and subject.startswith(("local-gods-eye-view:", "local-territorial:", "local-prisma:")):
            user = await app.state.identity_factory().gods_eye_view_user(subject.split(":", 1)[1])
            if user:
                return subject
        raise HTTPException(401, "Gods Eye View access required")

    @app.get("/api/prisma/session", include_in_schema=False)
    @app.get("/api/territorial/session", include_in_schema=False)
    @app.get("/api/gods-eye-view/session")
    async def session(response: Response, subject=Depends(require_viewer)):
        # Session subjects stay wire-compatible with older viewers during either upgrade order.
        wire_subject = "local-prisma:" + subject.split(":", 1)[1] if subject.startswith(("local-gods-eye-view:", "local-territorial:")) else subject
        response.headers["X-Gods-Eye-View-User"] = subject
        response.headers["X-Territorial-User"] = wire_subject
        response.headers["X-PRISMA-User"] = wire_subject
        return {"username": subject}

    if settings.local_development_mode:
        @app.post("/api/local/prisma/login", status_code=204, include_in_schema=False)
        @app.post("/api/local/territorial/login", status_code=204, include_in_schema=False)
        @app.post("/api/local/gods-eye-view/login", status_code=204)
        async def login(payload: ParticipantLogin, request: Request):
            address = request.headers.get("x-forwarded-for", "").split(",", 1)[0] or (request.client.host if request.client else "local")
            if not app.state.login_limiter.allow(address):
                raise HTTPException(429, "Too many login attempts")
            user_id = await app.state.identity_factory().authenticate(payload.username, payload.password)
            if not user_id or not await app.state.identity_factory().gods_eye_view_user(user_id):
                raise HTTPException(401, "Invalid credentials or project access is not enabled")
            response = Response(status_code=204)
            # Signed session claims are a persisted contract, including backend rollback.
            response.set_cookie(cookie_name, issue_session(app.state.session_key, f"local-prisma:{user_id}"),
                                max_age=28800, secure=settings.cookie_secure, httponly=True, samesite="strict", path="/")
            return response

        @app.get("/api/local/prisma/workspace", include_in_schema=False)
        @app.get("/api/local/territorial/workspace", include_in_schema=False)
        @app.get("/api/local/gods-eye-view/workspace")
        async def workspace(subject=Depends(require_viewer)):
            user = await app.state.identity_factory().gods_eye_view_user(subject.split(":", 1)[1] if ":" in subject else subject)
            if not user:
                raise HTTPException(404, "Sign in with a local participant account")
            return {"mode": "SIMULATED", "project": "God's Eye View", "user": user,
                    "project_access": {"workspace_path": "/Workspace/medallion/gods_eye_view", "role": "reader", "simulated": True},
                    "viewer_url": "/gods-eye-view/", "message": "Local access simulation. No OCI identity or email was created."}

        @app.put("/api/admin/prisma/users/{user_id}", include_in_schema=False)
        @app.put("/api/admin/territorial/users/{user_id}", include_in_schema=False)
        @app.put("/api/admin/gods-eye-view/users/{user_id}")
        async def grant(user_id: str, payload: ProjectGrant, _admin=Depends(require_admin)):
            identity = app.state.identity_factory()
            if not await identity.get_lab_user(user_id):
                raise HTTPException(404, "Participant not found")
            await identity.grant_gods_eye_view(user_id, payload.enabled)
            return {"enabled": payload.enabled, "mode": "SIMULATED"}

    return require_viewer
