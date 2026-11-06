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
