import json
from html import escape

import pytest
from pydantic import ValidationError

from app.config import Settings, SettingsStore
from app.viewer_identity import DEFAULT_IDENTITY, ViewerIdentity, render_identity
from test_api import login, make_client

HTML = '''<!doctype html><title>God's Eye View</title><div id="title-bar"><h1><span class="title-logo"></span> <span>GOD'S EYE <span class="title-accent">VIEW</span></span></h1><p class="subtitle">NO PLACE LEFT BEHIND</p></div><div class="loader-content"><h2>GOD'S EYE <span class="title-accent">VIEW</span></h2><p class="loader-status">Initializing photorealistic world...</p></div>'''


def test_identity_defaults_are_byte_identical_and_custom_values_are_text_only():
    for identity in (DEFAULT_IDENTITY, {"name": "", "description": "   "}):
        assert render_identity(HTML, identity) == HTML
    value = {"name": '<script>alert("name")</script>', "description": '</p><img src=x onerror=alert(1)>'}
    rendered = render_identity(HTML, value)
    assert rendered.count(escape(value["name"])) == 3
    assert rendered.count(escape(value["description"])) == 1
    assert "<script>" not in rendered and "<img src=x" not in rendered
    assert rendered.count("data-viewer-identity") == 2
    assert '<span class="title-logo"></span>' in rendered
    assert '<p class="loader-status">Initializing photorealistic world...</p>' in rendered
    with pytest.raises(ValueError, match="template changed"):
        render_identity(HTML.replace("<h2>", "<h2 class='new'>"), value)


def test_identity_settings_persist_and_other_settings_keep_them(tmp_path):
    settings = Settings(aidp_settings_file=str(tmp_path / "settings.json"))
    store = SettingsStore(settings)
    assert store.get_viewer_identity() == DEFAULT_IDENTITY
    saved = {"name": "Territorio & comunidad", "description": "Información verificada"}
    assert store.update_viewer_identity(saved) == saved
    code = store.participant_code("viewer@example.com")
    store.update("https://workbench.example.invalid", None, "UTC")
    reloaded = SettingsStore(settings)
    assert reloaded.get_viewer_identity() == saved
    assert reloaded.participant_code("viewer@example.com") == code
    assert reloaded.get_admin_settings()["time_zone"] == "UTC"
    assert reloaded.update_viewer_identity({"name": "", "description": "Custom"}) == {**DEFAULT_IDENTITY, "description": "Custom"}
    assert reloaded.update_viewer_identity({"name": "", "description": ""}) == DEFAULT_IDENTITY
    assert reloaded.get_admin_settings()["aidp_url"] == "https://workbench.example.invalid"
    values = json.loads((tmp_path / "settings.json").read_text())
    values["viewer_identity"] = {"name": ["not a string"], "description": "bad"}
    (tmp_path / "settings.json").write_text(json.dumps(values))
    assert reloaded.get_viewer_identity() == DEFAULT_IDENTITY
    assert reloaded.get_admin_settings()["time_zone"] == "UTC"


@pytest.mark.parametrize("payload", [
    {"name": "x" * 81, "description": ""}, {"name": "", "description": "x" * 201},
    {"name": None, "description": ""}, {"name": "ok", "description": "", "html": "extra"},
])
def test_identity_schema_rejects_invalid_inputs(payload):
    with pytest.raises(ValidationError):
        ViewerIdentity.model_validate(payload)


def test_identity_api_auth_and_persistence(tmp_path):
    client = make_client(tmp_path)
    path = "/api/admin/territorial/identity"
    assert client.get(path).status_code == 401
    assert client.put(path, json=DEFAULT_IDENTITY).status_code == 401
    assert client.get("/api/territorial/identity").status_code == 401
    assert client.get("/api/prisma/identity").status_code == 401
    login(client)
    assert client.get(path).json() == DEFAULT_IDENTITY
    identity = {"name": "A" * 80, "description": "B" * 200}
    assert client.put(path, json=identity).json() == identity
    response = client.get("/api/territorial/identity")
    assert response.json() == identity and response.headers["cache-control"] == "no-store"
    assert client.get("/api/prisma/identity").json() == identity
    assert client.put(path, json={**identity, "name": "A" * 81}).status_code == 422
    assert client.get(path).json() == identity
    assert client.put(path, json={"name": "", "description": ""}).json() == DEFAULT_IDENTITY
