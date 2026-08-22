import base64
import importlib.util
import json
import re
import sys
from pathlib import Path


def test_deploy_studio_manifest_contract() -> None:
    root = Path(__file__).parents[2]
    manifest = json.loads((root / "terraform" / "deploy-studio.json").read_text(encoding="utf-8"))
    variables = (root / "terraform" / "b_variables.tf").read_text(encoding="utf-8")
    assert manifest["schema_version"] == 1
    assert manifest["project_id"] == "oci-aidp-cloud-migration-lab"
    assert manifest["terraform"] == {"path": "terraform", "package_oci_credentials": False}
    assert manifest["capabilities"] == {
        "compartment_modes": ["new", "existing"],
        "requires_genai_region": False,
        "database_profile": "new_or_existing",
        "region_selection": "subscribed_compatible",
        "regional_requirements": ["aidp", "autonomous_26ai_dw", "genai_chat"],
    }
    assert manifest["post_apply"]["requires_oci_credentials"] is True
    assert manifest["post_apply"]["entrypoint"] == "terraform/hooks/post_apply.py"
    assert manifest["post_apply"]["timeout_seconds"] == 3600
    assert (root / manifest["post_apply"]["entrypoint"]).is_file()
    fields = {field["name"]: field for field in manifest["form"]["fields"]}
    assert "home_region" not in fields
    assert "preferred_vm_shape" not in fields
    assert "vm_ocpus" not in fields
    assert "vm_memory_gbs" not in fields
    assert "ssh_allowed_cidr" not in fields
    assert fields["admin_password"]["transform"] == "pbkdf2_sha256"
    assert fields["registration_code"]["pattern"] == "^[A-Z]{4}-[0-9]{4}$"
    assert fields["registration_code"]["transform"] == "uppercase_pbkdf2_sha256"
    assert fields["agent_model_id"]["options_source"] == "oci_genai_chat_models"
    assert fields["agent_model_id"]["group"] == "enterprise_ai"
    assert fields["autonomous_database_version"]["options"] == [
        {"value": "26ai:DW", "label": "Oracle AI Database 26ai — Data Warehouse"}
    ]
    assert "autonomous_database_compute_count" not in fields
    assert re.search(
        r'variable\s+"autonomous_database_compute_count"\s*\{.*?default\s*=\s*4\s*\}',
        variables,
        re.DOTALL,
    )
    assert fields["autonomous_database_mode"]["group"] == "database"
    assert fields["existing_autonomous_database_ocid"]["visible_when"] == {
        "field": "autonomous_database_mode",
        "equals": "existing",
    }
    assert fields["enable_ai_data_governance"]["group"] == "optional_addons"
    assert not any(name.startswith("governance_gateway_") for name in fields)
    assert fields["admin_username"]["group"] == "application_vm"
    assert manifest["post_apply"]["secret_inputs"] == [
        "autonomous_database_admin_password",
        "autonomous_database_wallet_password",
    ]
    assert manifest["form"]["email_access_fields"] == ["admin_username", "admin_password", "registration_code"]
    assert manifest["presentation"]["title"] == "OCI AI Data Platform Cloud Migration Lab"
    assert manifest["presentation"]["tags"] == ["VM", "VCN", "OKE", "AI Data Platform", "Data Governance", "Object Storage", "IAM"]
    assert manifest["presentation"]["image"] == "/assets/oci-aidp-cloud-migration-lab.png"
    assert [step["key"] for step in manifest["run_steps"]] == [
        "queue",
        "credentials",
        "compartment",
        "policies",
        "stack",
        "plan",
        "apply",
        "network",
        "bucket",
        "compute",
        "database",
        "wallet",
        "application",
        "artifacts",
        "email",
        "complete",
    ]
    assert {"database", "wallet"}.issubset({step["key"] for step in manifest["run_steps"]})
    assert [field["name"] for field in manifest["preflight"]["runtime_fields"]] == [
        "home_region",
        "operator_user_ocid",
        "preferred_vm_shape",
        "availability_domain_index",
        "governance_gateway_image",
        "governance_gateway_oidc_authority",
        "governance_gateway_oidc_issuer",
        "governance_gateway_oidc_static_jwks_json",
    ]
    assert manifest["preflight"]["output_inputs"] == [
        "home_region",
        "operator_user_ocid",
        "preferred_vm_shape",
        "availability_domain_index",
        "governance_gateway_image",
        "governance_gateway_oidc_authority",
        "governance_gateway_oidc_issuer",
        "governance_gateway_oidc_static_jwks_json",
    ]
    runtime_fields = {
        field["name"]: field for field in manifest["preflight"]["runtime_fields"]
    }
    assert re.fullmatch(
        runtime_fields["operator_user_ocid"]["pattern"],
        "ocid1.user.oc1..operator",
    )
    assert (root / manifest["preflight"]["entrypoint"]).is_file()
    assert "aidp_workbench_url" in manifest["outputs"]
    assert "aidp_alias_key" in manifest["outputs"]
    assert {
        "aidp_catalog_name",
        "aidp_shared_compute_name",
        "aidp_external_volume_count",
        "aidp_runtime_ready",
        "operator_user_ocid",
        "home_region",
        "autonomous_database_id",
        "autonomous_database_mode",
        "autonomous_database_version",
        "autonomous_database_workload",
        "autonomous_database_compute_count",
        "enable_ai_data_governance",
        "governance_gateway_cluster_id",
        "governance_gateway_private_endpoint",
        "governance_gateway_deploy_pipeline_id",
        "governance_gateway_deployment_id",
        "governance_gateway_jdbc_user_ocid",
        "governance_gateway_url",
        "governance_gateway_oidc_scopes",
        "governance_gateway_jdbc_secret_ocid",
        "governance_gateway_jdbc_driver_bucket",
        "governance_gateway_jdbc_driver_object",
        "governance_gateway_tokenization_key_ocid",
        "governance_gateway_tokenization_crypto_endpoint",
        "governance_gateway_api_gateway_id",
        "agent_model_id",
    }.issubset(manifest["outputs"])
    assert "aidp_console_url" not in manifest["outputs"]


