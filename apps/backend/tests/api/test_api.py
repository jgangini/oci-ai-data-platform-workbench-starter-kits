import json
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.services.oci.aidp import AidpProvisionConflict, AidpProvisionError, AidpProvisionPending, UserMaterial
from app.config import Settings, SettingsStore
from app.services.oci.identity import IdentityConflict, IdentityPending, IdentityRejected, RegistrationResult
from app.main import LOCAL_COOKIE_NAME, create_app
from app.security import RateLimiter, hash_secret


class FakeIdentity:
    def __init__(self, mode: str = "active") -> None:
        self.mode = mode
        self.activated: list[str] = []
        self.deleted: list[str] = []
        self.closed = False

    async def prepare_registration(self, name: str, email: str) -> RegistrationResult:
        if self.mode == "conflict":
            raise IdentityConflict("existing unmanaged account")
        if self.mode == "pending":
            raise IdentityPending("reconciliation pending")
        if self.mode == "rejected":
            raise IdentityRejected("upstream secret detail must not escape")
        status = "reconciled" if self.mode == "existing" else "created"
        return RegistrationResult(
            status,
            "user-id",
            "ocid1.user.oc1..ada",
            email,
            was_developer=self.mode == "existing",
        )

    async def activate_registration(self, user_id: str) -> None:
        if self.mode == "activation-pending":
            raise IdentityPending("activation pending")
        self.activated.append(user_id)

    async def list_lab_users(self) -> list[dict]:
        if self.mode == "unmanaged-admin":
            return []
        return [{
            "id": "user-id", "ocid": "ocid1.user.oc1..ada", "name": "Ada",
            "email": "ada@example.com", "status": "active", "active": True,
            "managed": True,
        }]

    async def list_users_by_ocids(self, user_ocids: set[str]) -> list[dict]:
        return await self.list_users_by_principals(user_ocids, set())

    async def list_users_by_principals(
        self, user_ocids: set[str], group_ocids: set[str]
    ) -> list[dict]:
        if self.mode == "group-admin":
            if "ocid1.group.oc1..platform-admins" not in group_ocids:
                return []
            return await self.list_lab_users()
        if "ocid1.user.oc1..ada" not in user_ocids:
            return []
        if self.mode == "unmanaged-admin":
            return [{
                "id": "user-id", "ocid": "ocid1.user.oc1..ada", "name": "Ada",
                "email": "ada@example.com", "status": "active", "active": True,
                "managed": False,
            }]
        return await self.list_lab_users()

    async def get_lab_user(self, user_id: str) -> dict | None:
        if self.mode == "foreign-delete":
            raise IdentityConflict("Only users created by this lab can be changed")
        return {
            "id": user_id, "ocid": "ocid1.user.oc1..ada", "email": "ada@example.com"
        } if user_id == "user-id" else None

    async def delete_lab_user(self, user_id: str) -> bool:
        if self.mode == "foreign-delete":
            raise IdentityConflict("Only users created by this lab can be deleted")
        self.deleted.append(user_id)
        return user_id == "user-id"

    async def healthcheck(self) -> None:
        if self.mode == "health-fail":
            raise RuntimeError("upstream secret detail must not escape")

    async def close(self) -> None:
        self.closed = True


