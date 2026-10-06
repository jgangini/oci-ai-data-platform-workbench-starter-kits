import asyncio
import json
from types import SimpleNamespace

import pytest

from app.identity import LocalIdentityClient


def test_local_user_credentials_material_and_permission_survive_restart(tmp_path):
    settings = SimpleNamespace(local_development_mode=True, local_identity_artifact_dir=str(tmp_path))

    async def run():
        identity = LocalIdentityClient(settings)
        registered = await identity.prepare_registration("Analista Bogotá", "analista@example.com")
        welcome_path = tmp_path / f"welcome-{registered.user_id}.json"
        welcome = json.loads(welcome_path.read_text(encoding="utf-8"))
        password = welcome["password"]
        assert welcome["mode"] == "SIMULADO" and welcome["login_url"] == "/local/gods-eye-view/login"
        assert password not in (tmp_path / "identity-state.json").read_text(encoding="utf-8")
        assert await identity.authenticate(welcome["username"], password) is None
        await identity.activate_registration(registered.user_id)
        assert await identity.gods_eye_view_user(registered.user_id) is None  # Active is not Gods Eye View permission.
        assert await identity.authenticate(welcome["username"], password) is None
        await identity.grant_gods_eye_view(registered.user_id, True)
        await identity.record_material(registered.user_id, {
            "participant_key": "local-analyst", "labs": [{"lab_id": "banking"}],
            "password_hash": "must-not-leak", "aidp_url": "https://not-local.example",
        })
        restarted = LocalIdentityClient(settings)
        assert await restarted.authenticate("ANALISTA@example.com", password) == registered.user_id
        assert await restarted.authenticate(welcome["username"], "wrong-password") is None
        public = await restarted.gods_eye_view_user(registered.user_id)
        assert public["material"]["labs"] == [{"lab_id": "banking"}]
        assert public["material"]["aidp_url"] == "/local/gods-eye-view/workspace"
        assert "password" not in json.dumps(await restarted.list_lab_users())
        assert "password" not in json.dumps(await restarted.list_users_by_ocids({registered.user_ocid}))
        assert json.loads(welcome_path.read_text(encoding="utf-8"))["password"] == password
        reconciled = await restarted.prepare_registration("Analista Bogotá", "analista@example.com")
        assert reconciled.user_id == registered.user_id
        assert await restarted.authenticate(welcome["username"], password) is None
        await restarted.activate_registration(registered.user_id)
        await restarted.grant_gods_eye_view(registered.user_id, False)
        assert await LocalIdentityClient(settings).gods_eye_view_user(registered.user_id) is None
        await restarted.delete_lab_user(registered.user_id)
        assert not welcome_path.exists()
        assert await LocalIdentityClient(settings).list_lab_users() == []

    asyncio.run(run())


def test_local_artifacts_cannot_be_enabled_in_cloud(tmp_path):
    with pytest.raises(ValueError, match="LOCAL_DEVELOPMENT_MODE"):
        LocalIdentityClient(SimpleNamespace(local_development_mode=False, local_identity_artifact_dir=str(tmp_path)))
