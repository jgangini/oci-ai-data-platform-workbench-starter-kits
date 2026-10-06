locals {
  gods_eye_view_enabled = coalesce(var.enable_gods_eye_view, var.enable_territorial_viewer, var.enable_prisma_viewer)
  # A reserved private address avoids a bootstrap dependency cycle between the two VMs.
  gods_eye_view_admin_private_ip = cidrhost(cidrsubnet(var._oci_vcn.cidr_block, 1, 0), 10)
}

# Physical names below identify existing OCI resources; moved blocks preserve their state.
resource "oci_core_nat_gateway" "gods_eye_view" {
  count          = local.gods_eye_view_enabled ? 1 : 0
  compartment_id = local.target_compartment
  vcn_id         = oci_core_vcn.lab.id
  display_name   = "${local.name_prefix}-prisma-nat"
}

resource "oci_core_route_table" "gods_eye_view" {
  count          = local.gods_eye_view_enabled ? 1 : 0
  compartment_id = local.target_compartment
  vcn_id         = oci_core_vcn.lab.id
  route_rules {
    destination       = "0.0.0.0/0"
    destination_type  = "CIDR_BLOCK"
    network_entity_id = oci_core_nat_gateway.gods_eye_view[0].id
  }
}

resource "oci_core_security_list" "gods_eye_view" {
  count          = local.gods_eye_view_enabled ? 1 : 0
  compartment_id = local.target_compartment
  vcn_id         = oci_core_vcn.lab.id
  display_name   = "${local.name_prefix}-prisma-egress"
  egress_security_rules {
    protocol    = "6"
    destination = "0.0.0.0/0"
    tcp_options {
      min = 443
      max = 443
    }
  }
  egress_security_rules {
    protocol    = "6"
    destination = "0.0.0.0/0"
    tcp_options {
      min = 80
      max = 80
    }
  }
}

resource "oci_core_subnet" "gods_eye_view" {
  count                      = local.gods_eye_view_enabled ? 1 : 0
  compartment_id             = local.target_compartment
  vcn_id                     = oci_core_vcn.lab.id
  cidr_block                 = cidrsubnet(var._oci_vcn.cidr_block, 1, 1)
  display_name               = "${local.name_prefix}-prisma-private"
  prohibit_public_ip_on_vnic = true
  prohibit_internet_ingress  = true
  route_table_id             = oci_core_route_table.gods_eye_view[0].id
  security_list_ids          = [oci_core_security_list.gods_eye_view[0].id]
}

resource "oci_core_network_security_group" "gods_eye_view_proxy" {
  count          = local.gods_eye_view_enabled ? 1 : 0
  compartment_id = local.target_compartment
  vcn_id         = oci_core_vcn.lab.id
  display_name   = "${local.name_prefix}-prisma-proxy"
}

resource "oci_core_network_security_group" "gods_eye_view" {
  count          = local.gods_eye_view_enabled ? 1 : 0
  compartment_id = local.target_compartment
  vcn_id         = oci_core_vcn.lab.id
  display_name   = "${local.name_prefix}-prisma-viewer"
}

resource "oci_core_network_security_group_security_rule" "gods_eye_view" {
  count                     = local.gods_eye_view_enabled ? 1 : 0
  network_security_group_id = oci_core_network_security_group.gods_eye_view[0].id
  direction                 = "INGRESS"
  protocol                  = "6"
  source_type               = "NETWORK_SECURITY_GROUP"
  source                    = oci_core_network_security_group.gods_eye_view_proxy[0].id
  tcp_options {
    destination_port_range {
      min = 8081
      max = 8081
    }
  }
}

resource "oci_core_instance" "gods_eye_view" {
  count               = local.gods_eye_view_enabled ? 1 : 0
  compartment_id      = local.target_compartment
  availability_domain = local.availability_domain
  display_name        = "${local.name_prefix}-prisma-viewer"
  shape               = var.preferred_vm_shape
  shape_config {
    ocpus         = var._oci_instance.shape.ocpus
    memory_in_gbs = var._oci_instance.shape.memory_in_gbs
  }
  create_vnic_details {
    subnet_id        = oci_core_subnet.gods_eye_view[0].id
    assign_public_ip = false
    nsg_ids          = [oci_core_network_security_group.gods_eye_view[0].id]
  }
  source_details {
    source_type = "image"
    source_id   = data.oci_core_images.oracle_linux.images[0].id
  }
  agent_config {
    is_management_disabled = false
    plugins_config {
      name          = "Compute Instance Run Command"
      desired_state = "ENABLED"
    }
  }
  metadata = {
    user_data = base64encode(templatefile("${path.module}/templatefile/gods_eye_view_user_data.sh", {
      source_repo_url         = var.source_repository_url
      source_commit_sha       = var.source_commit_sha
      region                  = var.region
      objectstorage_namespace = var.objectstorage_namespace
      bucket_name             = local.medallion_bucket_names["gold"]
      admin_private_ip        = local.gods_eye_view_admin_private_ip
    }))
  }
  preserve_boot_volume = false
  lifecycle {
    # Runtime releases update the container; never replace a VM to publish God’s Eye View content.
    ignore_changes = [source_details[0].source_id, metadata["user_data"]]
  }
}

