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
  description = "Fresh-only v2.0.0 contract: post-apply creates no external volumes."
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
