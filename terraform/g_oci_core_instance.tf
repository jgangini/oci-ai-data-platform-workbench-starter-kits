data "oci_identity_availability_domains" "lab" {
  compartment_id = var.tenancy_ocid
}

locals {
  availability_domain = data.oci_identity_availability_domains.lab.availability_domains[var.availability_domain_index].name
}

resource "terraform_data" "vm_release" {
  input = var.source_commit_sha
}

data "oci_core_images" "oracle_linux" {
  compartment_id           = local.target_compartment
  operating_system         = "Oracle Linux"
  operating_system_version = "9"
  shape                    = var.preferred_vm_shape
  sort_by                  = "TIMECREATED"
  sort_order               = "DESC"
}

resource "oci_identity_tag_namespace" "vm_bootstrap" {
  provider       = oci.home
  compartment_id = var.tenancy_ocid
  name           = "ctn_aidp_lab_${local.suffix}"
  description    = "Deployment-scoped tags for the OCI AIDP lab bootstrap"
}

resource "oci_identity_tag" "vm_bootstrap" {
  provider         = oci.home
  tag_namespace_id = oci_identity_tag_namespace.vm_bootstrap.id
  name             = "run_command"
  description      = "Identifies the single VM allowed to consume bootstrap commands"
}

resource "oci_identity_dynamic_group" "vm" {
  provider       = oci.home
  compartment_id = var.tenancy_ocid
  name           = "${local.name_prefix}-vm"
  description    = "Instance principal for the AIDP lab registration VM"
  matching_rule  = "ALL {instance.compartment.id = '${local.target_compartment}', tag.${oci_identity_tag_namespace.vm_bootstrap.name}.${oci_identity_tag.vm_bootstrap.name}.value = '${local.suffix}'}"
}

resource "oci_identity_policy" "vm_bootstrap" {
  provider       = oci.home
  compartment_id = var.tenancy_ocid
  name           = "${local.name_prefix}-vm-bootstrap"
  description    = "Allow the tagged lab VM to receive commands and consume its one-use bootstrap object"
  statements = concat(
    [
      "Allow dynamic-group ${oci_identity_dynamic_group.vm.name} to use instance-agent-command-execution-family in compartment id ${local.target_compartment} where request.instance.id=target.instance.id",
      "Allow dynamic-group ${oci_identity_dynamic_group.vm.name} to manage objects in compartment id ${local.target_compartment} where all {target.bucket.name = '${local.bootstrap_bucket_name}', target.object.name = '.bootstrap/operator-credentials.json'}"
    ],
    var.enable_ai_data_governance ? [
      "Allow dynamic-group ${oci_identity_dynamic_group.vm.name} to manage objects in compartment id ${local.target_compartment} where all {target.bucket.name = '${local.artifacts_bucket_name}', target.object.name = '${local.governance_jdbc_object}'}"
    ] : []
  )
}

resource "oci_core_instance" "lab" {
  compartment_id      = local.target_compartment
  availability_domain = local.availability_domain
  display_name        = "${local.name_prefix}-vm"
  shape               = var.preferred_vm_shape

  defined_tags = {
    "${oci_identity_tag_namespace.vm_bootstrap.name}.${oci_identity_tag.vm_bootstrap.name}" = local.suffix
  }

  shape_config {
    ocpus         = var._oci_instance.shape.ocpus
    memory_in_gbs = var._oci_instance.shape.memory_in_gbs
  }

  create_vnic_details {
    subnet_id        = oci_core_subnet.public.id
    assign_public_ip = true
  }

  source_details {
    source_type = "image"
    source_id   = data.oci_core_images.oracle_linux.images[0].id
  }

  agent_config {
    is_management_disabled = false
    is_monitoring_disabled = false

    plugins_config {
      name          = "Compute Instance Run Command"
      desired_state = "ENABLED"
    }
  }

  metadata = {
    user_data = base64encode(templatefile("${path.module}/templatefile/user_data.sh", {
      admin_username                = var.admin_username
      admin_password_hash           = var.admin_password_hash
      deployment_mode               = var.deployment_mode
      registration_code_hash        = var.registration_code_hash
      identity_domain_url           = local.default_domain.url
      developer_group_id            = oci_identity_domains_group.developers.id
      pending_group_id              = oci_identity_domains_group.pending.id
      operator_user_ocid            = var.operator_user_ocid
      operator_username             = var.operator_username
      tenancy_ocid                  = var.tenancy_ocid
      objectstorage_namespace       = var.objectstorage_namespace
      bucket_name                   = local.bootstrap_bucket_name
      aidp_workbench_url            = local.aidp_workbench_url
      aidp_platform_id              = oci_ai_data_platform_ai_data_platform.lab.id
      aidp_workspace_name           = oci_ai_data_platform_ai_data_platform.lab.default_workspace_name
      aidp_region                   = var.region
      compartment_id                = local.target_compartment
      autonomous_database_id        = local.autonomous_database_id
      agent_model_id                = var.agent_model_id
      enforce_governed_data_access  = var.enable_ai_data_governance
      governance_gateway_url        = try(oci_apigateway_deployment.governance[0].endpoint, "")
      governance_control_bucket     = local.artifacts_bucket_name
      governance_jdbc_driver_object = local.governance_jdbc_object
      lab_marker                    = local.name_prefix
      source_repo_url               = var.source_repository_url
      source_commit_sha             = var.source_commit_sha
    }))
  }

  preserve_boot_volume = false

  depends_on = [
    oci_identity_policy.vm_bootstrap,
    terraform_data.validate_existing_autonomous_database,
    oci_containerengine_node_pool.governance,
  ]

  lifecycle {
    replace_triggered_by = [terraform_data.vm_release]

    ignore_changes = [
      source_details[0].source_id,
    ]

    precondition {
      condition     = var.availability_domain_index >= 0 && var.availability_domain_index < length(data.oci_identity_availability_domains.lab.availability_domains)
      error_message = "availability_domain_index is outside the region's available domains."
    }
  }
}

resource "oci_identity_policy" "vm_run_command" {
  provider       = oci.home
  compartment_id = var.tenancy_ocid
  name           = "${local.name_prefix}-vm-run-command"
  description    = "Allow the deployment operator to run commands only on the lab VM"
  statements = [
    "Allow group Administrators to manage instance-agent-command-family in compartment id ${local.target_compartment} where target.instance.id = '${oci_core_instance.lab.id}'"
  ]
}

data "oci_core_vnic_attachments" "lab" {
  compartment_id = local.target_compartment
  instance_id    = oci_core_instance.lab.id
}

data "oci_core_vnic" "lab" {
  vnic_id = data.oci_core_vnic_attachments.lab.vnic_attachments[0].vnic_id
}