resource "oci_core_network_security_group_security_rule" "gods_eye_view_admin" {
  count                     = local.gods_eye_view_enabled ? 1 : 0
  network_security_group_id = oci_core_network_security_group.gods_eye_view_proxy[0].id
  direction                 = "INGRESS"
  protocol                  = "6"
  source_type               = "NETWORK_SECURITY_GROUP"
  source                    = oci_core_network_security_group.gods_eye_view[0].id
  tcp_options {
    destination_port_range {
      min = 8000
      max = 8000
    }
  }
}

resource "oci_core_network_security_group_security_rule" "gods_eye_view_admin_egress" {
  count                     = local.gods_eye_view_enabled ? 1 : 0
  network_security_group_id = oci_core_network_security_group.gods_eye_view[0].id
  direction                 = "EGRESS"
  protocol                  = "6"
  destination_type          = "NETWORK_SECURITY_GROUP"
  destination               = oci_core_network_security_group.gods_eye_view_proxy[0].id
  tcp_options {
    destination_port_range {
      min = 8000
      max = 8000
    }
  }
}

resource "oci_identity_dynamic_group" "gods_eye_view" {
  count          = local.gods_eye_view_enabled ? 1 : 0
  provider       = oci.home
  compartment_id = var.tenancy_ocid
  name           = "${local.name_prefix}-prisma"
  description    = "Read-only published God’s Eye View objects for the private viewer VM"
  matching_rule  = "ALL {instance.id = '${oci_core_instance.gods_eye_view[0].id}'}"
}

resource "oci_identity_policy" "gods_eye_view" {
  count          = local.gods_eye_view_enabled ? 1 : 0
  provider       = oci.home
  compartment_id = var.tenancy_ocid
  name           = "${local.name_prefix}-prisma-read"
  description    = "God’s Eye View viewer reads only published evidence and agent endpoint metadata"
  statements = [
    "Allow dynamic-group ${oci_identity_dynamic_group.gods_eye_view[0].name} to read objects in compartment id ${local.target_compartment} where all {target.bucket.name = '${local.medallion_bucket_names["gold"]}', request.permission = 'OBJECT_READ', any {target.object.name = '04_gold/prisma/*', target.object.name = '.control/prisma/agent.json'}}"
  ]
}

resource "oci_identity_policy" "gods_eye_view_run_command" {
  count          = local.gods_eye_view_enabled ? 1 : 0
  provider       = oci.home
  compartment_id = var.tenancy_ocid
  name           = "${local.name_prefix}-prisma-update"
  description    = "Let the operator deliver pinned viewer updates through native Run Command"
  statements = [
    "Allow group Administrators to manage instance-agent-command-family in compartment id ${local.target_compartment} where target.instance.id = '${oci_core_instance.gods_eye_view[0].id}'",
    "Allow dynamic-group ${oci_identity_dynamic_group.gods_eye_view[0].name} to use instance-agent-command-execution-family in compartment id ${local.target_compartment} where request.instance.id=target.instance.id"
  ]
}

output "prisma_viewer_enabled" {
  value = local.gods_eye_view_enabled
}

output "prisma_viewer_url" {
  value = local.gods_eye_view_enabled ? "https://${data.oci_core_vnic.lab.public_ip_address}/gods-eye-view/" : null
}

output "prisma_viewer_private_url" {
  description = "Server-side viewer upstream; browsers use prisma_viewer_url through VM1 authentication."
  sensitive   = true
  value       = local.gods_eye_view_enabled ? "http://${oci_core_instance.gods_eye_view[0].private_ip}:8081" : null
}

output "prisma_viewer_instance_id" {
  value = local.gods_eye_view_enabled ? oci_core_instance.gods_eye_view[0].id : null
}

output "prisma_viewer_principal_group" {
  value = local.gods_eye_view_enabled ? oci_identity_dynamic_group.gods_eye_view[0].id : null
}

# Existing state addresses move without replacing their OCI resources or data.
moved {
  from = oci_core_nat_gateway.prisma
  to   = oci_core_nat_gateway.territorial
}

moved {
  from = oci_core_route_table.prisma
  to   = oci_core_route_table.territorial
}

