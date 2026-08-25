# OCI Provider 8.21 has no force_destroy argument; its native delete refuses a non-empty bucket.
locals {
  medallion_bucket_configuration = {
    landing = {
      mode          = var.landing_bucket_mode
      new_name      = trimspace(var.landing_new_bucket_name)
      existing_name = trimspace(var.landing_existing_bucket_name)
    }
    bronze = {
      mode          = var.bronze_bucket_mode
      new_name      = trimspace(var.bronze_new_bucket_name)
      existing_name = trimspace(var.bronze_existing_bucket_name)
    }
    silver = {
      mode          = var.silver_bucket_mode
      new_name      = trimspace(var.silver_new_bucket_name)
      existing_name = trimspace(var.silver_existing_bucket_name)
    }
    gold = {
      mode          = var.gold_bucket_mode
      new_name      = trimspace(var.gold_new_bucket_name)
      existing_name = trimspace(var.gold_existing_bucket_name)
    }
  }
  new_medallion_buckets = {
    for layer, config in local.medallion_bucket_configuration : layer => config
    if config.mode == "new"
  }
  existing_medallion_buckets = {
    for layer, config in local.medallion_bucket_configuration : layer => config
    if config.mode == "existing"
  }
  selected_medallion_bucket_names = [
    for config in values(local.medallion_bucket_configuration) :
    lower(config.mode == "new" ? config.new_name : config.existing_name)
  ]
}

resource "terraform_data" "validate_medallion_buckets" {
  lifecycle {
    precondition {
      condition = alltrue([
        for config in values(local.medallion_bucket_configuration) :
        can(regex("^[A-Za-z0-9._-]{1,128}$", config.mode == "new" ? config.new_name : config.existing_name))
      ])
      error_message = "Each selected medallion bucket name must contain 1-128 letters, numbers, dots, underscores, or hyphens."
    }

    precondition {
      condition     = length(distinct(local.selected_medallion_bucket_names)) == length(local.selected_medallion_bucket_names)
      error_message = "Landing, bronze, silver, and gold must use four unique Object Storage bucket names."
    }

    precondition {
      condition     = !contains(local.selected_medallion_bucket_names, "oci_artifacts")
      error_message = "The fixed oci_artifacts bucket is reserved for governance artifacts."
    }
  }
}

resource "oci_objectstorage_bucket" "data" {
  compartment_id = local.target_compartment
  namespace      = var.objectstorage_namespace
  name           = "aidp-data-${local.suffix}"
  access_type    = "NoPublicAccess"
  storage_tier   = "Standard"
  versioning     = "Disabled"
  auto_tiering   = "Disabled"

  freeform_tags = {
    managed-by = "deploy-studio"
    data-model = "medallion"
  }
}

resource "oci_objectstorage_bucket" "medallion" {
  for_each       = local.new_medallion_buckets
  compartment_id = local.target_compartment
  namespace      = var.objectstorage_namespace
  name           = each.value.new_name
  access_type    = "NoPublicAccess"
  storage_tier   = "Standard"
  versioning     = "Disabled"
  auto_tiering   = "Disabled"

  freeform_tags = {
    managed-by      = "deploy-studio"
    data-model      = "medallion"
    medallion-layer = each.key
  }

  depends_on = [terraform_data.validate_medallion_buckets]
}

data "oci_objectstorage_bucket" "medallion" {
  for_each  = local.existing_medallion_buckets
  namespace = var.objectstorage_namespace
  name      = each.value.existing_name
}

locals {
  layer_bucket_names = {
    for layer, config in local.medallion_bucket_configuration : layer => (
      config.mode == "new"
      ? oci_objectstorage_bucket.medallion[layer].name
      : data.oci_objectstorage_bucket.medallion[layer].name
    )
  }
  layer_bucket_compartment_ids = {
    for layer, config in local.medallion_bucket_configuration : layer => (
      config.mode == "new"
      ? local.target_compartment
      : data.oci_objectstorage_bucket.medallion[layer].compartment_id
    )
  }
  artifacts_bucket_name = var.artifacts_bucket_mode == "new" ? (
    oci_objectstorage_bucket.artifacts[0].name
    ) : (
    data.oci_objectstorage_bucket.artifacts[0].name
  )
  artifacts_bucket_compartment_id = var.artifacts_bucket_mode == "new" ? (
    local.target_compartment
    ) : (
    data.oci_objectstorage_bucket.artifacts[0].compartment_id
  )
  medallion_bucket_names = merge(local.layer_bucket_names, {
    artifacts = local.artifacts_bucket_name
  })
  medallion_bucket_compartment_ids = merge(local.layer_bucket_compartment_ids, {
    artifacts = local.artifacts_bucket_compartment_id
  })
  bootstrap_bucket_name           = local.medallion_bucket_names.landing
  bootstrap_bucket_compartment_id = local.medallion_bucket_compartment_ids.landing
}

resource "terraform_data" "validate_resolved_medallion_buckets" {
  lifecycle {
    precondition {
      condition = alltrue([
        for compartment_id in values(local.medallion_bucket_compartment_ids) :
        compartment_id == local.target_compartment
      ])
      error_message = "Existing medallion buckets must belong to the selected deployment compartment so least-privilege IAM policies remain exact."
    }
  }
}

resource "oci_objectstorage_bucket" "artifacts" {
  count          = var.artifacts_bucket_mode == "new" ? 1 : 0
  compartment_id = local.target_compartment
  namespace      = var.objectstorage_namespace
  name           = "oci_artifacts"
  access_type    = "NoPublicAccess"
  storage_tier   = "Standard"
  versioning     = "Disabled"
  auto_tiering   = "Disabled"

  freeform_tags = {
    managed-by = "deploy-studio"
    data-model = "medallion-artifacts"
  }

  depends_on = [terraform_data.validate_medallion_buckets]
}

data "oci_objectstorage_bucket" "artifacts" {
  count     = var.artifacts_bucket_mode == "existing" ? 1 : 0
  namespace = var.objectstorage_namespace
  name      = "oci_artifacts"
}

# ponytail: prefixes stay virtual until AIDP's first write; add markers only when OCI exposes write readiness.
