locals {
  governance_gateway_manifest = var.enable_ai_data_governance ? templatefile(
    "${path.module}/../deploy/governance/gateway.yaml",
    {
      gateway_image                = var.governance_gateway_image
      oci_region                   = var.region
      oidc_issuer                  = var.governance_gateway_oidc_issuer
      oidc_authority               = var.governance_gateway_oidc_authority
      oidc_audience                = local.governance_gateway_audience
      aidp_platform_id             = oci_ai_data_platform_ai_data_platform.lab.id
      jdbc_secret_ocid             = oci_vault_secret.governance_jdbc[0].id
      jdbc_user_ocid               = oci_identity_user.governance_jdbc[0].id
      jdbc_driver_object           = local.governance_jdbc_object
      jdbc_driver_bucket           = oci_objectstorage_bucket.control[0].name
      object_storage_namespace     = var.objectstorage_namespace
      control_delta_location       = "oci://${oci_objectstorage_bucket.control[0].name}@${var.objectstorage_namespace}/delta"
      tokenization_key_ocid        = oci_kms_key.governance[0].id
      tokenization_crypto_endpoint = oci_kms_vault.governance[0].crypto_endpoint
      gateway_backend_ip           = var._oci_governance.gateway_backend_ip
      gateway_subnet_cidr          = var._oci_governance.gateway_subnet_cidr
    }
  ) : ""
  governance_gateway_manifest_rendered = local.governance_gateway_manifest
  governance_gateway_release           = substr(sha256(local.governance_gateway_manifest_rendered), 0, 12)
}

resource "oci_ons_notification_topic" "governance_deploy" {
  count          = var.enable_ai_data_governance ? 1 : 0
  compartment_id = local.target_compartment
  name           = "${local.name_prefix}-governance-deploy"
  description    = "OCI DevOps notifications for the AI Data Governance Gateway"
}

resource "oci_devops_project" "governance" {
  count          = var.enable_ai_data_governance ? 1 : 0
  compartment_id = local.target_compartment
  name           = "${local.name_prefix}-governance"
  description    = "Deploys the versioned AI Data Governance Gateway manifest to private OKE"

  notification_config {
    topic_id = oci_ons_notification_topic.governance_deploy[0].id
  }
}

resource "oci_devops_deploy_environment" "governance" {
  count                   = var.enable_ai_data_governance ? 1 : 0
  deploy_environment_type = "OKE_CLUSTER"
  project_id              = oci_devops_project.governance[0].id
  cluster_id              = oci_containerengine_cluster.governance[0].id
  display_name            = "${local.name_prefix}-governance-private-oke"
  description             = "Private OKE target for the AI Data Governance Gateway"

  network_channel {
    network_channel_type = "PRIVATE_ENDPOINT_CHANNEL"
    subnet_id            = oci_core_subnet.governance_endpoint[0].id
  }
}

resource "oci_devops_deploy_artifact" "governance_manifest" {
  count                      = var.enable_ai_data_governance ? 1 : 0
  project_id                 = oci_devops_project.governance[0].id
  display_name               = "${local.name_prefix}-governance-${local.governance_gateway_release}"
  description                = "Immutable rendered Kubernetes manifest for the governance gateway"
  deploy_artifact_type       = "KUBERNETES_MANIFEST"
  argument_substitution_mode = "NONE"

  deploy_artifact_source {
    deploy_artifact_source_type = "INLINE"
    base64encoded_content       = base64encode(local.governance_gateway_manifest_rendered)
  }
}

resource "oci_devops_deploy_pipeline" "governance" {
  count        = var.enable_ai_data_governance ? 1 : 0
  project_id   = oci_devops_project.governance[0].id
  display_name = "${local.name_prefix}-governance"
  description  = "Server-side apply of the versioned governance gateway manifest"
}

resource "oci_identity_dynamic_group" "governance_deploy_pipeline" {
  count          = var.enable_ai_data_governance ? 1 : 0
  provider       = oci.home
  compartment_id = var.tenancy_ocid
  name           = "${local.name_prefix}-governance-deploy"
  description    = "Exact OCI DevOps pipeline allowed to apply the governance manifest"
  matching_rule  = "ALL {resource.type = 'devopsdeploypipeline', resource.id = '${oci_devops_deploy_pipeline.governance[0].id}'}"
}

resource "oci_identity_policy" "governance_deploy_pipeline" {
  count          = var.enable_ai_data_governance ? 1 : 0
  provider       = oci.home
  compartment_id = var.tenancy_ocid
  name           = "${local.name_prefix}-governance-deploy"
  description    = "Allow the exact OCI DevOps pipeline to apply only to its governance cluster"
  statements = [
    "Allow dynamic-group ${oci_identity_dynamic_group.governance_deploy_pipeline[0].name} to read all-artifacts in compartment id ${local.target_compartment}",
    "Allow dynamic-group ${oci_identity_dynamic_group.governance_deploy_pipeline[0].name} to manage cluster in compartment id ${local.target_compartment} where target.cluster.id = '${oci_containerengine_cluster.governance[0].id}'",
  ]
}

resource "oci_devops_deploy_stage" "governance" {
  count              = var.enable_ai_data_governance ? 1 : 0
  deploy_pipeline_id = oci_devops_deploy_pipeline.governance[0].id
  deploy_stage_type  = "OKE_DEPLOYMENT"
  display_name       = "${local.name_prefix}-governance-server-side-apply"
  description        = "Apply the governance namespace, workload, internal service, and network policy"
  namespace          = "aidp-governance"

  deploy_stage_predecessor_collection {
    items {
      id = oci_devops_deploy_pipeline.governance[0].id
    }
  }

  oke_cluster_deploy_environment_id       = oci_devops_deploy_environment.governance[0].id
  kubernetes_manifest_deploy_artifact_ids = [oci_devops_deploy_artifact.governance_manifest[0].id]
}

resource "oci_devops_deployment" "governance" {
  count                         = var.enable_ai_data_governance ? 1 : 0
  deploy_pipeline_id            = oci_devops_deploy_pipeline.governance[0].id
  deployment_type               = "PIPELINE_DEPLOYMENT"
  display_name                  = "${local.name_prefix}-governance-${local.governance_gateway_release}"
  trigger_new_devops_deployment = true

  depends_on = [
    oci_containerengine_node_pool.governance,
    oci_identity_policy.governance_deploy_pipeline,
    oci_identity_policy.governance_workload,
    oci_apigateway_deployment.governance,
  ]

  timeouts {
    create = "30m"
    update = "30m"
  }
}
