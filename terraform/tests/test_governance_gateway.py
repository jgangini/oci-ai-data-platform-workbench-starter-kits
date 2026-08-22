from pathlib import Path


ROOT = Path(__file__).parents[2]


def test_devops_pipeline_targets_only_the_governance_cluster() -> None:
    terraform = (ROOT / "terraform/i_oci_data_governance_deployment.tf").read_text(encoding="utf-8")
    assert "resource.type = 'devopsdeploypipeline'" in terraform
    assert "resource.id = '${oci_devops_deploy_pipeline.governance[0].id}'" in terraform
    assert "where target.cluster.id = '${oci_containerengine_cluster.governance[0].id}'" in terraform
    assert 'network_channel_type = "PRIVATE_ENDPOINT_CHANNEL"' in terraform
    assert 'deploy_artifact_source_type = "INLINE"' in terraform
    assert 'deploy_stage_type  = "OKE_DEPLOYMENT"' in terraform
    assert 'trigger_new_devops_deployment = true' in terraform


def test_gateway_manifest_is_private_tls_and_immutable() -> None:
    manifest = (ROOT / "deploy/governance/gateway.yaml").read_text(encoding="utf-8")
    variables = (ROOT / "terraform/b_variables.tf").read_text(encoding="utf-8")
    assert 'service.beta.kubernetes.io/oci-load-balancer-internal: "true"' in manifest
    assert "governance_gateway.tls_entrypoint" in manifest
    assert "GOVERNANCE_TLS_SECRET_OCID" in manifest
    assert "GOVERNANCE_OIDC_AUTHORITY" in manifest
    assert "GOVERNANCE_TOKENIZATION_KEY_OCID" in manifest
    assert "GOVERNANCE_TOKENIZATION_CRYPTO_ENDPOINT" in manifest
    assert "secretName:" not in manifest
    assert "@sha256:" in variables
    assert "private_key_pem" not in manifest
    assert "BEGIN PRIVATE KEY" not in manifest


def test_workload_identity_reads_only_named_runtime_inputs() -> None:
    gateway = (ROOT / "terraform/i_oci_data_governance_gateway.tf").read_text(encoding="utf-8")
    assert "request.principal.type='workload'" in gateway
    assert "request.principal.namespace='aidp-governance'" in gateway
    assert "request.principal.service_account='ai-data-governance'" in gateway
    assert "target.secret.id='${var.governance_gateway_tls_secret_ocid}'" in gateway
    assert "target.secret.id='${var.governance_gateway_jdbc_secret_ocid}'" in gateway
    assert "target.bucket.name='${var.governance_gateway_jdbc_driver_bucket}'" in gateway
    assert "target.object.name='${var.governance_gateway_jdbc_driver_object}'" in gateway
    assert "target.key.id='${var.governance_gateway_tokenization_key_ocid}'" in gateway
    assert "use keys" in gateway
    assert "manage secret" not in gateway


def test_production_gateway_uses_delta_not_process_memory() -> None:
    api = (ROOT / "apps/governance_gateway/api.py").read_text(encoding="utf-8")
    runtime = (ROOT / "apps/governance_gateway/jdbc.py").read_text(encoding="utf-8")
    assert "JdbcControlStore(runtime.connect" in api
    assert "MemoryControlStore" not in api
    assert "AIDP_GOVERNANCE_GATEWAY" in runtime
    assert "get_oke_workload_identity_resource_principal_signer" in runtime
