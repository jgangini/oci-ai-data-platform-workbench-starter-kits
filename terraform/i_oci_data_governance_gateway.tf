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
  node_pool_os_arch              = "AMD64"
  node_pool_os_type              = "Oracle Linux"
  should_list_all_patch_versions = true
}

resource "oci_containerengine_node_pool" "governance" {
  count              = var.enable_ai_data_governance ? 1 : 0
  cluster_id         = oci_containerengine_cluster.governance[0].id
  compartment_id     = local.target_compartment
  kubernetes_version = local.governance_kubernetes_version
  name               = "${local.name_prefix}-governance-workers"
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
    image_id    = data.oci_containerengine_node_pool_option.governance[0].sources[0].image_id
    source_type = data.oci_containerengine_node_pool_option.governance[0].sources[0].source_type
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
    "Allow any-user to read secret-bundles in compartment id ${local.target_compartment} where all {request.principal.type='workload', request.principal.namespace='aidp-governance', request.principal.service_account='ai-data-governance', request.principal.cluster_id='${oci_containerengine_cluster.governance[0].id}', target.secret.id='${var.governance_gateway_jdbc_secret_ocid}'}",
    "Allow any-user to read secret-bundles in compartment id ${local.target_compartment} where all {request.principal.type='workload', request.principal.namespace='aidp-governance', request.principal.service_account='ai-data-governance', request.principal.cluster_id='${oci_containerengine_cluster.governance[0].id}', target.secret.id='${var.governance_gateway_tls_secret_ocid}'}",
    "Allow any-user to read objects in compartment id ${local.target_compartment} where all {request.principal.type='workload', request.principal.namespace='aidp-governance', request.principal.service_account='ai-data-governance', request.principal.cluster_id='${oci_containerengine_cluster.governance[0].id}', target.bucket.name='${var.governance_gateway_jdbc_driver_bucket}', target.object.name='${var.governance_gateway_jdbc_driver_object}'}",
    "Allow any-user to use keys in compartment id ${local.target_compartment} where all {request.principal.type='workload', request.principal.namespace='aidp-governance', request.principal.service_account='ai-data-governance', request.principal.cluster_id='${oci_containerengine_cluster.governance[0].id}', target.key.id='${var.governance_gateway_tokenization_key_ocid}'}",
    "Allow any-user to use ai-data-platforms in compartment id ${local.target_compartment} where all {request.principal.type='workload', request.principal.namespace='aidp-governance', request.principal.service_account='ai-data-governance', request.principal.cluster_id='${oci_containerengine_cluster.governance[0].id}'}"
  ]
}
