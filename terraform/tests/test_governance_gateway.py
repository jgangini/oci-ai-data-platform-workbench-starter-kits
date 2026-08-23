from pathlib import Path


ROOT = Path(__file__).parents[2]


def test_oci_provider_matches_resource_manager_runtime() -> None:
    versions = (ROOT / "terraform/a_versions.tf").read_text(encoding="utf-8")
    assert 'version = "= 8.28.0"' in versions


def test_devops_pipeline_targets_only_the_governance_cluster() -> None:
    terraform = (ROOT / "terraform/i_oci_data_governance_deployment.tf").read_text(encoding="utf-8")
    assert "resource.type = 'devopsdeploypipeline'" in terraform
    assert "resource.id = '${oci_devops_deploy_pipeline.governance[0].id}'" in terraform
    assert "where target.cluster.id = '${oci_containerengine_cluster.governance[0].id}'" in terraform
    assert 'network_channel_type = "PRIVATE_ENDPOINT_CHANNEL"' in terraform
    assert 'deploy_artifact_source_type = "INLINE"' in terraform
    assert 'deploy_stage_type  = "OKE_DEPLOYMENT"' in terraform
    assert 'trigger_new_devops_deployment = true' in terraform
    assert 'resource "terraform_data" "governance_deploy_iam_propagation"' in terraform
    assert 'command = "sleep 120"' in terraform
    assert "terraform_data.governance_deploy_iam_propagation" in terraform


def test_devops_project_emits_service_logs_for_the_next_deployment() -> None:
    terraform = (ROOT / "terraform/i_oci_data_governance_deployment.tf").read_text(encoding="utf-8")
    assert 'resource "oci_logging_log_group" "governance_devops"' in terraform
    assert 'resource "oci_logging_log" "governance_devops"' in terraform
    assert 'log_type           = "SERVICE"' in terraform
    assert 'category    = "all"' in terraform
    assert 'service     = "devops"' in terraform
    assert 'source_type = "OCISERVICE"' in terraform
    assert "oci_logging_log.governance_devops" in terraform


def test_gateway_manifest_is_private_behind_public_oidc_gateway_and_immutable() -> None:
    manifest_path = ROOT / "terraform/templatefile/governance-gateway.yaml"
    manifest = manifest_path.read_text(encoding="utf-8")
    gateway = (ROOT / "terraform/i_oci_data_governance_gateway.tf").read_text(encoding="utf-8")
    edge = (ROOT / "terraform/i_oci_data_governance_edge.tf").read_text(encoding="utf-8")
    assert 'service.beta.kubernetes.io/oci-load-balancer-internal: "true"' in manifest
    assert "oci.oraclecloud.com/reserved-private-ips" in manifest
    assert "- --port\n            - \"8080\"" in manifest
    assert "governance_gateway.tls_entrypoint" not in manifest
    assert "GOVERNANCE_TLS_SECRET_OCID" not in manifest
    assert "GOVERNANCE_OIDC_AUTHORITY" in manifest
    assert "GOVERNANCE_TOKENIZATION_KEY_OCID" in manifest
    assert "GOVERNANCE_TOKENIZATION_CRYPTO_ENDPOINT" in manifest
    assert "readinessProbe:" in manifest
    assert "path: /readyz" not in manifest
    assert manifest.count("path: /healthz") == 2
    assert "secretName:" not in manifest
    assert "@sha256:" in gateway
    assert "private_key_pem" not in manifest
    assert "BEGIN PRIVATE KEY" not in manifest
    assert 'resource "oci_apigateway_gateway" "governance"' in edge
    assert 'type                        = "TOKEN_AUTHENTICATION"' in edge
    assert 'type = "STATIC_KEYS"' in edge
    assert "local.governance_gateway_scope" in edge
    assert "governance_gateway_oidc_static_jwks_json" in edge
    assert manifest_path.is_file()
    assert not (ROOT / "deploy/governance/gateway.yaml").exists()
    assert '"${path.module}/templatefile/governance-gateway.yaml"' in (
        ROOT / "terraform/i_oci_data_governance_deployment.tf"
    ).read_text(encoding="utf-8")


