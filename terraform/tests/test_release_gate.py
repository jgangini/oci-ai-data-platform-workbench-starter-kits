from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).parents[1] / "release_gate.py"
SPEC = importlib.util.spec_from_file_location("aidp_release_gate", MODULE_PATH)
assert SPEC and SPEC.loader
release_gate = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = release_gate
SPEC.loader.exec_module(release_gate)


def _context() -> dict[str, object]:
    return {
        "project_id": "oci-aidp-cloud-migration-lab",
        "region": "us-chicago-1",
        "compartment": "oci-aidp-cloud-migration-lab-5",
        "compartment_mode": "new",
        "source": {
            "repository": "https://github.com/jgangini/oci-aidp-cloud-migration-lab.git",
            "ref": "v2.1.16",
            "commit_sha": "0123456789abcdef0123456789abcdef01234567",
        },
    }


def _source(tmp_path: Path) -> Path:
    root = tmp_path / "terraform"
    root.mkdir()
    (root / "f_oci_objectstorage_bucket.tf").write_text(
        'resource "oci_objectstorage_bucket" "data" {\n  name = "aidp-data-safe"\n}\n',
        encoding="utf-8",
    )
    return root


def _plan(
    *,
    actions: list[str] | None = None,
    resource_type: str = "oci_core_instance",
    address: str | None = None,
) -> dict[str, object]:
    return {
        "resource_changes": [
            {
                "address": address or f"{resource_type}.lab",
                "mode": "managed",
                "type": resource_type,
                "change": {"actions": actions or ["create"], "after": {"display_name": "aidp-lab"}},
            }
        ]
    }


def test_context_requires_v212_source() -> None:
    release_gate.validate_context(_context())
    invalid = _context()
    invalid["source"] = {**invalid["source"], "ref": "main"}  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="v2.1.16"):
        release_gate.validate_context(invalid)


@pytest.mark.parametrize(
    "field,value,message",
    [
        ("project_id", "other", "project_id"),
        ("region", "invalid region", "effective OCI region"),
    ],
)
def test_context_rejects_wrong_release_target(field: str, value: str, message: str) -> None:
    context = _context()
    context[field] = value
    with pytest.raises(ValueError, match=message):
        release_gate.validate_context(context)


@pytest.mark.parametrize("name", ["", "contains spaces", "contains/slash", "x" * 101])
def test_context_rejects_invalid_oci_compartment_name(name: str) -> None:
    context = _context()
    context["compartment"] = name
    with pytest.raises(ValueError, match="compartment name"):
        release_gate.validate_context(context)


def test_context_accepts_editable_new_and_existing_compartments() -> None:
    for mode, name in (("new", "oci-aidp-cloud-migration-lab-5"), ("existing", "Shared_Lab.2026")):
        context = _context()
        context["compartment_mode"] = mode
        context["compartment"] = name
        release_gate.validate_context(context)

    invalid = _context()
    invalid["compartment_mode"] = "auto"
    with pytest.raises(ValueError, match="mode"):
        release_gate.validate_context(invalid)


def test_context_rejects_untrusted_repository_and_short_sha() -> None:
    context = _context()
    context["source"] = {**context["source"], "repository": "https://example.com/repo.git"}  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="trusted"):
        release_gate.validate_context(context)
    context = _context()
    context["source"] = {**context["source"], "commit_sha": "abc123"}  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="40-character"):
        release_gate.validate_context(context)


@pytest.mark.parametrize(
    "content,message",
    [
        ('resource "oci_opensearch_opensearch_cluster" "bad" {}', "OSCS/OpenSearch"),
        ('payload = { volumeType = "EXTERNAL" }', "external AIDP volume"),
        ('resource "oci_kms_vault" "bad" {}', "OCI Vault"),
        ('resource "oci_identity_domains_app" "bad" {}', "OAuth client"),
        ('resource "oci_identity_domains_user" "operator_copy" {}', "technical Identity Domains user"),
        ('resource "oci_identity_domains_group" "provisioner" {}', "technical Identity Domains group"),
        ('resource "oci_identity_domains_grant" "user_admin" {}', "technical Identity Domains grant"),
        ('value = "AIDP_LAB_PROVISIONER"', "AIDP_LAB_PROVISIONER"),
        ('statement = "Allow any-user to manage vnics in tenancy"', "VNIC"),
        ('statement = "Allow any-user to use subnets in tenancy"', "subnet"),
        ('statement = "Allow any-user to use network-security-groups in tenancy"', "NSG"),
        (
            'statement = "Allow service objectstorage-us-chicago-1 to manage object-family in tenancy"',
            "Object Storage deletion",
        ),
        ('name = "oci-aidp-cloud-migration-lab-2"', "lab-2/lab-3"),
    ],
)
def test_source_rejects_forbidden_release_content(tmp_path: Path, content: str, message: str) -> None:
    root = _source(tmp_path)
    (root / "z_forbidden.tf").write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        release_gate.validate_source(root)


