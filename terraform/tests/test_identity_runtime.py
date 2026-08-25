import re
from pathlib import Path


ROOT = Path(__file__).parents[2]


def _resource(source: str, resource_type: str, label: str) -> str:
    marker = f'resource "{resource_type}" "{label}"'
    start = source.index(marker)
    next_resource = source.find('\nresource "', start + len(marker))
    return source[start:] if next_resource == -1 else source[start:next_resource]


def test_operator_identity_is_reused_without_gateway_control_plane_resources() -> None:
    identity = (ROOT / "terraform/h_oci_identity.tf").read_text(encoding="utf-8")
    compute = (ROOT / "terraform/g_oci_core_instance.tf").read_text(encoding="utf-8")
    terraform = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "terraform").glob("*.tf"))

    assert 'resource "oci_identity_domains_user"' not in identity
    assert 'resource "oci_identity_domains_group" "provisioner"' not in identity
    assert 'resource "oci_identity_domains_grant"' not in identity
    assert 'resource "oci_identity_policy" "provisioner_runtime"' not in identity
    assert 'resource "oci_identity_domains_app"' not in identity
    assert 'resource "oci_kms_' not in identity
    assert 'resource "oci_vault_' not in identity
    assert 'resource "oci_identity_policy" "vm_secret"' not in compute
    assert "secret-bundles" not in compute
    for resource_type in (
        "oci_containerengine_",
        "oci_kms_",
        "oci_vault_",
        "oci_apigateway_",
        "oci_devops_",
    ):
        assert resource_type not in terraform
    for filename in (
        "i_oci_data_governance_gateway.tf",
        "i_oci_data_governance_edge.tf",
        "i_oci_data_governance_deployment.tf",
    ):
        assert not (ROOT / "terraform" / filename).exists()


def test_identity_groups_ignore_service_managed_schema_extensions() -> None:
    identity = (ROOT / "terraform/h_oci_identity.tf").read_text(encoding="utf-8")

    for group in ("developers", "pending"):
        block = _resource(identity, "oci_identity_domains_group", group)
        assert "ignore_changes = [schemas]" in block


def test_vm_receives_operator_credentials_through_one_use_encrypted_bootstrap() -> None:
    compute = (ROOT / "terraform/g_oci_core_instance.tf").read_text(encoding="utf-8")
    cloud_init = (ROOT / "terraform/templatefile/user_data.sh").read_text(encoding="utf-8")

    assert "vm_aidp_runtime" not in compute
    assert "manage datalake" not in compute
    assert re.search(
        r"operator_user_ocid\s*=\s*var\.operator_user_ocid",
        compute,
    )
    assert 'OCI_DIR="/opt/aidp-lab/.oci"' in cloud_init
    assert 'AUTONOMOUS_DIR="/opt/aidp-lab/autonomous"' in cloud_init
    assert 'BOOTSTRAP_DIR="/opt/aidp-lab/bootstrap"' in cloud_init
    assert 'install -d -m 0700 "$TLS_DIR" "$STATE_DIR" "$UPDATE_DIR" "$UPDATE_INBOX_DIR" "$UPDATE_STATUS_DIR" "$RELEASES_DIR" "$OCI_DIR" "$AUTONOMOUS_DIR" "$BOOTSTRAP_DIR"' in cloud_init
    assert 'openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072' in cloud_init
    assert '-m app.credential_bootstrap' in cloud_init
    assert 'OCI_EXPECTED_USER_OCID=${operator_user_ocid}' in cloud_init
    assert '"$OCI_DIR:/etc/aidp-lab/oci:rw,Z"' in cloud_init
    assert 'OCI_CONFIG_FILE=/etc/aidp-lab/oci/config' in cloud_init
    assert "AGENT_RUNTIME_SECRET_FILE" not in cloud_init
    assert "AIDP_RUNTIME_SECRET_FILE" not in cloud_init
    assert "runtime-secrets.json" not in cloud_init
    assert 'AUTONOMOUS_RUNTIME_FILE=/etc/aidp-lab/autonomous/runtime.json' in cloud_init
    assert '"$AUTONOMOUS_DIR:/etc/aidp-lab/autonomous:ro,Z"' in cloud_init
    assert "autonomous_database_admin_password" not in cloud_init
    assert 'rm -f "$BOOTSTRAP_DIR/key.pem" "$BOOTSTRAP_DIR/key_public.pem"' in cloud_init
    assert 'cat > /opt/aidp-lab/.env' in cloud_init
    assert '--env-file /opt/aidp-lab/.env' in cloud_init
    assert "IDENTITY_OAUTH" not in cloud_init
    assert "OAUTH_SECRET" not in cloud_init
    assert '"$OCI_DIR:/etc/aidp-lab/oci:ro,Z"' in cloud_init
    assert 'if [ "$HEALTH_STATUS" = "200" ]; then' in cloud_init
    assert '[ "$HEALTH_STATUS" = "503" ]' not in cloud_init


