# Stable nine-statement AIDP Advanced policy. Optional VNIC, subnet, NSG, and
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

resource "oci_ai_data_platform_ai_data_platform" "lab" {
  compartment_id         = local.target_compartment
  display_name           = local.name_prefix
  default_workspace_name = "aidp-lab-workspace-${local.suffix}"
  ai_data_platform_type  = "STRUCTURED"

  freeform_tags = {
    managed-by = "deploy-studio"
    workload   = "migration-lab"
  }

  timeouts {
    create = "120m"
  }

  depends_on = [oci_identity_policy.aidp_service]
}

data "oci_identity_tenancy" "current" {
  tenancy_id = var.tenancy_ocid
}

locals {
  aidp_web_socket_endpoint = oci_ai_data_platform_ai_data_platform.lab.web_socket_endpoint == null ? "" : oci_ai_data_platform_ai_data_platform.lab.web_socket_endpoint
  aidp_alias_key           = oci_ai_data_platform_ai_data_platform.lab.alias_key == null ? "" : oci_ai_data_platform_ai_data_platform.lab.alias_key
  aidp_region_key          = var.region == "us-chicago-1" ? "ord" : ""
  aidp_alias_endpoint      = local.aidp_alias_key == "" ? "" : "${local.aidp_alias_key}${local.aidp_region_key}"
  aidp_endpoint            = local.aidp_web_socket_endpoint != "" ? local.aidp_web_socket_endpoint : local.aidp_alias_endpoint
  aidp_endpoint_host       = element(split("/", trimprefix(trimprefix(local.aidp_endpoint, "https://"), "wss://")), 0)
  aidp_workbench_host      = local.aidp_endpoint_host == "" ? "" : (endswith(local.aidp_endpoint_host, ".datalake.oci.oraclecloud.com") ? local.aidp_endpoint_host : "${local.aidp_endpoint_host}.datalake.oci.oraclecloud.com")
  aidp_workbench_url       = local.aidp_workbench_host == "" ? "" : "https://${local.aidp_workbench_host}#?tenant=${data.oci_identity_tenancy.current.name}&domain=${local.default_domain.display_name}"
}
