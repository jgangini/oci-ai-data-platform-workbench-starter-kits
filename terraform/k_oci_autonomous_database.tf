locals {
  autonomous_database_name = upper(substr(replace("AIDP${local.suffix}", "-", ""), 0, 14))
}

resource "terraform_data" "validate_autonomous_inputs" {
  lifecycle {
    precondition {
      condition     = var.autonomous_database_mode == "existing" || var.autonomous_database_version == "26ai"
      error_message = "New databases must use Oracle AI Database 26ai."
    }
    precondition {
      condition     = var.autonomous_database_mode == "existing" || var.autonomous_database_workload == "DW"
      error_message = "New databases must use the Data Warehouse workload."
    }
    precondition {
      condition = (
        var.autonomous_database_mode == "existing" ||
        (var.autonomous_database_compute_count == floor(var.autonomous_database_compute_count) &&
        var.autonomous_database_compute_count >= 2 && var.autonomous_database_compute_count <= 512)
      )
      error_message = "autonomous_database_compute_count must be a supported integer between 2 and 512."
    }
    precondition {
      condition = (
        var.autonomous_database_mode == "new" ||
        can(regex("^ocid1\\.autonomousdatabase\\.", var.existing_autonomous_database_ocid))
      )
      error_message = "existing_autonomous_database_ocid is required in existing mode."
    }
  }
}

resource "oci_database_autonomous_database" "agent" {
  count = var.autonomous_database_mode == "new" ? 1 : 0

  compartment_id              = local.target_compartment
  display_name                = "${local.name_prefix}-agent-db"
  db_name                     = local.autonomous_database_name
  admin_password              = var.autonomous_database_admin_password
  db_version                  = "26ai"
  db_workload                 = "DW"
  compute_model               = "ECPU"
  compute_count               = var.autonomous_database_compute_count
  is_auto_scaling_enabled     = false
  data_storage_size_in_tbs    = 1
  license_model               = "LICENSE_INCLUDED"
  is_mtls_connection_required = true

  freeform_tags = {
    managed-by = "deploy-studio"
    workload   = "aidp-agent-governance"
  }
}

data "oci_database_autonomous_database" "existing_agent" {
  count                  = var.autonomous_database_mode == "existing" ? 1 : 0
  autonomous_database_id = var.existing_autonomous_database_ocid
}

locals {
  autonomous_database_id = var.autonomous_database_mode == "new" ? (
    oci_database_autonomous_database.agent[0].id
  ) : data.oci_database_autonomous_database.existing_agent[0].id
  autonomous_database_version = var.autonomous_database_mode == "new" ? "26ai" : (
    data.oci_database_autonomous_database.existing_agent[0].db_version
  )
  autonomous_database_workload = var.autonomous_database_mode == "new" ? "DW" : (
    data.oci_database_autonomous_database.existing_agent[0].db_workload
  )
}

resource "terraform_data" "validate_existing_autonomous_database" {
  count = var.autonomous_database_mode == "existing" ? 1 : 0

  lifecycle {
    precondition {
      condition     = local.autonomous_database_version == "26ai"
      error_message = "The existing Autonomous database must use version 26ai."
    }
    precondition {
      condition     = local.autonomous_database_workload == "DW"
      error_message = "The existing Autonomous database must use the Data Warehouse workload."
    }
  }
}
