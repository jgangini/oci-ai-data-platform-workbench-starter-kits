output "application_url" {
  description = "HTTPS registration application; public certificate when public_ip_tls_enabled is true."
  value       = "https://${data.oci_core_vnic.lab.public_ip_address}"
}

output "application_login_url" {
  description = "Administrator login URL included in the deployment access email."
  value       = "https://${data.oci_core_vnic.lab.public_ip_address}/admin/login"
}

output "source_commit_sha" {
  value = var.source_commit_sha
}

output "deployment_suffix" {
  value = local.suffix
}

output "public_ip_tls_enabled" {
  value = var.enable_public_ip_tls
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
  value = local.bootstrap_bucket_name
}

output "medallion_bucket_names" {
  description = "Resolved landing, bronze, silver, and gold Object Storage bucket names."
  value       = local.medallion_bucket_names
}

output "objectstorage_namespace" {
  value = var.objectstorage_namespace
}

output "medallion_prefixes" {
  value = local.medallion_prefixes
}

output "ai_data_platform_id" {
  value = local.selected_aidp.id
}

output "default_workspace_name" {
  value = local.selected_aidp.default_workspace_name
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

output "operator_username" {
  description = "Display name or email of the OCI user that created the deployment."
  value       = var.operator_username
}

output "home_region" {
  description = "Tenancy home region used for Identity Domains operations."
  value       = var.home_region
}

output "aidp_catalog_name" {
  value = "oci_medallion"
}

output "aidp_shared_compute_name" {
  value = "aidp_cluster_shared_compute"
}

output "aidp_external_volume_count" {
  description = "Base Terraform creates no external volumes; the Godâ€™s Eye View post-apply reports its validated Landing volume when enabled."
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

output "ssh_username" {
  description = "Default Oracle Linux account for the emailed VM SSH access key."
  value       = "opc"
}

output "vm_shape" {
  description = "Explicit shape used by this APPLY."
  value       = oci_core_instance.lab.shape
}

output "aidp_mode" {
  value = var.aidp_mode
}

output "existing_workspace_key" {
  value = local.selected_workspace_key
}

output "existing_workspace_name" {
  value = var.aidp_mode == "existing" ? var.existing_workspace_name : ""
}

output "workspace_name" {
  value = local.selected_workspace_name
}

output "deploy_studio_resource_labels" {
  description = "Plan-known display labels keyed by exact Terraform instance address; execution and ordering come from OCI Resource Manager."
  value = merge(
    {
      "random_string.suffix"                               = "Deployment name suffix"
      "terraform_data.validate_medallion_buckets"          = "Validate Medallion bucket selections"
      "oci_objectstorage_bucket.data"                      = "Workbench data bucket"
      "terraform_data.validate_resolved_medallion_buckets" = "Verify Medallion bucket access"
      "oci_core_vcn.lab"                                   = "Virtual cloud network"
      "oci_core_subnet.public"                             = "Application subnet"
      "oci_core_security_list.web"                         = "Application network security"
      "oci_core_internet_gateway.lab"                      = "Internet gateway"
      "oci_core_route_table.public"                        = "Public network routes"
      "data.oci_identity_domains.default"                  = "Identity Domain discovery"
      "oci_identity_domains_group.developers"              = "Workbench developer group"
      "oci_identity_domains_group.pending"                 = "Pending participant group"
      "oci_identity_policy.developer_console"              = "Developer console permissions"
      "oci_identity_policy.aidp_service"                   = "AIDP service permissions"
      "time_sleep.aidp_iam_propagation"                    = "IAM permission propagation"
      "data.oci_identity_tenancy.current"                  = "Tenancy discovery"
      "data.oci_identity_availability_domains.lab"         = "Availability Domain discovery"
      "tls_private_key.instance_ssh"                       = "Application SSH key"
      "terraform_data.vm_release"                          = "Application release binding"
      "data.oci_core_images.oracle_linux"                  = "Oracle Linux image discovery"
      "oci_identity_tag_namespace.vm_bootstrap"            = "Bootstrap tag namespace"
      "oci_identity_tag.vm_bootstrap"                      = "Bootstrap identity tag"
      "oci_identity_dynamic_group.vm"                      = "Application instance identity"
      "oci_identity_policy.vm_bootstrap"                   = "Application bootstrap permissions"
      "oci_core_instance.lab"                              = "Starter Kits application VM"
      "oci_identity_policy.vm_run_command"                 = "Application command permissions"
      "data.oci_core_vnic_attachments.lab"                 = "Application network attachment"
      "data.oci_core_vnic.lab"                             = "Application network interface"
    },
    { for index, instance in oci_ai_data_platform_ai_data_platform.lab : "oci_ai_data_platform_ai_data_platform.lab[${index}]" => "AI Data Platform" },
    { for index, instance in data.oci_ai_data_platform_ai_data_platform.existing : "data.oci_ai_data_platform_ai_data_platform.existing[${index}]" => "Existing AI Data Platform" },
    { for key, instance in oci_objectstorage_bucket.medallion : "oci_objectstorage_bucket.medallion[${jsonencode(key)}]" => "${title(key)} bucket" },
    { for key, instance in data.oci_objectstorage_bucket.medallion : "data.oci_objectstorage_bucket.medallion[${jsonencode(key)}]" => "Existing ${key} bucket" }
  )
}