def test_hook_result_matches_runner_and_manifest_contract() -> None:
    root = Path(__file__).parents[2]
    manifest = json.loads((root / "terraform" / "deploy-studio.json").read_text(encoding="utf-8"))
    module_path = root / manifest["post_apply"]["entrypoint"]
    spec = importlib.util.spec_from_file_location("post_apply_manifest_contract", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    assert module.POST_APPLY_BUDGET_SECONDS < manifest["post_apply"]["timeout_seconds"]
    context = {
        "deployment_id": "deployment-test",
        "source": {"repository": "owner/repo", "ref": "v2.0.0-rc.1", "commit_sha": "0" * 40},
    }
    resources = {
        "catalog_key": "catalog",
        "catalog_name": "aidp_lab",
        "shared_compute_name": "aidp_cluster_shared_compute",
        "external_volume_count": 0,
        "runtime_ready": True,
    }
    result = module.build_success_result(
        context, resources, ["ready"], "https://aidp.example.test"
    )
    assert set(result) == {"events", "artifacts", "outputs"}
    assert result["outputs"] == {
        "aidp_workbench_url": "https://aidp.example.test",
        "aidp_catalog_name": "aidp_lab",
        "aidp_shared_compute_name": "aidp_cluster_shared_compute",
        "aidp_runtime_ready": True,
        "aidp_external_volume_count": 0,
    }
    assert {item["name"] for item in result["artifacts"]} == set(manifest["artifacts"])
    assert set(result["outputs"]).issubset(manifest["outputs"])
    artifact = json.loads(base64.b64decode(result["artifacts"][0]["content_b64"]))
    assert artifact["schema_version"] == 2
    assert artifact["resources"] == {
        **resources,
        "aidp_workbench_url": "https://aidp.example.test",
    }


def test_runtime_security_contracts() -> None:
    root = Path(__file__).parents[2]
    attributes = (root / ".gitattributes").read_text(encoding="utf-8")
    nginx = (root / "docker/nginx.conf").read_text(encoding="utf-8")
    local_nginx = (root / "docker/nginx.oci-local.conf").read_text(encoding="utf-8")
    entrypoint = (root / "docker/entrypoint.sh").read_text(encoding="utf-8")
    cloud_init = (root / "terraform/templatefile/user_data.sh").read_text(encoding="utf-8")
    variables = (root / "terraform/b_variables.tf").read_text(encoding="utf-8")
    providers = (root / "terraform/d_main.tf").read_text(encoding="utf-8")
    compute = (root / "terraform/g_oci_core_instance.tf").read_text(encoding="utf-8")
    identity = (root / "terraform/h_oci_identity.tf").read_text(encoding="utf-8")
    aidp = (root / "terraform/i_oci_ai_data_platform.tf").read_text(encoding="utf-8")
    storage = (root / "terraform/f_oci_objectstorage_bucket.tf").read_text(encoding="utf-8")
    backend_main = (root / "apps/backend/app/main.py").read_text(encoding="utf-8")
    assert "$proxy_add_x_forwarded_for" not in nginx
    assert "*.sh text eol=lf" in attributes
    assert nginx.count("X-Forwarded-For $remote_addr") == 1
    for proxy in (nginx, local_nginx):
        assert "proxy_read_timeout 300s;" in proxy
        assert "proxy_send_timeout 300s;" in proxy
    assert "limit_req" not in nginx
    assert "opaque_rate_limit_key" in backend_main
    assert 'headers={"Retry-After": str(' in backend_main
    assert 'chmod 0600 "$TLS_DIR/tls.key" 2>/dev/null || true' in entrypoint
    assert "firewall-offline-cmd --zone=public --add-service=http" in cloud_init
    assert "firewall-offline-cmd --zone=public --add-service=https" in cloud_init
    assert "firewall-cmd" not in cloud_init
    assert "download.docker.com/linux/centos/docker-ce.repo" in cloud_init
    assert "public.ecr.aws/docker/library/node" in cloud_init
    assert "public.ecr.aws/docker/library/python" in cloud_init
    assert "retry 5 docker build" in cloud_init
    assert "tee -a /var/log/aidp-lab-bootstrap.log /dev/console" in cloud_init
    assert 'AIDP bootstrap failed with exit $status' in cloud_init
    assert 'if [ "$HEALTH_STATUS" = "200" ]; then' in cloud_init
    assert '|| [ "$HEALTH_STATUS" = "503" ]' not in cloud_init
    assert "PUBLIC_IP=$(oci-public-ip -g" in cloud_init
    assert "grep -Eo '([0-9]{1,3}\\.){3}[0-9]{1,3}'" in cloud_init
    assert "tr -cd 'A-Za-z0-9.-'" in cloud_init
    assert "IP.1 = $PUBLIC_IP" in cloud_init
    assert "DNS.1 = $FQDN" in cloud_init
    assert "-addext" not in cloud_init
    assert "touch /var/local/userdata.done" in cloud_init
    assert '"$TLS_DIR:/etc/aidp-lab/tls:ro,Z"' in cloud_init
    assert '"$STATE_DIR:/var/lib/aidp-lab:Z"' in cloud_init
    assert 'alias  = "home"' in providers
    assert "region = var.home_region" in providers
    assert "shape_candidates" not in compute
    assert "oci_core_shapes" not in compute
    assert compute.count("var.preferred_vm_shape") == 2
    assert 'operating_system_version = "9"' in compute
    assert "var._oci_instance.shape.ocpus" in compute
    assert "var._oci_instance.shape.memory_in_gbs" in compute
    assert 'name          = "Compute Instance Run Command"' in compute
    assert 'desired_state = "ENABLED"' in compute
    assert 'variable "vm_ocpus"' not in variables
    assert 'variable "vm_memory_gbs"' not in variables
    assert 'resource "oci_identity_domains_user"' not in identity
    assert 'resource "oci_identity_domains_group" "provisioner"' not in identity
    assert 'resource "oci_identity_domains_grant"' not in identity
    assert "API Key Administrator" not in identity
    assert aidp.count("oci.home") == 1
    assert 'resource "oci_identity_domains_app"' not in identity
    assert 'resource "oci_kms_' not in identity
    assert 'resource "oci_vault_' not in identity
    assert 'resource "time_sleep"' not in identity
    assert 'resource "oci_objectstorage_object"' not in storage
    assert 'web_socket_endpoint == null ? "" : oci_ai_data_platform_ai_data_platform.lab.web_socket_endpoint' in aidp
    assert 'alias_key == null ? "" : oci_ai_data_platform_ai_data_platform.lab.alias_key' in aidp
    assert 'timeouts {' in aidp
    assert 'create = "120m"' in aidp
    network = (root / "terraform/e_oci_core_vcn.tf").read_text(encoding="utf-8")
    assert 'resource "oci_core_security_list" "web"' in network
    assert "security_list_ids          = [oci_core_security_list.web.id]" in network
    assert 'dns_label      = "aidplab"' in network
    assert 'dns_label                  = "public"' in network
    assert 'ingress_tcp_ports = [80, 443]' in variables
    assert "oci_core_network_security_group" not in network
    source_sha_block = variables.split('variable "source_commit_sha"', 1)[1]
    assert 'default     = "main"' not in source_sha_block
    assert 'regex("^[0-9a-f]{40}$"' in source_sha_block
    assert "force_destroy" in storage
    assert "prevent_destroy" not in storage
    assert "manage datalake" not in compute
    assert "manage ai-data-platforms" not in compute
    assert 'resource "oci_identity_policy" "vm_bootstrap"' in compute
    assert 'resource "oci_identity_policy" "vm_run_command"' in compute
    assert "manage instance-agent-command-family" in compute
    assert "use instance-agent-command-execution-family" in compute
    credential_bootstrap = (root / "apps/backend/app/credential_bootstrap.py").read_text(encoding="utf-8")
    assert 'temporary.chmod(0o600)' in credential_bootstrap
    assert 'path.chmod(0o600)' in credential_bootstrap
    assert "database_admin_password" not in credential_bootstrap
    assert "wallet_password" in credential_bootstrap
    assert "runtime_secrets" not in credential_bootstrap
    post_apply = (root / "terraform/hooks/post_apply.py").read_text(encoding="utf-8")
    assert '"admin_password": admin_password' not in post_apply
    assert '"database_admin_password"' not in post_apply
    assert "wallet_zip_b64" in post_apply
    assert '"$OCI_DIR:/etc/aidp-lab/oci:ro,Z"' in cloud_init
    assert "ocarun ALL=(root) NOPASSWD: /usr/local/sbin/aidp-lab-bootstrap-public-key" in cloud_init
    assert "AIDP_LAB_CREDENTIALS_V2_READY" in cloud_init
    assert "cat /opt/aidp-lab/bootstrap/key_public.pem" in cloud_init
    assert "cat /opt/aidp-lab/bootstrap/key.pem" not in cloud_init
    assert "cat /opt/aidp-lab/.oci/config" not in cloud_init
    assert "cat /opt/aidp-lab/.oci/key.pem" not in cloud_init
    assert "AIDP_WORKBENCH_URL=${aidp_workbench_url}" in cloud_init
    assert "AIDP_CONSOLE_URL" not in cloud_init


def test_terraform_files_follow_select_ai_order() -> None:
    root = Path(__file__).parents[2] / "terraform"
    expected = {
        "a_versions.tf",
        "b_variables.tf",
        "c_naming.tf",
        "d_main.tf",
        "e_oci_core_vcn.tf",
        "f_oci_objectstorage_bucket.tf",
        "g_oci_core_instance.tf",
        "h_oci_identity.tf",
        "i_oci_ai_data_platform.tf",
        "j_outputs.tf",
    }
    assert expected.issubset({path.name for path in root.glob("*.tf")})
    assert [path.name[0] for path in sorted(root.glob("*.tf"))] == list("abcdefghiiiiij")
    assert not {"main.tf", "network.tf", "compute.tf", "storage.tf", "identity.tf", "aidp.tf", "outputs.tf", "providers.tf"} & {
        path.name for path in root.glob("*.tf")
    }
