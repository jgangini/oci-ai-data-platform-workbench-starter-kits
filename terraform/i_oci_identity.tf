data "oci_identity_domains" "default" {
  provider       = oci.home
  compartment_id = var.tenancy_ocid
  type           = "DEFAULT"
  state          = "ACTIVE"
}

locals {
  default_domain = one(data.oci_identity_domains.default.domains)
}

resource "oci_identity_domains_group" "developers" {
  provider      = oci.home
  idcs_endpoint = local.default_domain.url
  schemas       = ["urn:ietf:params:scim:schemas:core:2.0:Group"]
  display_name  = "aidp-lab-developers-${local.suffix}"
  external_id   = "${local.name_prefix}:developers"
  force_delete  = true

  lifecycle {
    ignore_changes = [schemas]
  }
}

resource "oci_identity_domains_group" "pending" {
  provider      = oci.home
  idcs_endpoint = local.default_domain.url
  schemas       = ["urn:ietf:params:scim:schemas:core:2.0:Group"]
  display_name  = "aidp-lab-pending-${local.suffix}"
  external_id   = "${local.name_prefix}:pending"
  force_delete  = true

  lifecycle {
    ignore_changes = [schemas]
  }
}

resource "oci_identity_domains_group" "gods_eye_view_readers" {
  provider      = oci.home
  idcs_endpoint = local.default_domain.url
  schemas       = ["urn:ietf:params:scim:schemas:core:2.0:Group"]
  display_name  = "aidp-viewer-readers-${local.suffix}"
  external_id   = "${local.name_prefix}:gods_eye_view"
  force_delete  = true
  lifecycle {
    ignore_changes = [schemas]
  }
}

# One public PKCE sign-in application for this portal; no provisioning/API privileges.
resource "oci_identity_domains_app" "viewer" {
  provider      = oci.home
  idcs_endpoint = local.default_domain.url
  schemas       = ["urn:ietf:params:scim:schemas:oracle:idcs:App"]
  display_name  = "Starter Kits viewer ${local.suffix}"
  name          = "aidp_viewer_${local.suffix}"
  based_on_template {
    value = "CustomBrowserMobileTemplateId"
  }
  active          = true
  is_oauth_client = true
  client_type     = "public"
  allowed_grants  = ["authorization_code"]
  redirect_uris   = ["https://${data.oci_core_vnic.lab.public_ip_address}/api/auth/oci/callback"]
  force_delete    = true
  lifecycle {
    ignore_changes = [schemas]
  }
}

resource "oci_identity_policy" "developer_console" {
  provider       = oci.home
  compartment_id = var.tenancy_ocid
  name           = "${local.name_prefix}-developer-console"
  description    = "Allow registered lab developers to open AIDP and use only the lab data bucket"
  statements = [
    "Allow group Administrators to manage ai-data-platforms in compartment id ${local.target_compartment}",
    "Allow group '${local.default_domain.display_name}'/'${oci_identity_domains_group.developers.display_name}' to use ai-data-platforms in compartment id ${local.target_compartment}",
    "Allow group '${local.default_domain.display_name}'/'${oci_identity_domains_group.developers.display_name}' to read buckets in compartment id ${local.target_compartment} where target.bucket.name = '${oci_objectstorage_bucket.data.name}'",
    "Allow group '${local.default_domain.display_name}'/'${oci_identity_domains_group.developers.display_name}' to manage objects in compartment id ${local.target_compartment} where target.bucket.name = 'aidp-data-${local.suffix}'"
  ]
}