class FakeAidp:
    def __init__(self, mode: str = "active") -> None:
        self.mode = mode
        self.assignments: dict[str, list[str]] = {
            "ocid1.user.oc1..ada": ["banking"]
        }
        self.cleaned: list[str] = []
        self.admin_ocids = set() if mode == "not-admin" else {"ocid1.user.oc1..ada"}
        self.admin_groups = {"ocid1.group.oc1..platform-admins"} if mode == "group-admin" else set()
        if mode == "group-admin":
            self.admin_ocids.clear()

    @staticmethod
    def material(email: str, lab_id: str) -> UserMaterial:
        return UserMaterial(
            email, lab_id, "u101",
            f"/Workspace/{lab_id}/u101_ada@example.com",
            f"wf_u101_{lab_id}", "1.0.0", "active", 101,
        )

    async def provision_user(
        self, user_ocid: str, email: str, lab_ids: list[str], participant_code: int
    ):
        assert participant_code == 101
        if self.mode == "aidp-pending":
            raise AidpProvisionPending("workbench is creating shared material", "schemas")
        if self.mode == "aidp-conflict":
            raise AidpProvisionConflict("lab operation conflicts")
        if self.mode == "error":
            raise AidpProvisionError("AIDP policy is missing")
        self.assignments[user_ocid] = list(lab_ids)
        return tuple(self.material(email, lab_id) for lab_id in lab_ids)

    async def list_user_labs(self, user_ocids: list[str]):
        return {
            user_ocid: [self.material("", lab_id) for lab_id in self.assignments.get(user_ocid, [])]
            for user_ocid in user_ocids
        }

    async def add_lab(self, user_ocid: str, email: str, lab_id: str) -> UserMaterial:
        if self.mode == "aidp-pending":
            raise AidpProvisionPending("lab provisioning pending", "content")
        self.assignments.setdefault(user_ocid, []).append(lab_id)
        return self.material(email, lab_id)

    async def redeploy_lab(
        self, user_ocid: str, email: str, lab_id: str, operation_id: str
    ) -> UserMaterial:
        if self.mode == "reset-pending":
            raise AidpProvisionPending("lab cleanup pending", "cleanup")
        if lab_id not in self.assignments.get(user_ocid, []):
            raise AidpProvisionConflict("lab is not assigned")
        return self.material(email, lab_id)

    async def delete_lab(self, user_ocid: str, lab_id: str, operation_id: str) -> None:
        labs = self.assignments.get(user_ocid, [])
        if len(labs) == 1:
            raise AidpProvisionConflict("The last lab cannot be removed")
        if lab_id in labs:
            labs.remove(lab_id)

    async def cleanup_user(self, user_ocid: str) -> None:
        if self.mode == "cleanup-pending":
            raise AidpProvisionPending("cleanup in progress")
        self.cleaned.append(user_ocid)

    async def healthcheck(self) -> None:
        if self.mode == "health-fail-aidp":
            raise RuntimeError("technical client detail must not escape")

    async def close(self) -> None:
        return None

    async def platform_admin_user_ocids(self) -> set[str]:
        return set(self.admin_ocids)

    async def platform_admin_principals(self) -> tuple[set[str], set[str]]:
        return set(self.admin_ocids), set(self.admin_groups)


def make_client(
    tmp_path: Path,
    mode: str = "active",
    registration_code_hash: str | None = None,
) -> TestClient:
    settings = Settings(
        admin_username="lab-admin",
        admin_password_hash=hash_secret("long-admin-password", iterations=1_000, salt=b"admin-test-salt"),
        registration_code_hash=(hash_secret("ABCD-1234", iterations=1_000, salt=b"code-test-salt")
                                if registration_code_hash is None else registration_code_hash),
        operator_username="joel.ganggini@oracle.com",
        identity_domain_url="https://identity.example.test",
        developer_group_id="developers", pending_group_id="pending",
        aidp_workbench_url="https://example.datalake.oci.oraclecloud.com#?tenant=test&domain=Default",
        aidp_platform_id="ocid1.aidataplatform.oc1..test",
        aidp_workspace_name="aidp-lab-workspace-test", aidp_region="us-chicago-1",
        oci_config_file="/etc/aidp-lab/oci/config",
        objectstorage_namespace="namespace", bucket_name="aidp-data-test",
        lab_marker="lab-test", session_secret_file=str(tmp_path / "session.key"),
        aidp_settings_file=str(tmp_path / "settings.json"),
        application_release="v2.2.0",
        application_commit_sha="a" * 40,
        application_update_dir=str(tmp_path / "update"),
        vm_update_enabled=True, application_deployment_profile="kits-only",
        cookie_secure=False,
    )
    app = create_app(settings)
    identity = FakeIdentity(mode)
    aidp = FakeAidp(mode)
    app.state.identity_factory = lambda: identity
    app.state.aidp_factory = lambda: aidp
    app.state.test_identity = identity
    app.state.test_aidp = aidp
    return TestClient(app)


def register_payload(**updates):
    return {
        "name": "Ada Lovelace", "email": "ada@example.com",
        "lab_ids": ["banking", "retail"], "code": "ABCD-1234", **updates,
    }