def test_source_requires_oracle_managed_bucket_key(tmp_path: Path) -> None:
    root = _source(tmp_path)
    bucket = root / "f_oci_objectstorage_bucket.tf"
    bucket.write_text(bucket.read_text(encoding="utf-8").replace("name =", "kms_key_id = var.key\n  name ="), encoding="utf-8")
    with pytest.raises(ValueError, match="Oracle-managed"):
        release_gate.validate_source(root)


def test_source_accepts_minimal_safe_deployment(tmp_path: Path) -> None:
    release_gate.validate_source(_source(tmp_path))


def test_source_accepts_only_named_governance_control_resources(tmp_path: Path) -> None:
    root = _source(tmp_path)
    (root / "i_oci_data_governance_edge.tf").write_text(
        '\n'.join(
            (
                'resource "oci_identity_domains_app" "governance_public_client" {}',
                'resource "oci_kms_vault" "governance" {}',
                'resource "oci_kms_key" "governance" {}',
                'resource "oci_vault_secret" "governance_jdbc" {}',
            )
        ),
        encoding="utf-8",
    )
    release_gate.validate_source(root)


def test_source_accepts_developer_pending_groups_and_ignores_docs(tmp_path: Path) -> None:
    root = _source(tmp_path)
    (root / "h_oci_identity.tf").write_text(
        '\n'.join(
            (
                'resource "oci_identity_domains_group" "developers" {}',
                'resource "oci_identity_domains_group" "pending" {}',
            )
        ),
        encoding="utf-8",
    )
    (root / "README.md").write_text("The deployment must not create AIDP_LAB_PROVISIONER.", encoding="utf-8")
    release_gate.validate_source(root)


def test_plan_accepts_only_create_actions() -> None:
    release_gate.validate_plan(_plan())
    for actions in (["no-op"], ["update"], ["delete"], ["create", "delete"]):
        with pytest.raises(ValueError, match="fresh-only"):
            release_gate.validate_plan(_plan(actions=actions))


def test_plan_rejects_forbidden_resource_and_customer_managed_bucket_key() -> None:
    with pytest.raises(ValueError, match="OSCS/OpenSearch"):
        release_gate.validate_plan(_plan(resource_type="oci_opensearch_opensearch_cluster"))
    for resource_type, message in (
        ("oci_kms_vault", "OCI Vault"),
        ("oci_kms_key", "OCI Vault"),
        ("oci_vault_secret", "OCI Vault"),
        ("oci_identity_domains_app", "OAuth client"),
    ):
        with pytest.raises(ValueError, match=message):
            release_gate.validate_plan(_plan(resource_type=resource_type))
    bucket_plan = _plan(resource_type="oci_objectstorage_bucket")
    bucket_plan["resource_changes"][0]["change"]["after"]["kms_key_id"] = "ocid1.key.oc1..test"  # type: ignore[index]
    with pytest.raises(ValueError, match="customer-managed"):
        release_gate.validate_plan(bucket_plan)


def test_plan_accepts_only_named_governance_control_resources() -> None:
    for resource_type, address in (
        ("oci_kms_vault", "oci_kms_vault.governance[0]"),
        ("oci_kms_key", "oci_kms_key.governance[0]"),
        ("oci_vault_secret", "oci_vault_secret.governance_jdbc[0]"),
        ("oci_identity_domains_app", "oci_identity_domains_app.governance_public_client[0]"),
    ):
        release_gate.validate_plan(_plan(resource_type=resource_type, address=address))


def test_plan_rejects_technical_identity_resources_but_allows_lab_groups() -> None:
    for resource_type, address, message in (
        ("oci_identity_domains_user", "oci_identity_domains_user.operator_copy", "technical Identity Domains user"),
        ("oci_identity_domains_group", "oci_identity_domains_group.provisioner", "technical Identity Domains group"),
        ("oci_identity_domains_grant", "oci_identity_domains_grant.user_admin", "technical Identity Domains grant"),
    ):
        with pytest.raises(ValueError, match=message):
            release_gate.validate_plan(_plan(resource_type=resource_type, address=address))

    for name in ("developers", "pending"):
        release_gate.validate_plan(
            _plan(resource_type="oci_identity_domains_group", address=f"oci_identity_domains_group.{name}")
        )


def test_plan_rejects_aidp_lab_provisioner_literal() -> None:
    plan = _plan()
    plan["resource_changes"][0]["change"]["after"]["display_name"] = "AIDP_LAB_PROVISIONER"  # type: ignore[index]
    with pytest.raises(ValueError, match="AIDP_LAB_PROVISIONER"):
        release_gate.validate_plan(plan)


def test_plan_accepts_provider_computed_bucket_key_when_hcl_does_not_assign_one() -> None:
    bucket_plan = _plan(resource_type="oci_objectstorage_bucket")
    bucket_plan["resource_changes"][0]["change"]["after"]["kms_key_id"] = None  # type: ignore[index]
    bucket_plan["resource_changes"][0]["change"]["after_unknown"] = {"kms_key_id": True}  # type: ignore[index]
    release_gate.validate_plan(bucket_plan)


def test_cli_fails_closed_on_invalid_plan(tmp_path: Path) -> None:
    source = _source(tmp_path)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(_plan(actions=["update"])), encoding="utf-8")
    assert release_gate.main(["--source-root", str(source), "--plan-json", str(plan_path)]) == 1
