# Stable AIDP Advanced policy. Optional VNIC, subnet, NSG, and
# Object Storage service deletion grants are intentionally not part of this policy.
resource "oci_identity_policy" "aidp_service" {
  provider       = oci.home
  compartment_id = var.tenancy_ocid
  name           = "${local.name_prefix}-aidp-service"
  description    = "Minimum AIDP Workbench service access for the lab compartment"
  statements = [
    "Allow any-user to {AUTHENTICATION_INSPECT, DOMAIN_INSPECT, DOMAIN_READ, DYNAMIC_GROUP_INSPECT, GROUP_INSPECT, GROUP_MEMBERSHIP_INSPECT, USER_INSPECT, USER_READ} in tenancy where all {request.principal.type='aidataplatform'}",
    "Allow any-user to manage log-groups in compartment id ${local.target_compartment} where all {request.principal.type='aidataplatform'}",
    "Allow any-user to read log-content in compartment id ${local.target_compartment} where all {request.principal.type='aidataplatform'}",
    "Allow any-user to use metrics in compartment id ${local.target_compartment} where all {request.principal.type='aidataplatform', target.metrics.namespace='oracle_aidataplatform'}",
    "Allow any-user to manage buckets in compartment id ${local.target_compartment} where all {request.principal.type='aidataplatform', any {request.permission='BUCKET_CREATE', request.permission='BUCKET_INSPECT', request.permission='BUCKET_READ', request.permission='BUCKET_UPDATE'}}",
    "Allow any-user to {TAG_NAMESPACE_USE} in tenancy where all {request.principal.type='aidataplatform'}",
    "Allow any-user to manage buckets in compartment id ${local.target_compartment} where all {request.principal.id=target.resource.tag.orcl-aidp.governingAidpId, any {request.permission='BUCKET_DELETE', request.permission='PAR_MANAGE', request.permission='RETENTION_RULE_LOCK', request.permission='RETENTION_RULE_MANAGE'}}",
    "Allow any-user to read objectstorage-namespaces in compartment id ${local.target_compartment} where all {request.principal.type='aidataplatform', request.permission='OBJECTSTORAGE_NAMESPACE_READ'}",
    "Allow any-user to manage objects in compartment id ${local.target_compartment} where all {request.principal.id=target.bucket.system-tag.orcl-aidp.governingAidpId}"
  ]
}

# IAM changes are replicated from the home region. Creating AIDP immediately
# after the policy can fail its internal bucket creation before permissions arrive.
# ponytail: bounded propagation window; replace with a native readiness probe
# when OCI exposes one for the AIDP service principal.
resource "time_sleep" "aidp_iam_propagation" {
  create_duration = "5m"
  triggers = {
    policy_id         = oci_identity_policy.aidp_service.id
    policy_statements = sha256(jsonencode(oci_identity_policy.aidp_service.statements))
  }
}

resource "oci_ai_data_platform_ai_data_platform" "lab" {
  count                  = var.aidp_mode == "new" ? 1 : 0
  compartment_id         = local.target_compartment
  display_name           = var.aidp_display_name
  default_workspace_name = "aidp-lab-workspace-${local.suffix}"
  ai_data_platform_type  = "STRUCTURED"

  freeform_tags = {
    managed-by = "deploy-studio"
    workload   = "migration-lab"
  }

  lifecycle {
    ignore_changes = [freeform_tags]
  }

  timeouts {
    create = "120m"
  }

  depends_on = [time_sleep.aidp_iam_propagation]
}

data "oci_identity_tenancy" "current" {
  tenancy_id = var.tenancy_ocid
}

locals {
  aidp_web_socket_endpoint = local.selected_aidp.web_socket_endpoint == null ? "" : local.selected_aidp.web_socket_endpoint
  aidp_alias_key           = local.selected_aidp.alias_key == null ? "" : local.selected_aidp.alias_key
  aidp_region_key          = var.region == "us-chicago-1" ? "ord" : ""
  aidp_alias_endpoint      = local.aidp_alias_key == "" ? "" : "${local.aidp_alias_key}${local.aidp_region_key}"
  aidp_endpoint            = local.aidp_web_socket_endpoint != "" ? local.aidp_web_socket_endpoint : local.aidp_alias_endpoint
  aidp_endpoint_host       = element(split("/", trimprefix(trimprefix(local.aidp_endpoint, "https://"), "wss://")), 0)
  aidp_workbench_host      = local.aidp_endpoint_host == "" ? "" : (endswith(local.aidp_endpoint_host, ".datalake.oci.oraclecloud.com") ? local.aidp_endpoint_host : "${local.aidp_endpoint_host}.datalake.oci.oraclecloud.com")
  aidp_workbench_url       = local.aidp_workbench_host == "" ? "" : "https://${local.aidp_workbench_host}#?tenant=${data.oci_identity_tenancy.current.name}&domain=${local.default_domain.display_name}"
}

# Existing platforms remain outside this stack's lifecycle.
data "oci_ai_data_platform_ai_data_platform" "existing" {
  count               = var.aidp_mode == "existing" ? 1 : 0
  ai_data_platform_id = var.existing_ai_data_platform_ocid
  lifecycle {
    postcondition {
      condition     = self.state == "ACTIVE" && self.ai_data_platform_type == "STRUCTURED" && self.compartment_id == local.target_compartment
      error_message = "The existing AIDP must be ACTIVE, STRUCTURED and in the selected compartment."
    }
  }
}

locals {
  selected_aidp = var.aidp_mode == "new" ? oci_ai_data_platform_ai_data_platform.lab[0] : data.oci_ai_data_platform_ai_data_platform.existing[0]
}

moved {
  from = oci_ai_data_platform_ai_data_platform.lab
  to   = oci_ai_data_platform_ai_data_platform.lab[0]
}
