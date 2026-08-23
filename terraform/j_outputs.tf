output "application_url" {
  description = "Self-signed HTTPS registration application."
  value       = "https://${data.oci_core_vnic.lab.public_ip_address}"
}

output "admin_url" {
  description = "Administrator users page."
  value       = "https://${data.oci_core_vnic.lab.public_ip_address}/admin/users"
}

output "aidp_workbench_url" {
  description = "Direct OCI AI Data Platform Workbench URL when OCI exposes the WebSocket endpoint."
  value       = local.aidp_workbench_url
}

output "aidp_web_socket_endpoint" {
  description = "AIDP WebSocket endpoint used to build the direct Workbench URL."
  value       = local.aidp_web_socket_endpoint
}

output "aidp_alias_key" {
  description = "AIDP alias used when OCI does not publish a WebSocket endpoint."
  value       = local.aidp_alias_key
}

output "tenancy_name" {
  value = data.oci_identity_tenancy.current.name
}

output "identity_domain_name" {
  value = local.default_domain.display_name
}

output "compartment_ocid" {
  value = local.target_compartment
}

output "bucket_name" {
  value = oci_objectstorage_bucket.data.name
}

output "objectstorage_namespace" {
  value = var.objectstorage_namespace
}

output "medallion_prefixes" {
  value = local.medallion_prefixes
}

output "ai_data_platform_id" {
  value = oci_ai_data_platform_ai_data_platform.lab.id
}

output "autonomous_database_id" {
  description = "Autonomous AI Database used by participant governance agents."
  value       = local.autonomous_database_id
}

output "autonomous_database_mode" {
  value = var.autonomous_database_mode
}

output "autonomous_database_version" {
  value = local.autonomous_database_version
}

output "autonomous_database_workload" {
  value = local.autonomous_database_workload
}

output "autonomous_database_compute_count" {
  value = var.autonomous_database_mode == "new" ? var.autonomous_database_compute_count : null
}

output "enable_ai_data_governance" {
  value = var.enable_ai_data_governance
}

output "governance_gateway_cluster_id" {
  value = try(oci_containerengine_cluster.governance[0].id, null)
}

output "governance_gateway_private_endpoint" {
  value = try(oci_containerengine_cluster.governance[0].endpoints[0].private_endpoint, null)
}

output "governance_gateway_deploy_pipeline_id" {
  value = try(oci_devops_deploy_pipeline.governance[0].id, null)
}

output "governance_gateway_deployment_id" {
  value = try(oci_devops_deployment.governance[0].id, null)
}

output "governance_gateway_jdbc_user_ocid" {
  value = try(oci_identity_user.governance_jdbc[0].id, null)
}

output "governance_gateway_oidc_authority" {
  value = var.enable_ai_data_governance ? var.governance_gateway_oidc_authority : null
}

output "governance_gateway_oidc_client_id" {
  value = try(oci_identity_domains_app.governance_public_client[0].name, null)
}

output "governance_gateway_oidc_issuer" {
  value = var.enable_ai_data_governance ? var.governance_gateway_oidc_issuer : null
}

output "governance_gateway_oidc_audience" {
  value = var.enable_ai_data_governance ? local.governance_gateway_audience : null
}

output "governance_gateway_url" {
  description = "Public TLS API Gateway endpoint backed only by the private OKE service."
  value       = try(oci_apigateway_deployment.governance[0].endpoint, null)
}

output "governance_gateway_oidc_scopes" {
  value = var.enable_ai_data_governance ? "openid offline_access ${local.governance_gateway_scope}" : null
}

output "governance_gateway_jdbc_secret_ocid" {
  value = try(oci_vault_secret.governance_jdbc[0].id, null)
}

output "governance_gateway_jdbc_driver_bucket" {
  value = try(oci_objectstorage_bucket.control[0].name, null)
}

output "governance_control_bucket" {
  description = "Private oci_artifact Object Storage bucket for the data_governance Delta schema and governance runtime artifacts."
  value       = try(oci_objectstorage_bucket.control[0].name, null)
}

output "governance_control_delta_location" {
  value = var.enable_ai_data_governance ? "oci://${oci_objectstorage_bucket.control[0].name}@${var.objectstorage_namespace}/data_governance" : null
}

output "governance_gateway_jdbc_driver_object" {
  value = var.enable_ai_data_governance ? local.governance_jdbc_object : null
}

output "governance_gateway_tokenization_key_ocid" {
  value = try(oci_kms_key.governance[0].id, null)
}

output "governance_gateway_tokenization_crypto_endpoint" {
  value = try(oci_kms_vault.governance[0].crypto_endpoint, null)
}

output "governance_gateway_api_gateway_id" {
  value = try(oci_apigateway_gateway.governance[0].id, null)
}

output "agent_model_id" {
  value = var.agent_model_id
}

output "default_workspace_name" {
  value = oci_ai_data_platform_ai_data_platform.lab.default_workspace_name
}

output "developer_group_ocid" {
  value = oci_identity_domains_group.developers.ocid
}

output "pending_group_ocid" {
  value = oci_identity_domains_group.pending.ocid
}

output "operator_user_ocid" {
  description = "OCI user OCID supplied by the Deploy Studio config."
  value       = var.operator_user_ocid
}

output "home_region" {
  description = "Tenancy home region used for Identity Domains operations."
  value       = var.home_region
}

output "aidp_catalog_name" {
  value = "aidp_lab"
}

output "aidp_shared_compute_name" {
  value = "aidp_cluster_shared_compute"
}

output "aidp_external_volume_count" {
  description = "Fresh-only v2.1.17 contract: post-apply creates no external volumes."
  value       = 0
}

output "identity_domain_url" {
  value = local.default_domain.url
}

output "instance_id" {
  value = oci_core_instance.lab.id
}

output "public_ip" {
  value = data.oci_core_vnic.lab.public_ip_address
}

output "vm_shape" {
  description = "Explicit shape used by this APPLY."
  value       = oci_core_instance.lab.shape
}
