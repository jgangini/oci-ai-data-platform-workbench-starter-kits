locals {
  gods_eye_view_enabled       = contains(var.enabled_vm_modules, "gods_eye_view")
  module_network_enabled      = var.portal_managed_modules || local.gods_eye_view_enabled
  gods_eye_view_resource_name = "${local.name_prefix}-gods-eye-view"
  # Reserved endpoints keep VM1 metadata and networking unchanged when a module is installed.
  gods_eye_view_admin_private_ip    = cidrhost(cidrsubnet(var._oci_vcn.cidr_block, 1, 0), 10)
  gods_eye_view_reserved_private_ip = cidrhost(cidrsubnet(var._oci_vcn.cidr_block, 1, 1), 10)
  gods_eye_view_upstream            = "http://${local.gods_eye_view_reserved_private_ip}:8081"
}

resource "oci_core_nat_gateway" "gods_eye_view" {
  count          = local.module_network_enabled ? 1 : 0
  compartment_id = local.target_compartment
  vcn_id         = oci_core_vcn.lab.id
  display_name   = "${local.gods_eye_view_resource_name}-nat"
}

resource "oci_core_route_table" "gods_eye_view" {
  count          = local.module_network_enabled ? 1 : 0
  compartment_id = local.target_compartment
  vcn_id         = oci_core_vcn.lab.id
  route_rules {
    destination       = "0.0.0.0/0"
    destination_type  = "CIDR_BLOCK"
    network_entity_id = oci_core_nat_gateway.gods_eye_view[0].id
  }
}

resource "oci_core_security_list" "gods_eye_view" {
  count          = local.module_network_enabled ? 1 : 0
  compartment_id = local.target_compartment
  vcn_id         = oci_core_vcn.lab.id
  display_name   = "${local.gods_eye_view_resource_name}-egress"
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
  count                      = local.module_network_enabled ? 1 : 0
  compartment_id             = local.target_compartment
  vcn_id                     = oci_core_vcn.lab.id
  cidr_block                 = cidrsubnet(var._oci_vcn.cidr_block, 1, 1)
  display_name               = "${local.gods_eye_view_resource_name}-private"
  prohibit_public_ip_on_vnic = true
  prohibit_internet_ingress  = true
  route_table_id             = oci_core_route_table.gods_eye_view[0].id
  security_list_ids          = [oci_core_security_list.gods_eye_view[0].id]
}

resource "oci_core_network_security_group" "gods_eye_view_proxy" {
  count          = local.module_network_enabled ? 1 : 0
  compartment_id = local.target_compartment
  vcn_id         = oci_core_vcn.lab.id
  display_name   = "${local.gods_eye_view_resource_name}-proxy"
}

resource "oci_core_network_security_group" "gods_eye_view" {
  count          = local.module_network_enabled ? 1 : 0
  compartment_id = local.target_compartment
  vcn_id         = oci_core_vcn.lab.id
  display_name   = "${local.gods_eye_view_resource_name}-viewer"
}

resource "oci_core_network_security_group_security_rule" "gods_eye_view" {
  count                     = local.module_network_enabled ? 1 : 0
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
  display_name        = "${local.gods_eye_view_resource_name}-viewer"
  shape               = var.preferred_vm_shape
  shape_config {
    ocpus         = var._oci_instance.shape.ocpus
    memory_in_gbs = var._oci_instance.shape.memory_in_gbs
  }
  create_vnic_details {
    subnet_id        = oci_core_subnet.gods_eye_view[0].id
    assign_public_ip = false
    private_ip       = local.gods_eye_view_reserved_private_ip
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
  count                     = local.module_network_enabled ? 1 : 0
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
  count                     = local.module_network_enabled ? 1 : 0
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
  name           = local.gods_eye_view_resource_name
  description    = "Read-only published God’s Eye View objects for the private viewer VM"
  matching_rule  = "ALL {instance.id = '${oci_core_instance.gods_eye_view[0].id}'}"
}

resource "oci_identity_policy" "gods_eye_view" {
  count          = local.gods_eye_view_enabled ? 1 : 0
  provider       = oci.home
  compartment_id = var.tenancy_ocid
  name           = "${local.gods_eye_view_resource_name}-read"
  description    = "God’s Eye View viewer reads only published evidence and agent endpoint metadata"
  statements = [
    "Allow dynamic-group ${oci_identity_dynamic_group.gods_eye_view[0].name} to read objects in compartment id ${local.target_compartment} where all {target.bucket.name = '${local.medallion_bucket_names["gold"]}', request.permission = 'OBJECT_READ', any {target.object.name = '04_gold/prisma/*', target.object.name = '.control/prisma/agent.json'}}"
  ]
}

resource "oci_identity_policy" "gods_eye_view_run_command" {
  count          = local.gods_eye_view_enabled ? 1 : 0
  provider       = oci.home
  compartment_id = var.tenancy_ocid
  name           = "${local.gods_eye_view_resource_name}-update"
  description    = "Let the operator deliver pinned viewer updates through native Run Command"
  statements = [
    "Allow group Administrators to manage instance-agent-command-family in compartment id ${local.target_compartment} where target.instance.id = '${oci_core_instance.gods_eye_view[0].id}'",
    "Allow dynamic-group ${oci_identity_dynamic_group.gods_eye_view[0].name} to use instance-agent-command-execution-family in compartment id ${local.target_compartment} where request.instance.id=target.instance.id"
  ]
}

output "gods_eye_view_enabled" {
  value = local.gods_eye_view_enabled
}

output "gods_eye_view_url" {
  description = "VM2 viewer through the authenticated VM1 proxy; both VMs share this stack VCN."
  value       = local.gods_eye_view_enabled ? "https://${data.oci_core_vnic.lab.public_ip_address}/gods-eye-view/" : null
}

output "gods_eye_view_private_url" {
  description = "Server-side viewer upstream; browsers use gods_eye_view_url through VM1 authentication."
  sensitive   = true
  value       = local.gods_eye_view_enabled ? "http://${oci_core_instance.gods_eye_view[0].private_ip}:8081" : null
}

output "gods_eye_view_instance_id" {
  description = "Dedicated private VM2 running the immutable release image built from pinned upstream and verified local patches."
  value       = local.gods_eye_view_enabled ? oci_core_instance.gods_eye_view[0].id : null
}

output "gods_eye_view_principal_group" {
  value = local.gods_eye_view_enabled ? oci_identity_dynamic_group.gods_eye_view[0].id : null
}
