from fastapi.testclient import TestClient

from governance_gateway.api import create_app
from governance_gateway.auth import AuthenticationError
from governance_gateway.catalog import CatalogColumn, CatalogSnapshot, new_object_id
from governance_gateway.jdbc import JdbcDriverValidationError
from governance_gateway.policy import ColumnRef, Principal
from governance_gateway.service import GovernanceService
from governance_gateway.store import MemoryControlStore
from governance_gateway.tokenization import VaultTokenizer


RUNTIME_IMAGE = "ghcr.io/jgangini/oci-aidp-data-governance-gateway@sha256:" + "a" * 64


class FakeAuthenticator:
    def authenticate(self, authorization: str | None) -> Principal:
        if authorization == "Bearer admin":
            return Principal("admin", roles=frozenset({"AI_DATA_PLATFORM_ADMIN"}))
        if authorization == "Bearer developer":
            return Principal("developer", roles=frozenset({"AIDP_DEVELOPER"}))
        if authorization == "Bearer tokenizer":
            return Principal("tokenizer", scopes=frozenset({"governance.tokenize"}))
        if authorization == "Bearer detokenizer":
            return Principal("detokenizer", scopes=frozenset({"governance.detokenize"}))
        raise AuthenticationError("A bearer token is required.")


def client() -> TestClient:
    store = MemoryControlStore()
    service = GovernanceService(store, lambda _sql, _parameters: ([], []))
    return TestClient(create_app(
        service,
        FakeAuthenticator(),
        jdbc_installed=lambda: False,
        runtime_image=RUNTIME_IMAGE,
    ))


def test_health_reports_the_gateway_release() -> None:
    gateway = client()
    assert gateway.app.version == "2.1.20"
    assert gateway.get("/healthz").json() == {"status": "ok", "version": "2.1.20"}


def test_missing_token_is_unauthorized() -> None:
    response = client().get("/v1/admin/status")
    assert response.status_code == 401


def test_status_is_available_to_admins_and_developers() -> None:
    gateway = client()
    developer = gateway.get("/v1/status", headers={"Authorization": "Bearer developer"})
    admin = gateway.get("/v1/status", headers={"Authorization": "Bearer admin"})
    assert developer.status_code == admin.status_code == 200
    assert developer.json() == {
        "administrator": False,
        "state": "initializing",
        "runtime": {"type": "OKE", "ready": True},
        "image": {
            "repository": "ghcr.io/jgangini/oci-aidp-data-governance-gateway",
            "reference": RUNTIME_IMAGE,
            "digest": "sha256:" + "a" * 64,
        },
        "jdbc": {"state": "required", "installed": False},
    }
    assert admin.json()["administrator"] is True


def test_status_does_not_initialize_the_data_plane_before_jdbc_is_installed() -> None:
    attempts: list[str] = []
    app = create_app(
        GovernanceService(MemoryControlStore(), lambda _sql, _parameters: ([], [])),
        FakeAuthenticator(),
        initialize=lambda: attempts.append("initialized"),
        jdbc_installed=lambda: False,
        runtime_image=RUNTIME_IMAGE,
    )
    response = TestClient(app).get(
        "/v1/status", headers={"Authorization": "Bearer developer"},
    )
    assert response.json()["state"] == "initializing"
    assert attempts == []


def test_openapi_contract_is_not_exposed() -> None:
    assert client().get("/openapi.json").status_code == 404


def test_developer_cannot_access_permissions_status() -> None:
    response = client().get("/v1/admin/status", headers={"Authorization": "Bearer developer"})
    assert response.status_code == 403


def test_admin_can_access_permissions_status() -> None:
    response = client().get("/v1/admin/status", headers={"Authorization": "Bearer admin"})
    assert response.status_code == 200


def test_only_admin_can_install_the_jdbc_driver_through_a_direct_par() -> None:
    calls: list[tuple[int, str]] = []
    digest = "b" * 64
    app = create_app(
        GovernanceService(MemoryControlStore(), lambda _sql, _parameters: ([], [])),
        FakeAuthenticator(),
        create_jdbc_upload=lambda size, sha256: {
            "upload_id": "opaque-upload-id",
            "upload_url": "https://objectstorage.example.test/p/token",
            "expires_at": "2026-08-23T12:00:00Z",
        },
        complete_jdbc_upload=lambda upload_id, size, sha256: calls.append((size, sha256)),
        jdbc_installed=lambda: True,
        runtime_image=RUNTIME_IMAGE,
    )
    gateway = TestClient(app)
    payload = {"size_bytes": 35_700_000, "sha256": digest}
    assert gateway.post(
        "/v1/admin/jdbc-driver:upload", json=payload,
        headers={"Authorization": "Bearer developer"},
    ).status_code == 403
    created = gateway.post(
        "/v1/admin/jdbc-driver:upload", json=payload,
        headers={"Authorization": "Bearer admin"},
    )
    assert created.status_code == 200
    assert created.json()["upload_url"].startswith("https://objectstorage.")
    assert created.json()["upload_id"] == "opaque-upload-id"
    completed = gateway.post(
        "/v1/admin/jdbc-driver:complete", json={**payload, "upload_id": "opaque-upload-id"},
        headers={"Authorization": "Bearer admin"},
    )
    assert completed.json()["jdbc"] == {"state": "ready", "installed": True}
    assert completed.json()["runtime"] == {"type": "OKE", "ready": True}
    assert calls == [(35_700_000, digest)]


