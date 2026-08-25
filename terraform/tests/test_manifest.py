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
    storage = (root / "terraform" / "f_oci_objectstorage_bucket.tf").read_text(encoding="utf-8")
    assert manifest["schema_version"] == 1
    assert manifest["project_id"] == "oci-aidp-cloud-migration-lab"
    assert manifest["terraform"] == {"path": "terraform", "package_oci_credentials": False}
    assert manifest["capabilities"] == {
        "compartment_modes": ["new", "existing"],
        "default_compartment_name": "oracle-ai-data-platform",
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
    password_pattern = "^(?!.*[Aa][Dd][Mm][Ii][Nn])(?=.*[A-Z])(?=.*[a-z])(?=.*[0-9])[^\"']{12,30}$"
    for name in ("autonomous_database_admin_password", "autonomous_database_wallet_password"):
        assert fields[name]["min_length"] == 12
        assert fields[name]["max_length"] == 30
        assert fields[name]["pattern"] == password_pattern
    assert fields["existing_autonomous_database_ocid"]["visible_when"] == {
        "field": "autonomous_database_mode",
        "equals": "existing",
    }
    for layer in ("landing", "bronze", "silver", "gold"):
        assert fields[f"{layer}_bucket_mode"]["group"] == "medallion"
        assert fields[f"{layer}_new_bucket_name"]["default"] == f"oci_{layer}"
        assert fields[f"{layer}_existing_bucket_name"]["options_source"] == "oci_active_buckets"
    assert fields["artifacts_bucket_mode"]["group"] == "medallion"
    assert "artifacts_new_bucket_name" not in fields
    assert "artifacts_existing_bucket_name" not in fields
    assert storage.count('name           = "oci_artifacts"') == 1
    assert storage.count('name      = "oci_artifacts"') == 1
    assert 'variable "artifacts_new_bucket_name"' not in variables
    assert 'variable "artifacts_existing_bucket_name"' not in variables
    assert "enable_ai_data_governance" not in fields
    assert "optional_addons" not in {field["group"] for field in fields.values()}
    assert "enable_medallion_architecture" not in fields
    assert not any(name.startswith("governance_gateway_") for name in fields)
    assert fields["admin_username"]["group"] == "application_vm"
    assert manifest["post_apply"]["secret_inputs"] == [
        "autonomous_database_admin_password",
        "autonomous_database_wallet_password",
    ]
    assert fields["deployment_mode"]["options"] == [
        {"value": "laboratory", "label": "Laboratory"},
        {"value": "production", "label": "Production"},
    ]
    assert fields["registration_code"]["visible_when"] == {
        "field": "deployment_mode",
        "equals": "laboratory",
    }
    assert manifest["form"]["email_access_fields"] == [
        "deployment_mode",
        "admin_username",
        "admin_password",
        "registration_code",
    ]
    assert manifest["presentation"]["title"] == "Oracle AI Data Platform Workbench Starter Kits"
    assert manifest["presentation"]["summary"].startswith("Deploys reusable Oracle AI Data Platform Workbench starter kits")
    assert manifest["presentation"]["href"] == "https://github.com/jgangini/oci-ai-data-platform-workbench-starter-kits"
    assert manifest["presentation"]["tags"] == ["VM", "VCN", "AI Data Platform Workbench", "Data Governance", "Object Storage", "IAM"]
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
        "operator_username",
        "preferred_vm_shape",
        "availability_domain_index",
    ]
    assert manifest["preflight"]["output_inputs"] == [
        "home_region",
        "operator_user_ocid",
        "operator_username",
        "preferred_vm_shape",
        "availability_domain_index",
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
        "catalog_name": "oci_medallion",
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
        "aidp_catalog_name": "oci_medallion",
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


def test_release_workflow_builds_frozen_amd64_assets_before_publish() -> None:
    root = Path(__file__).parents[2]
    workflow = (root / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )
    gate = 'python terraform/release_gate.py --source-root terraform --release-ref "$GITHUB_REF_NAME"'
    build = "docker build --platform linux/amd64"
    manifest = '(root / os.environ["RELEASE_MANIFEST_ASSET"]).write_text'
    draft = 'gh release create "$GITHUB_REF_NAME" --draft --verify-tag'
    upload = 'gh release upload "$GITHUB_REF_NAME"'
    publish = 'gh release edit "$GITHUB_REF_NAME" --draft=false'
    immutable = "- name: Verify published release is immutable"
    platform_check = '''test "$(docker image inspect "$image_tag" --format '{{.Os}}/{{.Architecture}}')" = "linux/amd64"'''
    assert 'tags: ["v*.*.*"]' in workflow
    assert 'tags: ["v2.2.0"]' not in workflow
    assert 'RELEASE_IMAGE_ASSET=aidp-lab-image-amd64.tar.gz' in workflow
    assert 'RELEASE_MANIFEST_ASSET=aidp-release.json' in workflow
    assert "RELEASE_REPOSITORY=https://github.com/$GITHUB_REPOSITORY" in workflow
    assert "docker save \"$image_tag\" | gzip -n -9" in workflow
    assert platform_check in workflow
    assert '"schema_version": 1' in workflow
    assert '"updater_protocol": 1' in workflow
    assert '"release": os.environ["GITHUB_REF_NAME"]' in workflow
    assert '"commit_sha": os.environ["RELEASE_COMMIT_SHA"]' in workflow
    assert '"repository": os.environ["RELEASE_REPOSITORY"]' in workflow
    assert '"image": {' in workflow
    assert '"asset_name": os.environ["RELEASE_IMAGE_ASSET"]' in workflow
    assert '"sha256": digest.hexdigest()' in workflow
    assert '"platform": "linux/amd64"' in workflow
    assert 'f\'aidp-lab:{os.environ["RELEASE_COMMIT_SHA"]}\'' in workflow
    assert "ghcr.io" not in workflow
    assert "docker push" not in workflow
    assert (
        0
        <= workflow.index(gate)
        < workflow.index(build)
        < workflow.index(manifest)
        < workflow.index(draft)
        < workflow.index(upload)
        < workflow.index(publish)
        < workflow.index(immutable)
    )


def test_release_workflow_reruns_only_mutate_drafts() -> None:
    root = Path(__file__).parents[2]
    workflow = (root / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )
    assert "group: release-${{ github.ref }}" in workflow
    assert "cancel-in-progress: false" in workflow
    assert 'gh api "repos/$GITHUB_REPOSITORY/releases/tags/$GITHUB_REF_NAME"' in workflow
    assert 'grep -q "HTTP 404" "$lookup_error"' in workflow
    assert "--json isDraft,isImmutable,isPrerelease" in workflow
    assert 'echo "mode=create"' in workflow
    assert 'echo "mode=draft"' in workflow
    assert 'echo "mode=immutable"' in workflow
    assert "Existing published release is mutable; refusing to edit or republish it." in workflow
    assert "if: steps.release.outputs.mode == 'immutable'" in workflow
    assert "immutable release asset manifest mismatch" in workflow
    assert workflow.count('test "$asset_names" = "$RELEASE_IMAGE_ASSET,$RELEASE_MANIFEST_ASSET"') == 2
    immutable_start = workflow.index("- name: Verify existing immutable release assets")
    immutable_end = workflow.index("- uses: hashicorp/setup-terraform@v3")
    immutable_verification = workflow[immutable_start:immutable_end]
    assert 'gh release download "$GITHUB_REF_NAME"' in immutable_verification
    assert "gh release edit" not in immutable_verification
    assert "gh release upload" not in immutable_verification
    assert workflow.count('gh release create "$GITHUB_REF_NAME"') == 1
    assert "--draft --verify-tag" in workflow
    assert "--clobber" in workflow
    assert 'cmp "$RELEASE_DIST/$RELEASE_IMAGE_ASSET"' in workflow
    assert "$'true\\tfalse\\tfalse'" in workflow
    assert "$'false\\ttrue\\tfalse'" in workflow
    for step in (
        "Create or reuse draft GitHub release",
        "Upload frozen assets to draft",
        "Publish GitHub release",
    ):
        guarded_step = workflow[workflow.index(f"- name: {step}") :]
        assert "if: steps.release.outputs.mode != 'immutable'" in guarded_step.split(
            "run:", 1
        )[0]


def test_runtime_security_contracts() -> None:
    root = Path(__file__).parents[2]
    attributes = (root / ".gitattributes").read_text(encoding="utf-8")
    nginx = (root / "docker/nginx.conf").read_text(encoding="utf-8")
    local_nginx = (root / "docker/nginx.oci-local.conf").read_text(encoding="utf-8")
    backend_requirements = (root / "apps/backend/requirements.txt").read_text(encoding="utf-8")
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
        assert "/api/admin/aidp/jdbc-driver" not in proxy
        assert "client_max_body_size 1m;" in proxy
        assert "proxy_read_timeout 300s;" in proxy
        assert "proxy_send_timeout 300s;" in proxy
    assert "fastapi==0.141.1" in backend_requirements
    assert "starlette==0.49.1" in backend_requirements
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
    assert 'required_version = ">= 1.5.7"' in (root / "a_versions.tf").read_text(encoding="utf-8")
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
    assert [path.name[0] for path in sorted(root.glob("*.tf"))] == list("abcdefghiij")
    assert not {"main.tf", "network.tf", "compute.tf", "storage.tf", "identity.tf", "aidp.tf", "outputs.tf", "providers.tf"} & {
        path.name for path in root.glob("*.tf")
    }
