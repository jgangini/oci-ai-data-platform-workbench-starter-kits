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
        if settings.local_development_mode and subject and subject.startswith("local-prisma:"):
            user = await app.state.identity_factory().prisma_user(subject.removeprefix("local-prisma:"))
            if user:
                return subject
        raise HTTPException(401, "Territorial Control access required")

    @app.get("/api/prisma/session")
    async def session(response: Response, subject=Depends(require_viewer)):
        response.headers["X-PRISMA-User"] = subject
        return {"username": subject}

    if settings.local_development_mode:
        @app.post("/api/local/prisma/login", status_code=204)
        async def login(payload: ParticipantLogin, request: Request):
            address = request.headers.get("x-forwarded-for", "").split(",", 1)[0] or (request.client.host if request.client else "local")
            if not app.state.login_limiter.allow(address):
                raise HTTPException(429, "Too many login attempts")
            user_id = await app.state.identity_factory().authenticate(payload.username, payload.password)
            if not user_id or not await app.state.identity_factory().prisma_user(user_id):
                raise HTTPException(401, "Invalid credentials or project access is not enabled")
            response = Response(status_code=204)
            response.set_cookie(cookie_name, issue_session(app.state.session_key, f"local-prisma:{user_id}"),
                                max_age=28800, secure=settings.cookie_secure, httponly=True, samesite="strict", path="/")
            return response

        @app.get("/api/local/prisma/workspace")
        async def workspace(subject=Depends(require_viewer)):
            user = await app.state.identity_factory().prisma_user(subject.removeprefix("local-prisma:"))
            if not user:
                raise HTTPException(404, "Sign in with a local participant account")
            return {"mode": "SIMULATED", "project": "Territorial Control", "user": user,
                    "project_access": {"workspace_path": "/Workspace/medallon/prisma", "role": "reader", "simulated": True},
                    "viewer_url": "/prisma/", "message": "Local access simulation. No OCI identity or email was created."}

        @app.put("/api/admin/prisma/users/{user_id}")
        async def grant(user_id: str, payload: ProjectGrant, _admin=Depends(require_admin)):
            identity = app.state.identity_factory()
            if not await identity.get_lab_user(user_id):
                raise HTTPException(404, "Participant not found")
            await identity.grant_prisma(user_id, payload.enabled)
            return {"enabled": payload.enabled, "mode": "SIMULATED"}

    return require_viewer
