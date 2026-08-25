from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import httpx
from oci._vendor import requests

from .config import Settings


SCIM_CONSISTENCY_ATTEMPTS = 5
SCIM_CONSISTENCY_DELAY_SECONDS = 1


class IdentityConflict(Exception):
    pass


class IdentityPending(Exception):
    pass


class IdentityRejected(Exception):
    pass


class IdentityRace(Exception):
    pass


@dataclass(slots=True)
class RegistrationResult:
    status: str
    user_id: str
    user_ocid: str
    email: str
    was_developer: bool = False


class IdentityClient:
    def __init__(self, settings: Settings, *, client: Any | None = None) -> None:
        self.settings = settings
        self.client = client or requests.Session()
        self._async_test_client = isinstance(client, httpx.AsyncClient)
        self._session_lock = threading.Lock()
        self.signer: Any | None = None
        if client is None:
            import oci

            config = oci.config.from_file(settings.oci_config_file, "DEFAULT")
            self.signer = oci.signer.Signer(
                tenancy=config["tenancy"],
                user=config["user"],
                fingerprint=config["fingerprint"],
                private_key_file_location=config["key_file"],
                pass_phrase=config.get("pass_phrase"),
            )

    async def close(self) -> None:
        if self._async_test_client:
            await self.client.aclose()
            return
        await asyncio.to_thread(self.client.close)

    def _request_sync(self, method: str, path: str, **kwargs: Any) -> Any:
        extra_headers = kwargs.pop("headers", {})
        request_kwargs = {
            **kwargs,
            "headers": {"Accept": "application/json", **extra_headers},
            "timeout": 30,
        }
        if self.signer is not None:
            request_kwargs["auth"] = self.signer
        with self._session_lock:
            return self.client.request(
                method,
                f"{self.settings.identity_domain_url}{path}",
                **request_kwargs,
            )

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        if self._async_test_client:
            extra_headers = kwargs.pop("headers", {})
            return await self.client.request(
                method,
                f"{self.settings.identity_domain_url}{path}",
                headers={"Accept": "application/json", **extra_headers},
                **kwargs,
            )
        return await asyncio.to_thread(self._request_sync, method, path, **kwargs)

    async def find_user(self, email: str) -> dict[str, Any] | None:
        literal = _scim_literal(email)
        users = await self._users_matching(
            f"userName eq {literal} or emails.value eq {literal}"
        )
        matches = [user for user in users if _user_has_email(user, email)]
        if any(user.get("externalId") != self.settings.lab_marker for user in matches):
            raise IdentityConflict("An unmanaged Identity Domains account already uses this email")
        if len(matches) > 1:
            raise IdentityConflict("Identity Domains returned multiple users for this email")
        if matches and str(matches[0].get("userName", "")).casefold() != email.casefold():
            raise IdentityConflict("An Identity Domains account uses this email with a different username")
        return matches[0] if matches else None

    async def create_user(self, name: str, email: str) -> dict[str, Any]:
        name_parts = name.rsplit(" ", 1)
        given_name = name_parts[0]
        family_name = name_parts[-1]
        response = await self._request(
            "POST",
            "/admin/v1/Users",
            json={
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
                "userName": email,
                "name": {"formatted": name, "givenName": given_name, "familyName": family_name},
                "displayName": name,
                "emails": [{"value": email, "type": "work", "primary": True}],
                "active": True,
                "externalId": self.settings.lab_marker,
            },
        )
        if response.status_code == 409:
            raise IdentityRace("Identity Domains reported a concurrent user creation")
        if response.status_code in {400, 422}:
            raise IdentityRejected(_safe_error(response))
        response.raise_for_status()
        return response.json()

    async def ensure_activation_email(self, user_id: str) -> None:
        try:
            response = await self._request(
                "PUT",
                f"/admin/v1/UserActivationInitiator/{user_id}",
                headers={"Content-Type": "application/scim+json"},
                json={
                    "schemas": [
                        "urn:ietf:params:scim:schemas:oracle:idcs:UserActivationInitiator"
                    ]
                },
            )
        except (httpx.HTTPError, requests.exceptions.RequestException) as exc:
            raise IdentityPending("User exists; activation email initiation is still in progress") from exc
        if response.status_code in {200, 201, 204, 409}:
            return
        if response.status_code in {400, 422}:
            raise IdentityRejected(_safe_error(response))
        raise IdentityPending("User exists; activation email initiation is still in progress")

    async def _is_member(self, group_id: str, user_id: str) -> bool:
        response = await self._request(
            "GET",
            "/admin/v1/Users",
            params={
                "filter": f"id eq {_scim_literal(user_id)} and groups.value eq {_scim_literal(group_id)}",
                "count": 1,
            },
        )
        response.raise_for_status()
        return bool(response.json().get("Resources", []))

    async def add_member(self, group_id: str, user_id: str) -> None:
        if await self._is_member(group_id, user_id):
            return
        response = await self._request(
            "PATCH",
            f"/admin/v1/Groups/{group_id}",
            headers={"Content-Type": "application/scim+json"},
            json={
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [{"op": "add", "path": "members", "value": [{"value": user_id}]}],
            },
        )
        response.raise_for_status()

    async def remove_member(self, group_id: str, user_id: str) -> None:
        if not await self._is_member(group_id, user_id):
            return
        response = await self._request(
            "PATCH",
            f"/admin/v1/Groups/{group_id}",
            headers={"Content-Type": "application/scim+json"},
            json={
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [{"op": "remove", "path": f"members[value eq {_scim_literal(user_id)}]"}],
            },
        )
        response.raise_for_status()

    async def prepare_registration(self, name: str, email: str) -> RegistrationResult:
        try:
            return await self._prepare_registration(name, email)
        except (IdentityConflict, IdentityPending, IdentityRejected):
            raise
        except (httpx.HTTPError, requests.exceptions.RequestException) as exc:
            raise IdentityPending("Identity Domains is still reconciling this registration") from exc

    async def _prepare_registration(self, name: str, email: str) -> RegistrationResult:
        user = await self.find_user(email)
        created = False
        if not user:
            try:
                user = await self.create_user(name, email)
                created = True
            except IdentityRace:
                for _ in range(SCIM_CONSISTENCY_ATTEMPTS):
                    await asyncio.sleep(SCIM_CONSISTENCY_DELAY_SECONDS)
                    user = await self.find_user(email)
                    if user:
                        break
                if not user:
                    raise IdentityPending(
                        "Identity Domains accepted a concurrent user creation that is not visible yet"
                    )
        user_id, user_ocid = _user_coordinates(user)
        was_developer = await self._is_member(self.settings.developer_group_id, user_id)
        was_pending = await self._is_member(self.settings.pending_group_id, user_id)
        if not (was_developer or was_pending):
            await self.ensure_activation_email(user_id)
        try:
            await self.add_member(self.settings.pending_group_id, user_id)
            await self.remove_member(self.settings.developer_group_id, user_id)
        except Exception as exc:
            raise IdentityPending("User created; pending access reconciliation is still in progress") from exc
        return RegistrationResult(
            "created" if created else "reconciled",
            user_id,
            user_ocid,
            email,
            was_developer,
        )

    async def activate_registration(self, user_id: str) -> None:
        try:
            await self.add_member(self.settings.developer_group_id, user_id)
            await self.remove_member(self.settings.pending_group_id, user_id)
        except Exception as exc:
            raise IdentityPending("Lab material is ready; developer access activation is still in progress") from exc

    async def delete_lab_user(self, user_id: str) -> bool:
        response = await self._request("GET", f"/admin/v1/Users/{user_id}")
        if response.status_code == 404:
            return False
        response.raise_for_status()
        if response.json().get("externalId") != self.settings.lab_marker:
            raise IdentityConflict("Only users created by this lab can be deleted")
        await self.remove_member(self.settings.developer_group_id, user_id)
        await self.remove_member(self.settings.pending_group_id, user_id)
        response = await self._request("DELETE", f"/admin/v1/Users/{user_id}")
        if response.status_code == 404:
            return False
        response.raise_for_status()
        return True

    async def get_lab_user(self, user_id: str) -> dict[str, Any] | None:
        response = await self._request("GET", f"/admin/v1/Users/{user_id}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        user = response.json()
        if user.get("externalId") != self.settings.lab_marker:
            raise IdentityConflict("Only users created by this lab can be deleted")
        return {
            "id": str(user["id"]),
            "ocid": _user_coordinates(user)[1],
            "email": str(user.get("userName", "")),
        }

    async def healthcheck(self) -> None:
        response = await self._request(
            "GET",
            "/admin/v1/Users",
            params={"count": 1, "attributes": "id"},
        )
        response.raise_for_status()

    async def _users_matching(self, filter_expression: str) -> list[dict[str, Any]]:
        users: list[dict[str, Any]] = []
        start_index = 1
        while True:
            response = await self._request(
                "GET",
                "/admin/v1/Users",
                params={
                    "filter": filter_expression,
                    "startIndex": start_index,
                    "count": 100,
                    "attributes": "id,ocid,userName,displayName,name,emails,active,externalId",
                },
            )
            response.raise_for_status()
            body = response.json()
            page = body.get("Resources", [])
            users.extend(page)
            start_index += len(page)
            if not page or start_index > int(body.get("totalResults", len(users))):
                return users

    async def _users_in_group(self, group_id: str) -> list[dict[str, Any]]:
        return await self._users_matching(f"groups.value eq {_scim_literal(group_id)}")

    async def list_lab_users(self) -> list[dict[str, Any]]:
        managed_users = await self._users_matching(f"externalId eq {_scim_literal(self.settings.lab_marker)}")
        active_users = await self._users_in_group(self.settings.developer_group_id)
        pending_users = await self._users_in_group(self.settings.pending_group_id)
        membership = {str(user["id"]): "pending" for user in pending_users if user.get("id")}
        membership.update({str(user["id"]): "active" for user in active_users if user.get("id")})
        users_by_id: dict[str, dict[str, Any]] = {}
        for user in (*managed_users, *pending_users, *active_users):
            if user.get("id"):
                users_by_id.setdefault(str(user["id"]), user)
        users: list[dict[str, Any]] = []
        for user_id, user in users_by_id.items():
            status = membership.get(user_id, "pending")
            users.append(
                {
                    "id": user_id,
                    "ocid": user.get("ocid", ""),
                    "name": user.get("displayName") or user.get("name", {}).get("formatted") or "",
                    "email": user.get("userName", ""),
                    "status": status,
                    "active": bool(user.get("active", False)),
                    "managed": user.get("externalId") == self.settings.lab_marker,
                }
            )
        return sorted(users, key=lambda item: (item["status"], item["email"].lower()))

    async def list_users_by_ocids(self, user_ocids: set[str]) -> list[dict[str, Any]]:
        """Resolve platform-role members even when the starter kit did not create them."""
        return await self.list_users_by_principals(user_ocids, set())

    async def list_users_by_principals(
        self,
        user_ocids: set[str],
        group_ocids: set[str],
    ) -> list[dict[str, Any]]:
        """Resolve direct users and every user inherited through an OCI group role assignee."""
        users_by_id: dict[str, dict[str, Any]] = {}
        for user_ocid in sorted(user_ocids):
            if not user_ocid.startswith("ocid1.user."):
                continue
            matches = await self._users_matching(f"ocid eq {_scim_literal(user_ocid)}")
            if len(matches) > 1:
                raise IdentityPending("Identity Domains returned duplicate users for one OCI identifier")
            if not matches:
                continue
            user = matches[0]
            users_by_id[str(user["id"])] = _platform_role_user(
                user, user_ocid, self.settings.lab_marker
            )
        for group_ocid in sorted(group_ocids):
            response = await self._request(
                "GET",
                "/admin/v1/Groups",
                params={
                    "filter": f"ocid eq {_scim_literal(group_ocid)}",
                    "count": 2,
                    "attributes": "id,ocid",
                },
            )
            response.raise_for_status()
            body = response.json()
            matches = body.get("Resources", []) if isinstance(body, dict) else []
            resource_count = len(matches) if isinstance(matches, list) else -1
            try:
                total_results = int(body.get("totalResults", resource_count))
            except (TypeError, ValueError):
                total_results = -1
            if not isinstance(matches, list) or total_results != 1 or resource_count != 1:
                raise IdentityPending(
                    "Identity Domains has not published exactly one administrator group yet"
                )
            group = matches[0]
            if (
                not isinstance(group, dict)
                or str(group.get("ocid") or "") != group_ocid
                or not group.get("id")
            ):
                raise IdentityPending(
                    "Identity Domains has not published exactly one administrator group yet"
                )
            for user in await self._users_in_group(str(group["id"])):
                user_id, user_ocid = _user_coordinates(user)
                users_by_id.setdefault(
                    user_id,
                    _platform_role_user(user, user_ocid, self.settings.lab_marker),
                )
        return list(users_by_id.values())


class LocalIdentityClient:
    """In-memory Identity Domains substitute for the local Docker profile only."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.users: dict[str, dict[str, Any]] = {}
        self.group_members: dict[str, set[str]] = {}

    async def close(self) -> None:
        return None

    async def healthcheck(self) -> None:
        return None

    async def prepare_registration(self, name: str, email: str) -> RegistrationResult:
        normalized_email = email.casefold()
        for user in self.users.values():
            if user["email"].casefold() == normalized_email:
                was_developer = user["status"] == "active"
                user["status"] = "pending"
                return RegistrationResult(
                    "reconciled",
                    user["id"],
                    user["ocid"],
                    user["email"],
                    was_developer,
                )
        user_id = uuid4().hex
        user_ocid = f"ocid1.user.oc1..local{uuid4().hex}"
        self.users[user_id] = {
            "id": user_id,
            "ocid": user_ocid,
            "name": name,
            "email": email,
            "status": "pending",
            "active": True,
            "managed": True,
        }
        return RegistrationResult("created", user_id, user_ocid, email)

    async def activate_registration(self, user_id: str) -> None:
        user = self.users.get(user_id)
        if not user:
            raise IdentityPending("Local lab user is not ready")
        user["status"] = "active"

    async def list_lab_users(self) -> list[dict[str, Any]]:
        return sorted(self.users.values(), key=lambda item: item["email"].casefold())

    async def list_users_by_ocids(self, user_ocids: set[str]) -> list[dict[str, Any]]:
        return [
            user
            for user in self.users.values()
            if str(user.get("ocid") or "") in user_ocids
        ]

    async def list_users_by_principals(
        self,
        user_ocids: set[str],
        group_ocids: set[str],
    ) -> list[dict[str, Any]]:
        resolved_ocids = set(user_ocids)
        for group_ocid in group_ocids:
            if group_ocid not in self.group_members:
                raise IdentityPending(
                    "Local Identity Domains has not published the administrator group yet"
                )
            resolved_ocids.update(self.group_members[group_ocid])
        return await self.list_users_by_ocids(resolved_ocids)

    async def delete_lab_user(self, user_id: str) -> bool:
        return self.users.pop(user_id, None) is not None

    async def get_lab_user(self, user_id: str) -> dict[str, Any] | None:
        user = self.users.get(user_id)
        if not user:
            return None
        return {"id": user_id, "ocid": str(user["ocid"]), "email": str(user["email"])}


def _scim_literal(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _user_has_email(user: dict[str, Any], email: str) -> bool:
    values = {str(user.get("userName", "")).casefold()}
    emails = user.get("emails", [])
    if isinstance(emails, list):
        values.update(
            str(item.get("value", "")).casefold()
            for item in emails
            if isinstance(item, dict)
        )
    return email.casefold() in values


def _user_coordinates(user: dict[str, Any]) -> tuple[str, str]:
    user_id = str(user.get("id") or "")
    user_ocid = str(user.get("ocid") or "")
    if not user_id:
        raise IdentityPending("Identity Domains has not published the user identifier yet")
    if not user_ocid.startswith("ocid1.user."):
        raise IdentityPending("Identity Domains has not published the OCI user OCID yet")
    return user_id, user_ocid


def _platform_role_user(
    user: dict[str, Any], user_ocid: str, lab_marker: str
) -> dict[str, Any]:
    return {
        "id": str(user["id"]),
        "ocid": user_ocid,
        "name": user.get("displayName") or user.get("name", {}).get("formatted") or "",
        "email": user.get("userName", ""),
        "status": "active",
        "active": bool(user.get("active", False)),
        "managed": user.get("externalId") == lab_marker,
    }


def _safe_error(response: Any) -> str:
    try:
        payload = response.json()
        return str(payload.get("detail") or payload.get("message") or "Identity Domains rejected the request")
    except (json.JSONDecodeError, AttributeError):
        return "Identity Domains rejected the request"
