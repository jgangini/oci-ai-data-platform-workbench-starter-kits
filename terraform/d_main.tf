provider "oci" {
  alias               = "home"
  region              = var.home_region
  ignore_defined_tags = ["Oracle-Tags.CreatedBy", "Oracle-Tags.CreatedOn"]
}
