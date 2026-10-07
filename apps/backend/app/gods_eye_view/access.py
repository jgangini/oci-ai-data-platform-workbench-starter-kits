"""Shared viewer access uses signed sessions and explicit OCI or local membership."""
from fastapi import Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from urllib.parse import urlsplit
import httpx
from oci._vendor import requests

from ..security import issue_session, verify_session
from ..identity import IdentityConflict, IdentityPending, IdentityRejected
from ..viewer_auth import _application as viewer_application


class ParticipantLogin(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=256)


class ProjectGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool


def mount_access(app, require_admin, cookie_name):
    settings = app.state.settings

    async def require_viewer(request: Request):
        subject = verify_session(request.cookies.get(cookie_name, ""), app.state.session_key)
        if subject == settings.admin_username and not subject.startswith("viewer:"):
            return subject
        prefixes = ("viewer:",)
        if settings.local_development_mode:
            prefixes += ("local-gods-eye-view:", "local-territorial:", "local-prisma:")
        if subject and subject.startswith(prefixes):
            try:
                user = await app.state.identity_factory().gods_eye_view_user(subject.split(":", 1)[1])
            except (IdentityPending, httpx.HTTPError, requests.exceptions.RequestException):
                raise HTTPException(503, "Viewer access could not be verified") from None
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
        role = "admin" if subject == settings.admin_username and not subject.startswith("viewer:") else "reader"
        response.headers["X-Gods-Eye-View-Role"] = role
        response.headers["Cache-Control"] = "no-store"
        return {"username": subject, "role": role}

    @app.put("/api/admin/prisma/users/{user_id}", include_in_schema=False)
    @app.put("/api/admin/territorial/users/{user_id}", include_in_schema=False)
    @app.put("/api/admin/gods-eye-view/users/{user_id}")
    async def grant(user_id: str, payload: ProjectGrant, request: Request, _admin=Depends(require_admin)):
        if request.headers.get("origin"):
            origin = urlsplit(request.headers["origin"])
            host = request.headers.get("x-forwarded-host", request.headers.get("host", ""))
            if origin.scheme != request.url.scheme or origin.netloc != host or origin.path or origin.query or origin.fragment:
                raise HTTPException(403, "Same-origin request required")
        identity = app.state.identity_factory()
        try:
            if not await identity.get_gods_eye_view_account(user_id):
                raise HTTPException(404, "Participant not found")
            if payload.enabled and not settings.local_development_mode:
                module = await app.state.gods_eye_view_status()
                if not (module.get("installed") is True and module.get("enabled") is True and module.get("status") == "ready"):
                    raise HTTPException(409, "Deploy God's Eye View before granting access")
                await viewer_application(identity, settings)
            await identity.grant_gods_eye_view(user_id, payload.enabled)
        except (IdentityConflict, IdentityRejected) as exc:
            raise HTTPException(409, str(exc)) from None
        except IdentityPending as exc:
            raise HTTPException(503, str(exc)) from None
        except (ValueError, KeyError, TypeError, AttributeError):
            raise HTTPException(503, "OCI viewer sign-in could not be verified") from None
        except (httpx.HTTPError, requests.exceptions.RequestException):
            raise HTTPException(503, "Viewer access could not be verified") from None
        return {"enabled": payload.enabled, "mode": "SIMULATED" if settings.local_development_mode else "OCI"}

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

    return require_viewer