def test_invalid_jdbc_zip_is_rejected_after_direct_upload() -> None:
    def invalid(_upload_id: str, _size: int, _sha256: str) -> None:
        raise JdbcDriverValidationError("The uploaded JDBC driver is not a ZIP archive.")

    app = create_app(
        GovernanceService(MemoryControlStore(), lambda _sql, _parameters: ([], [])),
        FakeAuthenticator(), complete_jdbc_upload=invalid,
    )
    response = TestClient(app).post(
        "/v1/admin/jdbc-driver:complete",
        json={"upload_id": "opaque-upload-id", "size_bytes": 100, "sha256": "c" * 64},
        headers={"Authorization": "Bearer admin"},
    )
    assert response.status_code == 422
    assert "not a ZIP" in response.json()["detail"]


def test_readiness_initializes_the_control_schema_once() -> None:
    attempts: list[str] = []
    store = MemoryControlStore()
    app = create_app(
        GovernanceService(store, lambda _sql, _parameters: ([], [])),
        FakeAuthenticator(),
        lambda: attempts.append("initialized"),
    )
    test_client = TestClient(app)
    assert test_client.get("/healthz").status_code == 200
    assert attempts == []
    assert test_client.get("/readyz").status_code == 200
    assert test_client.get("/readyz").status_code == 200
    assert attempts == ["initialized"]


class FakeCatalogSync:
    def __init__(self) -> None:
        self.calls = 0

    def synchronize(self) -> dict[str, object]:
        self.calls += 1
        return {"source": "AIDP_MASTER_CATALOG", "columns": 3}


class FakePrincipalDirectory:
    def principals(self) -> list[dict[str, str]]:
        return [{"principal_type": "ROLE", "principal_name": "AIDP_DEVELOPER"}]


def test_only_admin_can_start_catalog_sync() -> None:
    sync = FakeCatalogSync()
    store = MemoryControlStore()
    app = create_app(
        GovernanceService(store, lambda _sql, _parameters: ([], [])),
        FakeAuthenticator(),
        catalog_sync=sync,  # type: ignore[arg-type]
    )
    test_client = TestClient(app)
    assert test_client.post(
        "/v1/admin/catalog:sync", headers={"Authorization": "Bearer developer"}
    ).status_code == 403
    response = test_client.post("/v1/admin/catalog:sync", headers={"Authorization": "Bearer admin"})
    assert response.status_code == 200
    assert response.json()["columns"] == 3
    assert sync.calls == 1


def test_principal_directory_is_admin_only() -> None:
    store = MemoryControlStore()
    app = create_app(
        GovernanceService(store, lambda _sql, _parameters: ([], [])),
        FakeAuthenticator(), principal_directory=FakePrincipalDirectory(),
    )
    test_client = TestClient(app)
    assert test_client.get(
        "/v1/admin/principals", headers={"Authorization": "Bearer developer"},
    ).status_code == 403
    response = test_client.get("/v1/admin/principals", headers={"Authorization": "Bearer admin"})
    assert response.status_code == 200
    assert response.json()["items"][0]["principal_name"] == "AIDP_DEVELOPER"


def test_only_admin_can_classify_catalog_columns() -> None:
    column = CatalogColumn(
        "aidp_lab", "gold", "customers", "email", "aidp_lab", "guid", "aidp_lab.gold",
        "aidp_lab.gold.customers", "fingerprint", 0, "string", "", "STANDARD",
        "2026-01-01T00:00:00Z", "ocid1.user.oc1..owner",
    )
    store = MemoryControlStore()
    store.apply_catalog_snapshot(CatalogSnapshot("v1", "hash", (column,)))
    app = create_app(
        GovernanceService(store, lambda _sql, _parameters: ([], [])), FakeAuthenticator(),
        principal_directory=FakePrincipalDirectory(),
    )
    test_client = TestClient(app)
    object_id = new_object_id(column)
    payload = {
        "classification": "PERSONAL_DATA",
        "sensitivity": "CONFIDENTIAL",
        "owner": "data-owner@example.com",
        "review_state": "APPROVED",
    }
    assert test_client.put(
        f"/v1/admin/catalog/{object_id}/classification", json=payload,
        headers={"Authorization": "Bearer developer"},
    ).status_code == 403
    assert test_client.put(
        f"/v1/admin/catalog/{object_id}/classification", json=payload,
        headers={"Authorization": "Bearer admin"},
    ).status_code == 204
    record = test_client.get("/v1/admin/catalog", headers={"Authorization": "Bearer admin"}).json()["items"][0]
    assert record["classification"] == "PERSONAL_DATA"