moved {
  from = oci_core_security_list.prisma
  to   = oci_core_security_list.territorial
}

moved {
  from = oci_core_subnet.prisma
  to   = oci_core_subnet.territorial
}

moved {
  from = oci_core_network_security_group.prisma_proxy
  to   = oci_core_network_security_group.territorial_proxy
}

moved {
  from = oci_core_network_security_group.prisma
  to   = oci_core_network_security_group.territorial
}

moved {
  from = oci_core_network_security_group_security_rule.prisma
  to   = oci_core_network_security_group_security_rule.territorial
}

moved {
  from = oci_core_instance.prisma
  to   = oci_core_instance.territorial
}

moved {
  from = oci_core_network_security_group_security_rule.prisma_admin
  to   = oci_core_network_security_group_security_rule.territorial_admin
}

moved {
  from = oci_core_network_security_group_security_rule.prisma_admin_egress
  to   = oci_core_network_security_group_security_rule.territorial_admin_egress
}

moved {
  from = oci_identity_dynamic_group.prisma
  to   = oci_identity_dynamic_group.territorial
}

moved {
  from = oci_identity_policy.prisma
  to   = oci_identity_policy.territorial
}

moved {
  from = oci_identity_policy.prisma_run_command
  to   = oci_identity_policy.territorial_run_command
}


moved {
  from = oci_core_nat_gateway.territorial
  to   = oci_core_nat_gateway.gods_eye_view
}

moved {
  from = oci_core_route_table.territorial
  to   = oci_core_route_table.gods_eye_view
}

moved {
  from = oci_core_security_list.territorial
  to   = oci_core_security_list.gods_eye_view
}

moved {
  from = oci_core_subnet.territorial
  to   = oci_core_subnet.gods_eye_view
}

moved {
  from = oci_core_network_security_group.territorial_proxy
  to   = oci_core_network_security_group.gods_eye_view_proxy
}

moved {
  from = oci_core_network_security_group.territorial
  to   = oci_core_network_security_group.gods_eye_view
}

moved {
  from = oci_core_network_security_group_security_rule.territorial
  to   = oci_core_network_security_group_security_rule.gods_eye_view
}

moved {
  from = oci_core_instance.territorial
  to   = oci_core_instance.gods_eye_view
}

moved {
  from = oci_core_network_security_group_security_rule.territorial_admin
  to   = oci_core_network_security_group_security_rule.gods_eye_view_admin
}

moved {
  from = oci_core_network_security_group_security_rule.territorial_admin_egress
  to   = oci_core_network_security_group_security_rule.gods_eye_view_admin_egress
}

moved {
  from = oci_identity_dynamic_group.territorial
  to   = oci_identity_dynamic_group.gods_eye_view
}

moved {
  from = oci_identity_policy.territorial
  to   = oci_identity_policy.gods_eye_view
}

moved {
  from = oci_identity_policy.territorial_run_command
  to   = oci_identity_policy.gods_eye_view_run_command
}

output "gods_eye_view_enabled" {
  value = local.gods_eye_view_enabled
}

output "gods_eye_view_url" {
  value = local.gods_eye_view_enabled ? "https://${data.oci_core_vnic.lab.public_ip_address}/gods-eye-view/" : null
}

output "gods_eye_view_private_url" {
  description = "Server-side viewer upstream; browsers use gods_eye_view_url through VM1 authentication."
  sensitive   = true
  value       = local.gods_eye_view_enabled ? "http://${oci_core_instance.gods_eye_view[0].private_ip}:8081" : null
}

output "gods_eye_view_instance_id" {
  value = local.gods_eye_view_enabled ? oci_core_instance.gods_eye_view[0].id : null
}

output "gods_eye_view_principal_group" {
  value = local.gods_eye_view_enabled ? oci_identity_dynamic_group.gods_eye_view[0].id : null
}

# Legacy output names remain available to previously installed deployment hooks.
output "territorial_viewer_enabled" {
  value = local.gods_eye_view_enabled
}

output "territorial_viewer_url" {
  value = local.gods_eye_view_enabled ? "https://${data.oci_core_vnic.lab.public_ip_address}/gods-eye-view/" : null
}

output "territorial_viewer_private_url" {
  description = "Server-side viewer upstream; browsers use territorial_viewer_url through VM1 authentication."
  sensitive   = true
  value       = local.gods_eye_view_enabled ? "http://${oci_core_instance.gods_eye_view[0].private_ip}:8081" : null
}

output "territorial_viewer_instance_id" {
  value = local.gods_eye_view_enabled ? oci_core_instance.gods_eye_view[0].id : null
}

output "territorial_viewer_principal_group" {
  value = local.gods_eye_view_enabled ? oci_identity_dynamic_group.gods_eye_view[0].id : null
}