def test_participant_codes_start_at_101_and_are_stable_by_email(tmp_path: Path) -> None:
    settings = Settings(aidp_settings_file=str(tmp_path / "settings.json"))
    store = SettingsStore(settings)
    assert store.participant_code("Nadia.Cloud.AI@gmail.com") == 101
    assert store.participant_code("nadia.cloud.ai@gmail.com") == 101
    assert store.participant_code("next@example.com") == 102
    reloaded = SettingsStore(settings)
    assert reloaded.participant_code("nadia.cloud.ai@gmail.com") == 101
    assert reloaded.participant_code("third@example.com") == 103


@pytest.mark.parametrize("name", [None, "environment-artifacts.01", "a" * 128])
def test_artifacts_bucket_name_supports_default_and_selected_bucket(monkeypatch, name) -> None:
    monkeypatch.delenv("ARTIFACTS_BUCKET_NAME", raising=False)
    if name is not None:
        monkeypatch.setenv("ARTIFACTS_BUCKET_NAME", name)
    assert Settings.from_env().artifacts_bucket_name == (name or "oci_artifacts")


@pytest.mark.parametrize("name", ["", " ", "oci://bucket", "bucket/path", "bucket name", "bucket'", "a" * 129])
def test_artifacts_bucket_name_rejects_invalid_storage_names(monkeypatch, name) -> None:
    monkeypatch.setenv("ARTIFACTS_BUCKET_NAME", name)
    with pytest.raises(ValueError, match="ARTIFACTS_BUCKET_NAME"):
        Settings.from_env()


def login(client: TestClient) -> None:
    response = client.post(
        "/api/admin/login",
        json={"username": "lab-admin", "password": "long-admin-password"},
    )
    assert response.status_code == 204
    assert LOCAL_COOKIE_NAME in response.cookies


def test_public_catalog_exposes_only_five_participant_labs(tmp_path: Path) -> None:
    payload = make_client(tmp_path).get("/api/config").json()
    assert [lab["lab_id"] for lab in payload["labs"]] == [
        "banking", "telecommunications", "telco_lineage", "retail", "healthcare"
    ]
    assert all(lab["available"] for lab in payload["labs"])
    assert all(lab["description"].strip() for lab in payload["labs"])
    assert payload["labs"][-1]["status"] == "available"
    assert payload["labs"][-1]["pack_version"] == "2.0.0"
    assert "deployment_mode" not in payload
    assert "industries" not in payload


def test_registration_requires_a_code_and_administrator_can_configure_it(tmp_path: Path) -> None:
    client = make_client(tmp_path, registration_code_hash="")
    assert client.post("/api/register", json=register_payload()).status_code == 503
    assert client.put("/api/admin/settings", json={"registration_code": "ABCD-1234"}).status_code == 401
    login(client)
    assert client.get("/api/admin/session").json() == {
        "username": "lab-admin",
        "operator_username": "joel.ganggini@oracle.com",
    }
    configured = client.put("/api/admin/settings", json={"registration_code": "ABCD-1234"})
    assert configured.status_code == 200
    assert configured.json()["registration_code_configured"] is True
    assert "ABCD-1234" not in configured.text
    assert client.post("/api/register", json=register_payload()).status_code == 201


