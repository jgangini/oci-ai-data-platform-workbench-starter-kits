from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from app.aidp import AidpProvisionConflict, AidpProvisionError, AidpProvisionPending, UserMaterial
from app.config import Settings, SettingsStore
from app.identity import IdentityConflict, IdentityPending, IdentityRejected, RegistrationResult
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
        return [{
            "id": "user-id", "ocid": "ocid1.user.oc1..ada", "name": "Ada",
            "email": "ada@example.com", "status": "active", "active": True,
            "managed": True,
        }]

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

    @staticmethod
    def material(email: str, lab_id: str) -> UserMaterial:
        return UserMaterial(
            email, lab_id, "u101",
            f"/Workspace/medallon/u101_ada@example.com/{lab_id}",
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


def make_client(tmp_path: Path, mode: str = "active") -> TestClient:
    settings = Settings(
        admin_username="lab-admin",
        admin_password_hash=hash_secret("long-admin-password", iterations=1_000, salt=b"admin-test-salt"),
        registration_code_hash=hash_secret("ABCD-1234", iterations=1_000, salt=b"code-test-salt"),
        identity_domain_url="https://identity.example.test",
        developer_group_id="developers", pending_group_id="pending",
        aidp_workbench_url="https://example.datalake.oci.oraclecloud.com#?tenant=test&domain=Default",
        aidp_platform_id="ocid1.aidataplatform.oc1..test",
        aidp_workspace_name="aidp-lab-workspace-test", aidp_region="us-chicago-1",
        oci_config_file="/etc/aidp-lab/oci/config",
        objectstorage_namespace="namespace", bucket_name="aidp-data-test",
        lab_marker="lab-test", session_secret_file=str(tmp_path / "session.key"),
        aidp_settings_file=str(tmp_path / "settings.json"), cookie_secure=False,
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


def login(client: TestClient) -> None:
    response = client.post(
        "/api/admin/login",
        json={"username": "lab-admin", "password": "long-admin-password"},
    )
    assert response.status_code == 204
    assert LOCAL_COOKIE_NAME in response.cookies


def test_public_catalog_exposes_five_lineage_labs_and_available_agent(tmp_path: Path) -> None:
    payload = make_client(tmp_path).get("/api/config").json()
    assert [lab["lab_id"] for lab in payload["labs"]] == [
        "banking", "telecommunications", "telco_lineage", "retail", "healthcare", "agent"
    ]
    assert all(lab["available"] for lab in payload["labs"])
    assert all(lab["description"].strip() for lab in payload["labs"])
    assert payload["labs"][-1]["status"] == "available"
    assert payload["labs"][-1]["pack_version"] == "1.4.5"
    assert "industries" not in payload


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
    assert response.json()["aidp_platform_id"] == "ocid1.aidataplatform.oc1..test"


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