def test_only_admin_can_manage_soft_deleted_column_policies() -> None:
    column = CatalogColumn(
        "aidp_lab", "gold", "customers", "email", "aidp_lab", "guid", "aidp_lab.gold",
        "aidp_lab.gold.customers", "fingerprint", 0, "string", "", "STANDARD",
        "2026-01-01T00:00:00Z", "ocid1.user.oc1..owner",
    )
    store = MemoryControlStore()
    store.apply_catalog_snapshot(CatalogSnapshot("v1", "hash", (column,)))
    app = create_app(
        GovernanceService(store, lambda _sql, _parameters: ([], [])), FakeAuthenticator(),
        principal_directory=FakePrincipalDirectory(),
    )
    test_client = TestClient(app)
    object_id = new_object_id(column)
    payload = {
        "object_id": object_id,
        "principal_type": "ROLE",
        "principal_name": "AIDP_DEVELOPER",
        "action": "MASK",
        "priority": 20,
        "enabled": True,
    }
    assert test_client.post(
        "/v1/admin/policies", json=payload, headers={"Authorization": "Bearer developer"},
    ).status_code == 403
    created = test_client.post(
        "/v1/admin/policies", json=payload, headers={"Authorization": "Bearer admin"},
    )
    assert created.status_code == 201
    policy_id = created.json()["policy_id"]
    listed = test_client.get(
        f"/v1/admin/policies?object_id={object_id}", headers={"Authorization": "Bearer admin"},
    )
    assert listed.status_code == 200
    assert listed.json()["items"] == [created.json()]
    assert test_client.delete(
        f"/v1/admin/policies/{policy_id}", headers={"Authorization": "Bearer admin"},
    ).status_code == 204
    assert store.list_access_policies()[0]["enabled"] is False
    assert any(event["decision"] == "POLICY_DISABLED" for event in store.audit_events)


def test_policy_requires_existing_catalog_column() -> None:
    store = MemoryControlStore()
    app = create_app(
        GovernanceService(store, lambda _sql, _parameters: ([], [])), FakeAuthenticator(),
        principal_directory=FakePrincipalDirectory(),
    )
    response = TestClient(app).post(
        "/v1/admin/policies",
        json={
            "object_id": "123e4567-e89b-12d3-a456-426614174000", "principal_type": "ROLE",
            "principal_name": "AIDP_DEVELOPER", "action": "DENY",
        },
        headers={"Authorization": "Bearer admin"},
    )
    assert response.status_code == 404


def test_policy_rejects_principal_not_visible_in_aidp_roles() -> None:
    response = TestClient(create_app(
        GovernanceService(MemoryControlStore(), lambda _sql, _parameters: ([], [])), FakeAuthenticator(),
        principal_directory=FakePrincipalDirectory(),
    )).post(
        "/v1/admin/policies",
        json={
            "object_id": "123e4567-e89b-12d3-a456-426614174000", "principal_type": "ROLE",
            "principal_name": "MADE_UP_ROLE", "action": "DENY",
        },
        headers={"Authorization": "Bearer admin"},
    )
    assert response.status_code == 422


class FakeCipher:
    def encrypt(self, plaintext: bytes) -> tuple[str, str]:
        return plaintext.hex(), "key-v1"

    def decrypt(self, ciphertext: str, key_version: str) -> bytes:
        assert key_version == "key-v1"
        return bytes.fromhex(ciphertext)


def test_tokenization_and_detokenization_require_separate_oauth_scopes() -> None:
    store = MemoryControlStore()
    tokenizer = VaultTokenizer(store, FakeCipher())
    app = create_app(
        GovernanceService(store, lambda _sql, _parameters: ([], [])), FakeAuthenticator(), tokenizer=tokenizer,
    )
    test_client = TestClient(app)
    assert test_client.post(
        "/v1/tokens", json={"value": "secret"}, headers={"Authorization": "Bearer admin"},
    ).status_code == 403
    created = test_client.post(
        "/v1/tokens", json={"value": "secret"}, headers={"Authorization": "Bearer tokenizer"},
    )
    assert created.status_code == 200
    token = created.json()["token"]
    assert test_client.post(
        f"/v1/tokens/{token}:reveal", headers={"Authorization": "Bearer tokenizer"},
    ).status_code == 403
    revealed = test_client.post(
        f"/v1/tokens/{token}:reveal", headers={"Authorization": "Bearer detokenizer"},
    )
    assert revealed.json() == {"value": "secret"}