def test_health_reports_safe_failure_and_recreates_cached_client(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    failing = FakeIdentity("health-fail")
    recovered = FakeIdentity()
    client.app.state.identity_client = failing

    def identity_factory() -> FakeIdentity:
        if client.app.state.identity_client is None:
            client.app.state.identity_client = recovered
        return client.app.state.identity_client

    client.app.state.identity_factory = identity_factory
    response = client.get("/api/health")
    assert response.status_code == 503
    assert response.json() == {"detail": "Lab control services are unavailable"}
    assert response.headers["x-lab-health-failures"] == "identity:RuntimeError"
    assert "secret" not in response.text
    assert failing.closed

    client.app.state.health_expires_at = 0.0
    assert client.get("/api/health").status_code == 200
    assert client.app.state.identity_client is recovered


def test_admin_settings_exposes_the_aidp_platform_ocid(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    login(client)
    response = client.get("/api/admin/settings")
    assert response.status_code == 200
    assert response.json()["aidp_service_endpoint"] == "https://aidp.us-chicago-1.oci.oraclecloud.com"
    assert response.json()["aidp_platform_id"] == "ocid1.aidataplatform.oc1..test"
    assert "deployment_mode" not in response.json()
    assert response.json()["operator_username"] == "joel.ganggini@oracle.com"


def test_admin_time_zone_is_validated_persisted_and_shared(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    assert client.put("/api/admin/settings", json={"time_zone": "UTC"}).status_code == 401
    login(client)
    initial = client.get("/api/admin/settings").json()
    assert initial["time_zone"] == "America/Bogota"
    assert {"UTC", "America/Bogota", "America/Lima", "Europe/Madrid"} <= set(initial["time_zones"])
    for zone in ("UTC", "Europe/Madrid", "Asia/Kathmandu"):
        response = client.put("/api/admin/settings", json={"time_zone": zone})
        assert response.status_code == 200
        assert response.json()["time_zone"] == zone
        assert SettingsStore(client.app.state.settings).get_admin_settings()["time_zone"] == zone
        assert response.json()["registration_code_configured"] == initial["registration_code_configured"]
    for zone in ("Not/AZone", "../America/Bogota", "+25:00", ""):
        assert client.put("/api/admin/settings", json={"time_zone": zone}).status_code == 422
    assert client.get("/api/admin/settings").json()["time_zone"] == "Asia/Kathmandu"


def test_application_release_and_update_are_admin_only_and_idempotent(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    assert client.get("/api/admin/application").status_code == 401
    assert client.post(
        "/api/admin/application/update",
        json={"operation_id": "d9282ff6-8717-4db7-9f59-241469a2c526"},
    ).status_code == 401
    login(client)

    async def latest() -> dict[str, object]:
        tag = "v2.3.0"
        return {
            "tag_name": tag,
            "draft": False,
            "prerelease": False,
            "immutable": True,
            "published_at": "2026-08-24T12:00:00Z",
            "assets": [
                {
                    "name": name,
                    "browser_download_url": (
                        "https://github.com/jgangini/"
                        f"oci-ai-data-platform-workbench-starter-kits/releases/download/{tag}/{name}"
                    ),
                    "digest": f"sha256:{character * 64}",
                }
                for name, character in (
                    ("aidp-release.json", "a"),
                    ("aidp-lab-image-amd64.tar.gz", "b"),
                )
            ],
        }

    client.app.state.release_manager._fetch_latest = latest
    release = client.get("/api/admin/application")
    assert release.status_code == 200
    assert release.json()["current_release"] == "v2.2.0"
    assert release.json()["latest_release"] == "v2.3.0"
    assert release.json()["update_available"] is True
    package_ids = {item["package_id"] for item in release.json()["packages"]}
    assert package_ids == {"banking", "telecommunications", "telco_lineage", "retail", "healthcare"}

    operation_id = "d9282ff6-8717-4db7-9f59-241469a2c526"
    pending = client.post(
        "/api/admin/application/update", json={"operation_id": operation_id}
    )
    assert pending.status_code == 202
    assert pending.json()["status"] == "pending"
    request = json.loads((tmp_path / "update/inbox/request.json").read_text(encoding="utf-8"))
    assert request["operation_id"] == operation_id

    (tmp_path / "update/status").mkdir()
    (tmp_path / "update/status/status.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "operation_id": operation_id,
                "status": "succeeded",
                "message": "Application updated.",
            }
        ),
        encoding="utf-8",
    )
    complete = client.post(
        "/api/admin/application/update", json={"operation_id": operation_id}
    )
    assert complete.status_code == 200
    assert complete.json()["status"] == "active"


def test_registration_accepts_multiple_labs_and_activates_after_all_are_ready(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    response = client.post("/api/register", json=register_payload())
    assert response.status_code == 201
    assert [lab["lab_id"] for lab in response.json()["labs"]] == ["banking", "retail"]
    assert client.app.state.test_identity.activated == ["user-id"]


def test_public_registration_cannot_change_an_existing_lab_assignment(tmp_path: Path) -> None:
    client = make_client(tmp_path, "existing")
    response = client.post("/api/register", json=register_payload())
    assert response.status_code == 409
    assert response.json()["detail"] == (
        "Existing lab assignments can only be changed by an administrator"
    )
    assert client.app.state.test_aidp.assignments["ocid1.user.oc1..ada"] == ["banking"]
    assert client.app.state.test_identity.activated == ["user-id"]


def test_public_registration_can_retry_the_same_existing_assignment(tmp_path: Path) -> None:
    client = make_client(tmp_path, "existing")
    response = client.post(
        "/api/register", json=register_payload(lab_ids=["banking"])
    )
    assert response.status_code == 200
    assert [lab["lab_id"] for lab in response.json()["labs"]] == ["banking"]
    assert client.app.state.test_identity.activated == ["user-id"]


def test_registration_rejects_duplicates_unknown_empty_and_legacy_industry(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    cases = [
        register_payload(lab_ids=[]),
        register_payload(lab_ids=["banking", "banking"]),
        register_payload(lab_ids=["unknown"]),
        {"name": "Ada", "email": "ada@example.com", "industry": "banking", "code": "ABCD-1234"},
        register_payload(password="must-not-be-accepted"),
    ]
    assert all(client.post("/api/register", json=payload).status_code == 422 for payload in cases)


def test_invalid_code_and_opaque_rate_limit_run_before_identity(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    client.app.state.identity_factory = lambda: (_ for _ in ()).throw(
        AssertionError("identity must not be called")
    )
    assert client.post(
        "/api/register", json=register_payload(code="WXYZ-9999")
    ).status_code == 422

    client = make_client(tmp_path / "rate")
    client.app.state.register_limiter = RateLimiter(1, 60)
    assert client.post("/api/register", json=register_payload()).status_code == 201
    limited = client.post("/api/register", json=register_payload())
    assert limited.status_code == 429
    assert "Retry-After" in limited.headers


def test_pending_aidp_does_not_activate_identity(tmp_path: Path) -> None:
    client = make_client(tmp_path, "aidp-pending")
    response = client.post("/api/register", json=register_payload())
    assert response.status_code == 202
    assert response.json()["phase"] == "schemas"
    assert client.app.state.test_identity.activated == []


def test_admin_lists_labs_and_can_add_redeploy_and_remove_one(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    login(client)
    users = client.get("/api/admin/users")
    assert users.status_code == 200
    assert [lab["lab_id"] for lab in users.json()["users"][0]["labs"]] == ["banking"]
    assert "industry" not in users.json()["users"][0]
    assert users.json()["users"][0]["is_aidp_admin"] is True

    added = client.post(
        "/api/admin/users/user-id/labs", json={"lab_id": "retail"}
    )
    assert added.status_code == 200
    operation_id = "4ab88c5e-c9e3-47bf-8dca-97f7eb7d0d43"
    redeployed = client.post(
        "/api/admin/users/user-id/labs/banking/redeploy",
        json={"operation_id": operation_id},
    )
    assert redeployed.status_code == 200
    removed = client.delete(
        f"/api/admin/users/user-id/labs/retail?operation_id={operation_id}"
    )
    assert removed.status_code == 200
    protected = client.delete(
        f"/api/admin/users/user-id/labs/banking?operation_id={operation_id}"
    )
    assert protected.status_code == 409


def test_admin_lab_mutation_reports_retryable_pending(tmp_path: Path) -> None:
    client = make_client(tmp_path, "reset-pending")
    login(client)
    operation_id = "4ab88c5e-c9e3-47bf-8dca-97f7eb7d0d43"
    response = client.post(
        "/api/admin/users/user-id/labs/banking/redeploy",
        json={"operation_id": operation_id},
    )
    assert response.status_code == 202
    assert response.json()["phase"] == "cleanup"
    assert response.json()["operation_id"] == operation_id


def test_admin_delete_participant_cleans_aidp_before_identity(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    login(client)
    response = client.delete("/api/admin/users/user-id")
    assert response.status_code == 204
    assert client.app.state.test_aidp.cleaned == ["ocid1.user.oc1..ada"]
    assert client.app.state.test_identity.deleted == ["user-id"]


def test_local_development_mode_runs_multi_lab_lifecycle_without_oci(tmp_path: Path) -> None:
    settings = Settings(
        admin_username="local-admin",
        admin_password_hash=hash_secret("local-admin-password", iterations=1_000, salt=b"local-admin-salt"),
        registration_code_hash=hash_secret("AIDP-2026", iterations=1_000, salt=b"local-code-salt"),
        aidp_workbench_url="https://example.datalake.oci.oraclecloud.com#?tenant=local&domain=Default",
        session_secret_file=str(tmp_path / "session.key"), cookie_secure=False,
        aidp_settings_file=str(tmp_path / "settings.json"),
        local_development_mode=True,
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/health").status_code == 200
        assert client.post(
            "/api/admin/login",
            json={"username": "local-admin", "password": "local-admin-password"},
        ).status_code == 204
        created = client.post(
            "/api/admin/users",
            json={"name": "Ada Lovelace", "email": "ada@example.com", "lab_ids": ["healthcare", "retail"]},
        )
        assert created.status_code == 201
        user = client.get("/api/admin/users").json()["users"][0]
        assert user["participant_code"] == 101
        assert [lab["lab_id"] for lab in user["labs"]] == ["healthcare", "retail"]
        assert client.delete(f"/api/admin/users/{user['id']}").status_code == 204


def test_https_profile_keeps_host_cookie_prefix(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    secure = replace(client.app.state.settings, cookie_secure=True)
    secure_client = TestClient(create_app(secure), base_url="https://testserver")
    response = secure_client.post(
        "/api/admin/login",
        json={"username": "lab-admin", "password": "long-admin-password"},
    )
    assert response.status_code == 204
    assert "__Host-aidp_lab_admin" in response.headers["set-cookie"]


@pytest.mark.parametrize('method,path', [
    ('get', '/api/admin/modules/ai_social_network'),
    ('get', '/api/admin/modules/ai_social_network/agent-endpoint'),
    ('post', '/api/admin/users/user-id/modules/ai_social_network'),
    ('post', '/api/admin/users/user-id/modules/ai_social_network/redeploy'),
])
def test_retired_social_routes_are_not_mounted(tmp_path, method, path):
    client = make_client(tmp_path)
    assert getattr(client, method)(path).status_code == 404
    login(client)
    response = getattr(client, method)(path)
    assert response.status_code == 404
    assert not hasattr(client.app.state, 'social_factory')


@pytest.mark.parametrize("path", [
    "/api/admin/modules",
    "/api/admin/modules/ai_data_governance/agent-endpoint",
    "/api/admin/modules/ai_market_intelligence",
    "/api/admin/modules/gods_eye_view",
    "/api/viewer/session",
    "/viewer/login",
])
def test_migrated_module_and_viewer_routes_are_absent(tmp_path, path):
    client = make_client(tmp_path)
    assert client.get(path).status_code == 404
    login(client)
    assert client.get(path).status_code == 404


@pytest.mark.parametrize("payload", [
    {"lab_ids": []},
    {"lab_ids": ["ai_data_governance"]},
    {"lab_ids": ["ai_market_intelligence"]},
    {"lab_ids": ["gods_eye_view"]},
    {"lab_ids": ["banking"], "gods_eye_view": True},
])
def test_admin_creation_rejects_module_only_and_removed_access_fields(tmp_path, payload):
    client = make_client(tmp_path)
    login(client)
    response = client.post("/api/admin/users", json={
        "name": "Ada User", "email": "ada@example.com", **payload,
    })
    assert response.status_code == 422
    assert client.app.state.identity_factory().activated == []


def test_kits_lifespan_starts_without_module_clients(tmp_path):
    settings = Settings(local_development_mode=True, cookie_secure=False,
                        session_secret_file=str(tmp_path / "session.key"),
                        aidp_settings_file=str(tmp_path / "settings.json"))
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/health").status_code == 200
        config = client.get("/api/config").json()
        assert {kit["lab_id"] for kit in config["labs"]} == {
            "banking", "telecommunications", "telco_lineage", "retail", "healthcare",
        }
        assert not {"viewer_identity", "viewer_signin_enabled"} & config.keys()
        assert not hasattr(client.app.state, "market_factory")
        assert not hasattr(client.app.state, "gods_eye_view_aidp_factory")
