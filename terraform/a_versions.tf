terraform {
  required_version = ">= 1.5.7"

  required_providers {
    oci = {
      source  = "oracle/oci"
      version = "= 8.28.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.7"
    }
    time = {
      source  = "hashicorp/time"
      version = "~> 0.13"
    }
  }
}

provider "oci" {
  region              = var.region
  ignore_defined_tags = ["Oracle-Tags.CreatedBy", "Oracle-Tags.CreatedOn"]
}
