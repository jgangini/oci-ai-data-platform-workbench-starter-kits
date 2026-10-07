import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from app.config import Settings
from app.identity import IdentityClient, IdentityPending, IdentityRejected


def native_identity(*, active=True, pending=False, configured=True, confirm=True, group_marker="lab:gods_eye_view"):
    account = {"id": "participant", "ocid": "ocid1.user.oc1..participant",
               "userName": "participant@example.com", "displayName": "Participant", "active": active}
    members = {"pending"} if pending else set()
    patches = []

    def handler(request):
        path = request.url.path
        if request.method == "GET" and path == "/admin/v1/Users/participant":
            return httpx.Response(200, json=account)
        if request.method == "GET" and path.startswith("/admin/v1/Users/"):
            return httpx.Response(404)
        if request.method == "GET" and path == "/admin/v1/Groups/readers":
            return httpx.Response(200, json={"id": "readers", "externalId": group_marker})
        if request.method == "GET" and path == "/admin/v1/Users":
            expression = request.url.params["filter"]
            rows = [account] if any(f'groups.value eq "{group}"' in expression for group in members) else []
            return httpx.Response(200, json={"Resources": rows, "totalResults": len(rows)})
        if request.method == "PATCH":
            assert path == "/admin/v1/Groups/readers"  # No developer/admin role changes.
            operation = json.loads(request.content)["Operations"][0]
            patches.append(operation)
            if confirm:
                if operation["op"] == "add":
                    assert operation["value"] == [{"value": "participant"}]
                    members.add("readers")
                else:
                    assert operation["path"] == 'members[value eq "participant"]'
                    members.discard("readers")
            return httpx.Response(204)
        raise AssertionError(f"Unexpected request: {request.method} {path}")

    settings = Settings(identity_domain_url="https://identity.example.test", lab_marker="lab",
                        developer_group_id="developers", pending_group_id="pending",
                        gods_eye_view_group_id="readers" if configured else "")
    client = IdentityClient(settings, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return client, members, patches


def test_native_reader_grant_is_persisted_and_revoked_without_other_roles():
    async def run():
        client, members, patches = native_identity()
        assert await client.gods_eye_view_user("participant") is None
        await client.grant_gods_eye_view("participant", True)
        await client.grant_gods_eye_view("participant", True)
        assert members == {"readers"} and len(patches) == 1
        restarted = IdentityClient(client.settings, client=client.client)
        account = await restarted.gods_eye_view_user("participant")
        assert account["mode"] == "OCI" and account["managed"] is False
        assert account["gods_eye_view_access"] is account["territorial_access"] is account["prisma_access"] is True
        listed = await restarted.list_lab_users()
        assert len(listed) == 1 and listed[0]["id"] == "participant" and listed[0]["gods_eye_view_access"]
        await restarted.grant_gods_eye_view("participant", False)
        assert await client.gods_eye_view_user("participant") is None
        assert members == set() and len(patches) == 2
        await client.close()

    asyncio.run(run())


@pytest.mark.parametrize("active,pending", [(False, False), (True, True)])
def test_inactive_and_pending_accounts_cannot_enter_or_receive_grant_but_can_be_revoked(active, pending):
    async def run():
        client, members, patches = native_identity(active=active, pending=pending)
        members.add("readers")
        assert await client.gods_eye_view_user("participant") is None
        assert (await client.list_lab_users())[0]["gods_eye_view_access"] is False
        with pytest.raises(IdentityRejected, match="Only active users"):
            await client.grant_gods_eye_view("participant", True)
        await client.grant_gods_eye_view("participant", False)
        assert "readers" not in members and len(patches) == 1
        await client.close()

    asyncio.run(run())


def test_older_deployment_missing_group_keeps_users_inventory_and_denies_access():
    async def run():
        client, members, patches = native_identity(configured=False)
        members.add("developers")
        users = await client.list_lab_users()
        assert users[0]["status"] == "active" and users[0]["gods_eye_view_access"] is False
        assert await client.gods_eye_view_user("participant") is None
        with pytest.raises(IdentityPending, match="not configured"):
            await client.grant_gods_eye_view("participant", True)
        assert patches == []
        await client.close()

    asyncio.run(run())


@pytest.mark.parametrize("invalid_group", ["developers", "pending"])
def test_reader_group_must_not_alias_platform_groups(invalid_group):
    async def run():
        client, _, patches = native_identity()
        client.settings = replace(client.settings, gods_eye_view_group_id=invalid_group)
        with pytest.raises(IdentityPending, match="own reader group"):
            await client.grant_gods_eye_view("participant", True)
        assert patches == []
        await client.close()

    asyncio.run(run())


def test_reader_group_must_belong_to_exact_deployment():
    async def run():
        client, members, patches = native_identity(group_marker="different-lab:gods_eye_view")
        members.add("readers")
        with pytest.raises(IdentityPending, match="does not belong"):
            await client.gods_eye_view_user("participant")
        with pytest.raises(IdentityPending, match="does not belong"):
            await client.grant_gods_eye_view("participant", True)
        assert patches == []
        await client.close()

    asyncio.run(run())


def test_successful_patch_requires_confirmed_native_membership(monkeypatch):
    async def no_sleep(_seconds):
        pass

    monkeypatch.setattr("app.identity.asyncio.sleep", no_sleep)

    async def run():
        client, members, patches = native_identity(confirm=False)
        with pytest.raises(IdentityPending, match="not confirmed"):
            await client.grant_gods_eye_view("participant", True)
        assert members == set() and len(patches) == 1
        assert await client.gods_eye_view_user("participant") is None
        await client.close()

    asyncio.run(run())


def test_grants_require_existing_native_user_and_strict_boolean():
    async def run():
        client, _, patches = native_identity()
        with pytest.raises(IdentityRejected, match="not found"):
            await client.grant_gods_eye_view("missing", True)
        with pytest.raises(ValueError, match="boolean"):
            await client.grant_gods_eye_view("participant", "true")
        assert patches == []
        await client.close()

    asyncio.run(run())