def test_oke_private_network_allows_worker_registration() -> None:
    network = (ROOT / "terraform/e_oci_core_vcn.tf").read_text(encoding="utf-8")
    endpoint = network.split('resource "oci_core_security_list" "governance_endpoint"', 1)[1].split(
        'resource "oci_core_security_list" "governance_workers"', 1
    )[0]
    workers = network.split('resource "oci_core_security_list" "governance_workers"', 1)[1].split(
        'resource "oci_core_security_list" "governance_service"', 1
    )[0]
    assert "min = 12250" in endpoint
    assert "max = 12250" in endpoint
    assert endpoint.count('source      = var._oci_governance.worker_subnet_cidr') >= 2
    assert 'description = "Allow OKE path MTU discovery"' in endpoint
    assert "type = 3" in endpoint
    assert "code = 4" in endpoint
    assert 'source      = "0.0.0.0/0"' in workers
    assert 'description = "Allow OKE path MTU discovery"' in workers


def test_governance_cross_variable_contract_uses_terraform_15_preconditions() -> None:
    variables = (ROOT / "terraform/b_variables.tf").read_text(encoding="utf-8")
    gateway = (ROOT / "terraform/i_oci_data_governance_gateway.tf").read_text(encoding="utf-8")
    assert 'resource "terraform_data" "validate_governance_inputs"' in gateway
    assert gateway.count("precondition {") == 4
    assert gateway.count("!var.enable_ai_data_governance ||") == 4
    assert "!var.enable_ai_data_governance" not in variables


def test_workload_identity_reads_only_named_runtime_inputs() -> None:
    gateway = (ROOT / "terraform/i_oci_data_governance_gateway.tf").read_text(encoding="utf-8")
    assert "request.principal.type='workload'" in gateway
    assert "request.principal.namespace='aidp-governance'" in gateway
    assert "request.principal.service_account='ai-data-governance'" in gateway
    assert "target.secret.id='${oci_vault_secret.governance_jdbc[0].id}'" in gateway
    assert "target.bucket.name='${oci_objectstorage_bucket.control[0].name}'" in gateway
    assert "target.object.name='${local.governance_jdbc_object}'" in gateway
    assert "target.key.id='${oci_kms_key.governance[0].id}'" in gateway
    assert "use keys" in gateway
    assert "manage secret" not in gateway
    assert 'regexall("-GPU-", source.source_name)' in gateway
    assert "local.governance_node_sources[0].image_id" in gateway
    assert "node_pool_option.governance[0].sources[0].image_id" not in gateway
    assert 'name               = "${local.name_prefix}-gov-workers"' in gateway
    assert 'name               = "${local.name_prefix}-governance-workers"' not in gateway


def test_control_bucket_centralizes_delta_tables_and_the_jdbc_driver() -> None:
    storage = (ROOT / "terraform/f_oci_objectstorage_bucket.tf").read_text(encoding="utf-8")
    deployment = (ROOT / "terraform/i_oci_data_governance_deployment.tf").read_text(encoding="utf-8")
    edge = (ROOT / "terraform/i_oci_data_governance_edge.tf").read_text(encoding="utf-8")
    manifest = (ROOT / "terraform/templatefile/governance-gateway.yaml").read_text(encoding="utf-8")
    assert 'resource "oci_objectstorage_bucket" "control"' in storage
    assert 'count          = var.enable_ai_data_governance ? 1 : 0' in storage
    assert 'name           = "oci_control"' in storage
    assert 'access_type    = "NoPublicAccess"' in storage
    assert "oci_objectstorage_bucket.control[0].name" in deployment
    assert '/delta"' in deployment
    assert "GOVERNANCE_CONTROL_LOCATION" in manifest
    assert "to read buckets" in edge and "to inspect objects" in edge
    assert edge.count("target.bucket.name = '${oci_objectstorage_bucket.control[0].name}'") == 2
    assert "to read objects" not in edge and "to manage objects" not in edge


def test_production_gateway_uses_delta_not_process_memory() -> None:
    api = (ROOT / "apps/governance_gateway/api.py").read_text(encoding="utf-8")
    runtime = (ROOT / "apps/governance_gateway/jdbc.py").read_text(encoding="utf-8")
    assert "JdbcControlStore(" in api and "runtime.connect" in api
    assert "MemoryControlStore" not in api
    assert "AIDP_GOVERNANCE_GATEWAY" in runtime
    assert "get_oke_workload_identity_resource_principal_signer" in runtime