def _governed_sql_client() -> tuple[TestClient, MemoryControlStore, list[tuple[str, dict[str, object]]]]:
    visible = CatalogColumn(
        "aidp_lab", "gold", "customers", "customer_id", "aidp_lab", "guid", "aidp_lab.gold",
        "aidp_lab.gold.customers", "fingerprint", 0, "long", "", "STANDARD",
        "2026-01-01T00:00:00Z", "ocid1.user.oc1..owner",
    )
    denied = CatalogColumn(
        "aidp_lab", "gold", "customers", "ssn", "aidp_lab", "guid", "aidp_lab.gold",
        "aidp_lab.gold.customers", "fingerprint", 1, "string", "", "STANDARD",
        "2026-01-01T00:00:00Z", "ocid1.user.oc1..owner",
    )
    store = MemoryControlStore()
    store.apply_catalog_snapshot(CatalogSnapshot("v1", "hash", (visible, denied)))
    store.register_query(
        "customer_ids", "SELECT customer_id FROM aidp_lab.gold.customers",
        [ColumnRef("aidp_lab", "gold", "customers", "customer_id")],
    )
    for column, action in ((visible, "ALLOW"), (denied, "DENY")):
        store.save_access_policy(
            None, new_object_id(column), "ROLE", "AIDP_DEVELOPER", action, 0, True, "admin-hash",
        )
    executions: list[tuple[str, dict[str, object]]] = []

    def execute(statement: str, parameters: dict[str, object]) -> tuple[list[str], list[tuple[object, ...]]]:
        executions.append((statement, parameters))
        return ["customer_id"], [(101,)]

    app = create_app(
        GovernanceService(store, execute), FakeAuthenticator(), principal_directory=FakePrincipalDirectory(),
    )
    return TestClient(app), store, executions


def test_public_catalog_and_registered_queries_are_filtered_by_effective_policy() -> None:
    test_client, _, _ = _governed_sql_client()
    headers = {"Authorization": "Bearer developer"}
    catalog = test_client.get("/v1/catalog", headers=headers)
    assert catalog.status_code == 200
    assert [item["column"] for item in catalog.json()["items"]] == ["customer_id"]
    queries = test_client.get("/v1/queries", headers=headers)
    assert queries.status_code == 200
    assert [item["query_id"] for item in queries.json()["items"]] == ["customer_ids"]
    assert queries.json()["free_sql"] is False


def test_free_sql_requires_an_admin_grant_to_an_existing_aidp_principal() -> None:
    test_client, store, executions = _governed_sql_client()
    developer = {"Authorization": "Bearer developer"}
    statement = "SELECT customer_id FROM aidp_lab.gold.customers"
    denied = test_client.post("/v1/sql:execute", json={"statement": statement}, headers=developer)
    assert denied.status_code == 403
    created = test_client.post(
        "/v1/admin/sql-access",
        json={"principal_type": "ROLE", "principal_name": "AIDP_DEVELOPER", "enabled": True},
        headers={"Authorization": "Bearer admin"},
    )
    assert created.status_code == 201
    allowed = test_client.post("/v1/sql:execute", json={"statement": statement}, headers=developer)
    assert allowed.status_code == 200
    assert allowed.json()["rows"] == [[101]]
    assert allowed.json()["mode"] == "FREE_SQL"
    assert executions and "customer_id" in executions[0][0]
    assert all(statement not in repr(event) for event in store.audit_events)
    assert any(event["decision"] == "FREE_SQL_GRANTED" for event in store.audit_events)


def test_free_sql_grants_are_admin_only_and_soft_disabled() -> None:
    test_client, store, _ = _governed_sql_client()
    payload = {"principal_type": "ROLE", "principal_name": "AIDP_DEVELOPER", "enabled": True}
    assert test_client.post(
        "/v1/admin/sql-access", json=payload, headers={"Authorization": "Bearer developer"},
    ).status_code == 403
    created = test_client.post(
        "/v1/admin/sql-access", json=payload, headers={"Authorization": "Bearer admin"},
    ).json()
    assert test_client.delete(
        f"/v1/admin/sql-access/{created['grant_id']}", headers={"Authorization": "Bearer admin"},
    ).status_code == 204
    assert store.list_sql_access()[0]["enabled"] is False
    assert any(event["decision"] == "FREE_SQL_DISABLED" for event in store.audit_events)
