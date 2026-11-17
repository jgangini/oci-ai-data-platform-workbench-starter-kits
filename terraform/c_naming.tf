resource "random_string" "suffix" {
  length  = 8
  lower   = true
  upper   = false
  numeric = true
  special = false
}

locals {
  suffix             = var.deployment_suffix != "" ? var.deployment_suffix : random_string.suffix.result
  target_compartment = var.compartment_ocid
  name_prefix        = "aidp-lab-${local.suffix}"
  medallion_prefixes = ["01_landing/", "02_bronze/", "03_silver/", "04_gold/"]
}
