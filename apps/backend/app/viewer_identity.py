"""Persisted viewer labels and escaped, pre-render native HTML branding."""
from html import escape

from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

DEFAULT_IDENTITY = {"name": "God's Eye View", "description": "NO PLACE LEFT BEHIND"}


class ViewerIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)
    name: str = Field(max_length=80)
    description: str = Field(max_length=200)


def render_identity(html: str, identity: dict) -> str:
    values = ViewerIdentity.model_validate(identity).model_dump()
    name = values["name"] or DEFAULT_IDENTITY["name"]
    description = values["description"] or DEFAULT_IDENTITY["description"]
    original = 'GOD\'S EYE <span class="title-accent">VIEW</span>'
    replacements = []
    if name != DEFAULT_IDENTITY["name"]:
        replacements.extend([
            ("<title>God's Eye View</title>", f"<title>{escape(name)}</title>"),
            (f"<span>{original}</span>", f"<span>{escape(name)}</span>"),
            (f"<h2>{original}</h2>", f"<h2>{escape(name)}</h2>"),
        ])
    if description != DEFAULT_IDENTITY["description"]:
        replacements.append(('<p class="subtitle">NO PLACE LEFT BEHIND</p>', f'<p class="subtitle">{escape(description)}</p>'))
    if replacements:
        replacements.extend((
            ('<div id="title-bar">', '<div id="title-bar" data-viewer-identity>'),
            ('<div class="loader-content">', '<div class="loader-content" data-viewer-identity>'),
        ))
    for original, replacement in replacements:
        if html.count(original) != 1:
            raise ValueError("The native viewer identity template changed")
        html = html.replace(original, replacement, 1)
    return html


def mount_identity(app, require_admin, require_viewer):
    @app.get("/api/prisma/identity", include_in_schema=False)
    @app.get("/api/territorial/identity")
    def viewer_identity(request: Request, _viewer=Depends(require_viewer)):
        return JSONResponse(request.app.state.settings_store.get_viewer_identity(), headers={"Cache-Control": "no-store"})

    @app.get("/api/admin/territorial/identity")
    def admin_identity(request: Request, _admin=Depends(require_admin)):
        return JSONResponse(request.app.state.settings_store.get_viewer_identity(), headers={"Cache-Control": "no-store"})

    @app.put("/api/admin/territorial/identity")
    def save_identity(payload: ViewerIdentity, request: Request, _admin=Depends(require_admin)):
        return JSONResponse(request.app.state.settings_store.update_viewer_identity(payload.model_dump()), headers={"Cache-Control": "no-store"})
