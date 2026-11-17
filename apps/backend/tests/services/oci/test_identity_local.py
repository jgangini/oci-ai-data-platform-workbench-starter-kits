import asyncio
import json
from types import SimpleNamespace

import pytest

from app.services.oci.identity import LocalIdentityClient


def test_local_user_material_survives_restart_without_viewer_credentials(tmp_path):
    settings = SimpleNamespace(local_development_mode=True, local_identity_artifact_dir=str(tmp_path))

    async def run():
        identity = LocalIdentityClient(settings)
        registered = await identity.prepare_registration("Analista", "analista@example.com")
        welcome_path = tmp_path / f"welcome-{registered.user_id}.json"
        welcome = json.loads(welcome_path.read_text(encoding="utf-8"))
        assert welcome["mode"] == "SIMULADO"
        assert not {"password", "login_url"} & welcome.keys()
        await identity.activate_registration(registered.user_id)
        await identity.record_material(registered.user_id, {
            "participant_key": "u101", "labs": [{"lab_id": "banking"}],
            "password_hash": "must-not-leak", "aidp_url": "https://not-local.example",
        })
        restarted = LocalIdentityClient(settings)
        users = await restarted.list_lab_users()
        assert len(users) == 1 and users[0]["status"] == "active"
        assert users[0]["material"]["labs"] == [{"lab_id": "banking"}]
        assert "password" not in json.dumps(users)
        assert "gods_eye_view_access" not in users[0]
        reconciled = await restarted.prepare_registration("Analista", "analista@example.com")
        assert reconciled.user_id == registered.user_id
        assert reconciled.was_developer
        assert (await restarted.list_lab_users())[0]["status"] == "pending"
        await restarted.activate_registration(registered.user_id)
        await restarted.delete_lab_user(registered.user_id)
        assert not welcome_path.exists()
        assert await LocalIdentityClient(settings).list_lab_users() == []

    asyncio.run(run())


def test_local_artifacts_cannot_be_enabled_in_cloud(tmp_path):
    with pytest.raises(ValueError, match="LOCAL_DEVELOPMENT_MODE"):
        LocalIdentityClient(SimpleNamespace(local_development_mode=False, local_identity_artifact_dir=str(tmp_path)))
