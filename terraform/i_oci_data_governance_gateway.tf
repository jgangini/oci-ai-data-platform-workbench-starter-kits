resource "terraform_data" "validate_governance_inputs" {
  lifecycle {
    precondition {
      condition = !var.enable_ai_data_governance || can(regex(
        "^[a-z0-9./_-]+(?:\\.[a-z0-9.-]+)?/[a-z0-9./_-]+@sha256:[0-9a-f]{64}$",
        var.governance_gateway_image,
      ))
      error_message = "governance_gateway_image must be an immutable image@sha256:digest reference when governance is enabled."
    }
    precondition {
      condition = !var.enable_ai_data_governance || can(regex(
        "^https://[A-Za-z0-9.-]+(?::[0-9]+)?(?:/[A-Za-z0-9._~/-]*)?$",
        var.governance_gateway_oidc_issuer,
      ))
      error_message = "governance_gateway_oidc_issuer must be an HTTPS issuer URL when governance is enabled."
    }
    precondition {
      condition = !var.enable_ai_data_governance || can(regex(
        "^https://[A-Za-z0-9.-]+(?::[0-9]+)?(?:/[A-Za-z0-9._~/-]+)?$",
        var.governance_gateway_oidc_authority,
      ))
      error_message = "governance_gateway_oidc_authority must be an HTTPS Identity Domain URL when governance is enabled."
    }
    precondition {
      condition = !var.enable_ai_data_governance || can(
        length(jsondecode(var.governance_gateway_oidc_static_jwks_json)) >= 1 &&
        length(jsondecode(var.governance_gateway_oidc_static_jwks_json)) <= 10 &&
        alltrue([
          for key in jsondecode(var.governance_gateway_oidc_static_jwks_json) :
          key.alg == "RS256" && key.kty == "RSA" && key.e != "" && key.kid != "" && key.n != ""
        ])
      )
      error_message = "governance_gateway_oidc_static_jwks_json must contain 1-10 RS256 public RSA keys."
    }
  }
}

data "oci_containerengine_cluster_option" "governance" {
  count                          = var.enable_ai_data_governance ? 1 : 0
  cluster_option_id              = "all"
  compartment_id                 = local.target_compartment
  should_list_all_patch_versions = true
}

locals {
  governance_kubernetes_version = var.enable_ai_data_governance ? reverse(sort(
    data.oci_containerengine_cluster_option.governance[0].kubernetes_versions
  ))[0] : ""
}

resource "oci_containerengine_cluster" "governance" {
  count              = var.enable_ai_data_governance ? 1 : 0
  compartment_id     = local.target_compartment
  kubernetes_version = local.governance_kubernetes_version
  name               = "${local.name_prefix}-governance"
  type               = "ENHANCED_CLUSTER"
  vcn_id             = oci_core_vcn.lab.id

  cluster_pod_network_options {
    cni_type = "FLANNEL_OVERLAY"
  }

  endpoint_config {
    is_public_ip_enabled = false
    subnet_id            = oci_core_subnet.governance_endpoint[0].id
  }

  options {
    service_lb_subnet_ids = [oci_core_subnet.governance_service[0].id]

    add_ons {
      is_kubernetes_dashboard_enabled = false
      is_tiller_enabled               = false
    }

    admission_controller_options {
      is_pod_security_policy_enabled = false
    }

    kubernetes_network_config {
      pods_cidr     = "10.244.0.0/16"
      services_cidr = "10.96.0.0/16"
    }
  }

  freeform_tags = {
    managed-by = "deploy-studio"
    workload   = "ai-data-governance"
  }

  timeouts {
    create = "90m"
    update = "90m"
    delete = "90m"
  }
}

data "oci_containerengine_node_pool_option" "governance" {
  count                          = var.enable_ai_data_governance ? 1 : 0
  node_pool_option_id            = "all"
  compartment_id                 = local.target_compartment
  node_pool_k8s_version          = local.governance_kubernetes_version
  node_pool_os_arch              = "X86_64"
  node_pool_os_type              = "OL8"
  should_list_all_patch_versions = true
}

locals {
  governance_node_sources = var.enable_ai_data_governance ? [
    for source in data.oci_containerengine_node_pool_option.governance[0].sources : source
    if length(regexall("-GPU-", source.source_name)) == 0
  ] : []
}

resource "oci_containerengine_node_pool" "governance" {
  count              = var.enable_ai_data_governance ? 1 : 0
  cluster_id         = oci_containerengine_cluster.governance[0].id
  compartment_id     = local.target_compartment
  kubernetes_version = local.governance_kubernetes_version
  name               = "${local.name_prefix}-gov-workers"
  node_shape         = var._oci_governance.node_shape

  node_config_details {
    size = var._oci_governance.node_count

    placement_configs {
      availability_domain = local.availability_domain
      subnet_id           = oci_core_subnet.governance_workers[0].id
    }
  }

  node_shape_config {
    ocpus         = var._oci_governance.node_ocpus
    memory_in_gbs = var._oci_governance.node_memory_in_gbs
  }

  node_source_details {
    image_id    = local.governance_node_sources[0].image_id
    source_type = local.governance_node_sources[0].source_type
  }

  initial_node_labels {
    key   = "workload"
    value = "ai-data-governance"
  }

  freeform_tags = {
    managed-by = "deploy-studio"
    workload   = "ai-data-governance"
  }

  timeouts {
    create = "90m"
    update = "90m"
    delete = "90m"
  }
}

resource "oci_identity_policy" "governance_workload" {
  count          = var.enable_ai_data_governance ? 1 : 0
  provider       = oci.home
  compartment_id = var.tenancy_ocid
  name           = "${local.name_prefix}-governance-workload"
  description    = "Minimum OCI access for the OKE data-governance workload identity"
  statements = [
    "Allow any-user to read secret-bundles in compartment id ${local.target_compartment} where all {request.principal.type='workload', request.principal.namespace='aidp-governance', request.principal.service_account='ai-data-governance', request.principal.cluster_id='${oci_containerengine_cluster.governance[0].id}', target.secret.id='${oci_vault_secret.governance_jdbc[0].id}'}",
    "Allow any-user to manage buckets in compartment id ${local.target_compartment} where all {request.principal.type='workload', request.principal.namespace='aidp-governance', request.principal.service_account='ai-data-governance', request.principal.cluster_id='${oci_containerengine_cluster.governance[0].id}', target.bucket.name='${oci_objectstorage_bucket.control[0].name}', request.permission='PAR_MANAGE'}",
    "Allow any-user to manage objects in compartment id ${local.target_compartment} where all {request.principal.type='workload', request.principal.namespace='aidp-governance', request.principal.service_account='ai-data-governance', request.principal.cluster_id='${oci_containerengine_cluster.governance[0].id}', target.bucket.name='${oci_objectstorage_bucket.control[0].name}', target.object.name='${local.governance_jdbc_object}'}",
    "Allow any-user to use keys in compartment id ${local.target_compartment} where all {request.principal.type='workload', request.principal.namespace='aidp-governance', request.principal.service_account='ai-data-governance', request.principal.cluster_id='${oci_containerengine_cluster.governance[0].id}', target.key.id='${oci_kms_key.governance[0].id}'}",
    "Allow any-user to use ai-data-platforms in compartment id ${local.target_compartment} where all {request.principal.type='workload', request.principal.namespace='aidp-governance', request.principal.service_account='ai-data-governance', request.principal.cluster_id='${oci_containerengine_cluster.governance[0].id}'}"
  ]
}
