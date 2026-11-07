output "deploy_studio_resource_labels" {
  description = "Plan-known display labels keyed by exact Terraform instance address; execution and ordering come from OCI Resource Manager."
  value = merge(
    {
      "random_string.suffix"                               = "Deployment name suffix"
      "terraform_data.validate_medallion_buckets"          = "Validate Medallion bucket selections"
      "oci_objectstorage_bucket.data"                      = "Workbench data bucket"
      "terraform_data.validate_resolved_medallion_buckets" = "Verify Medallion bucket access"
      "oci_core_vcn.lab"                                   = "Virtual cloud network"
      "oci_core_subnet.public"                             = "Application subnet"
      "oci_core_security_list.web"                         = "Application network security"
      "oci_core_internet_gateway.lab"                      = "Internet gateway"
      "oci_core_route_table.public"                        = "Public network routes"
      "data.oci_identity_domains.default"                  = "Identity Domain discovery"
      "oci_identity_domains_group.developers"              = "Workbench developer group"
      "oci_identity_domains_group.pending"                 = "Pending participant group"
      "oci_identity_policy.developer_console"              = "Developer console permissions"
      "oci_identity_policy.aidp_service"                   = "AIDP service permissions"
      "time_sleep.aidp_iam_propagation"                    = "IAM permission propagation"
      "data.oci_identity_tenancy.current"                  = "Tenancy discovery"
      "data.oci_identity_availability_domains.lab"         = "Availability Domain discovery"
      "tls_private_key.instance_ssh"                       = "Application SSH key"
      "terraform_data.vm_release"                          = "Application release binding"
      "data.oci_core_images.oracle_linux"                  = "Oracle Linux image discovery"
      "oci_identity_tag_namespace.vm_bootstrap"            = "Bootstrap tag namespace"
      "oci_identity_tag.vm_bootstrap"                      = "Bootstrap identity tag"
      "oci_identity_dynamic_group.vm"                      = "Application instance identity"
      "oci_identity_policy.vm_bootstrap"                   = "Application bootstrap permissions"
      "oci_core_instance.lab"                              = "Starter Kits application VM"
      "oci_identity_policy.vm_run_command"                 = "Application command permissions"
      "data.oci_core_vnic_attachments.lab"                 = "Application network attachment"
      "data.oci_core_vnic.lab"                             = "Application network interface"
    },
    { for index, instance in oci_ai_data_platform_ai_data_platform.lab : "oci_ai_data_platform_ai_data_platform.lab[${index}]" => "AI Data Platform" },
    { for index, instance in data.oci_ai_data_platform_ai_data_platform.existing : "data.oci_ai_data_platform_ai_data_platform.existing[${index}]" => "Existing AI Data Platform" },
    { for key, instance in oci_objectstorage_bucket.medallion : "oci_objectstorage_bucket.medallion[${jsonencode(key)}]" => "${title(key)} bucket" },
    { for key, instance in data.oci_objectstorage_bucket.medallion : "data.oci_objectstorage_bucket.medallion[${jsonencode(key)}]" => "Existing ${key} bucket" }
  )
}