def test_run_command_returns_only_public_material_or_the_ready_sentinel() -> None:
    compute = (ROOT / "terraform/g_oci_core_instance.tf").read_text(encoding="utf-8")
    cloud_init = (ROOT / "terraform/templatefile/user_data.sh").read_text(encoding="utf-8")

    assert "manage instance-agent-command-family" in compute
    assert "use instance-agent-command-execution-family" in compute
    assert "target.object.name = '.bootstrap/operator-credentials.json'" in compute
    helper = cloud_init.split("cat >/usr/local/sbin/aidp-lab-bootstrap-public-key <<'EOF'", 1)[1].split("\nEOF", 1)[0]
    assert "AIDP_LAB_CREDENTIALS_V2_READY" in helper
    assert "[ -s /opt/aidp-lab/.oci/config ] && [ -s /opt/aidp-lab/.oci/key.pem ]" in helper
    assert "key_public.pem" in helper
    assert "bootstrap_version" in helper
    assert "cat /opt/aidp-lab/.oci/key.pem" not in helper
    assert "cat /opt/aidp-lab/.oci/config" not in helper
    assert "cat /opt/aidp-lab/bootstrap/key.pem" not in helper
    assert "ocarun ALL=(root) NOPASSWD: /usr/local/sbin/aidp-lab-bootstrap-public-key" in cloud_init


def test_vm_bootstrap_identity_is_authorized_before_instance_launch() -> None:
    compute = (ROOT / "terraform/g_oci_core_instance.tf").read_text(encoding="utf-8")

    dynamic_group = _resource(compute, "oci_identity_dynamic_group", "vm")
    bootstrap_policy = _resource(compute, "oci_identity_policy", "vm_bootstrap")
    instance = _resource(compute, "oci_core_instance", "lab")
    assert "oci_core_instance.lab.id" not in dynamic_group
    assert "instance.compartment.id" in dynamic_group
    assert "tag.${oci_identity_tag_namespace.vm_bootstrap.name}.${oci_identity_tag.vm_bootstrap.name}.value" in dynamic_group
    assert "use instance-agent-command-execution-family" in bootstrap_policy
    assert "target.object.name = '.bootstrap/operator-credentials.json'" in bootstrap_policy
    assert "oci_identity_policy.vm_bootstrap" in instance
    assert "terraform_data.validate_existing_autonomous_database" in instance
    assert "oci_containerengine_node_pool" not in instance
    assert '"${oci_identity_tag_namespace.vm_bootstrap.name}.${oci_identity_tag.vm_bootstrap.name}" = local.suffix' in instance


def test_required_aidp_policy_has_no_optional_or_search_resources() -> None:
    aidp = (ROOT / "terraform/i_oci_ai_data_platform.tf").read_text(encoding="utf-8")

    policy = _resource(aidp, "oci_identity_policy", "aidp_service")
    assert policy.count('"Allow any-user') == 10
    assert "use generative-ai-family" in policy
    assert "manage vnics" not in policy
    assert "use subnets" not in policy
    assert "use network-security-groups" not in policy
    assert "Allow service objectstorage-" not in policy
    assert "manage object-family" not in policy
    assert "opensearch" not in aidp.lower()
    assert "external_volume" not in aidp.lower()
