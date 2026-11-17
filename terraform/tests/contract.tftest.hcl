mock_provider "oci" {}
mock_provider "time" {}
mock_provider "tls" {}

override_resource {
  target          = tls_private_key.instance_ssh
  override_during = plan
  values = {
    public_key_openssh = "ssh-rsa deployment-public-key-fixture"
  }
}

mock_provider "oci" {
  alias = "home"
}
mock_provider "random" {}

variables {
  operator_username = "test.operator@example.test"
}

override_resource {
  target = random_string.suffix
  values = { result = "test1234" }
}

override_resource {
  target          = oci_ai_data_platform_ai_data_platform.lab[0]
  override_during = plan
  values = {
    alias_key           = "testalias"
    web_socket_endpoint = null
  }
}

override_data {
  target = data.oci_identity_domains.default
  values = {
    domains = [{
      id           = "ocid1.domain.test"
      display_name = "Default"
      url          = "https://identity.example.test"
      state        = "ACTIVE"
    }]
  }
}

override_data {
  target = data.oci_identity_availability_domains.lab
  values = { availability_domains = [{ name = "AD-1" }] }
}

override_data {
  target = data.oci_identity_tenancy.current
  values = { name = "oci-deploy-1" }
}

override_data {
  target = data.oci_core_images.oracle_linux
  values = { images = [{ id = "ocid1.image.test" }] }
}

override_data {
  target = data.oci_core_vnic_attachments.lab
  values = { vnic_attachments = [{ vnic_id = "ocid1.vnic.test" }] }
}

override_data {
  target = data.oci_core_vnic.lab
  values = { public_ip_address = "192.0.2.10" }
}

run "ssh_authorized_source" {
  command = plan
  variables {
    tenancy_ocid            = "ocid1.tenancy.oc1..test"
    home_region             = "us-ashburn-1"
    operator_user_ocid      = "ocid1.user.oc1..operator"
    compartment_ocid        = "ocid1.compartment.oc1..test"
    objectstorage_namespace = "testnamespace"
    deployment_suffix       = "test1234"
    admin_password_hash     = "pbkdf2_sha256$600000$salt$digest"
    registration_code_hash  = "pbkdf2_sha256$600000$salt$digest"
    source_commit_sha       = "0123456789abcdef0123456789abcdef01234567"
    ssh_allowed_cidr        = "203.0.113.10/32"
  }
  assert {
    condition = length([
      for rule in oci_core_security_list.web.ingress_security_rules : rule
      if try(rule.tcp_options[0].min, 0) == 22 && rule.source == "203.0.113.10/32"
    ]) == 1
    error_message = "SSH must use only the configured source CIDR."
  }
}

run "ssh_reject_worldwide_source" {
  command = plan
  variables {
    tenancy_ocid            = "ocid1.tenancy.oc1..test"
    home_region             = "us-ashburn-1"
    operator_user_ocid      = "ocid1.user.oc1..operator"
    compartment_ocid        = "ocid1.compartment.oc1..test"
    objectstorage_namespace = "testnamespace"
    deployment_suffix       = "test1234"
    admin_password_hash     = "pbkdf2_sha256$600000$salt$digest"
    registration_code_hash  = "pbkdf2_sha256$600000$salt$digest"
    source_commit_sha       = "0123456789abcdef0123456789abcdef01234567"
    ssh_allowed_cidr        = "0.0.0.0/0"
  }
  expect_failures = [var.ssh_allowed_cidr]
}

run "resolved_compartment_contract" {
  command = plan

  assert {
    condition     = oci_core_instance.lab.metadata["ssh_authorized_keys"] == "ssh-rsa deployment-public-key-fixture" && output.ssh_username == "opc"
    error_message = "The emailed SSH key must correspond to the public key installed for opc."
  }

  assert {
    condition     = length([for rule in oci_core_security_list.web.ingress_security_rules : rule if try(rule.tcp_options[0].min, 0) == 22]) == 0
    error_message = "Public SSH must stay closed until an authorized source CIDR is configured."
  }

  assert {
    condition     = oci_core_subnet.public.cidr_block == var._oci_vcn.cidr_block
    error_message = "Existing labs must keep their one-VM network when God’s Eye View is disabled."
  }

  variables {
    enable_public_ip_tls    = false
    tenancy_ocid            = "ocid1.tenancy.oc1..test"
    home_region             = "us-ashburn-1"
    operator_user_ocid      = "ocid1.user.oc1..operator"
    compartment_ocid        = "ocid1.compartment.oc1..test"
    objectstorage_namespace = "testnamespace"
    deployment_suffix       = "test1234"
    admin_password_hash     = "pbkdf2_sha256$600000$salt$digest"
    registration_code_hash  = "pbkdf2_sha256$600000$salt$digest"
    source_commit_sha       = "0123456789abcdef0123456789abcdef01234567"
  }

  assert {
    condition     = oci_objectstorage_bucket.data.name == "aidp-data-test1234"
    error_message = "The single data bucket must use the canonical name."
  }

  assert {
    condition     = toset(keys(output.medallion_bucket_names)) == toset(["landing", "bronze", "silver", "gold"]) && length(oci_objectstorage_bucket.medallion) == 4
    error_message = "Kits must expose only the four Medallion layers."
  }

  assert {
    condition     = oci_ai_data_platform_ai_data_platform.lab[0].default_workspace_name == "aidp-lab-workspace-test1234"
    error_message = "The AIDP default workspace must be deterministic."
  }

  assert {
    condition     = local.aidp_web_socket_endpoint == ""
    error_message = "A null AIDP WebSocket endpoint must not fail the apply."
  }

  assert {
    condition     = local.aidp_workbench_url == "https://testaliasord.datalake.oci.oraclecloud.com#?tenant=oci-deploy-1&domain=Default"
    error_message = "The AIDP alias must build the direct Workbench URL when the WebSocket endpoint is null."
  }

  assert {
    condition     = local.medallion_prefixes == ["01_landing/", "02_bronze/", "03_silver/", "04_gold/"]
    error_message = "The four logical medallion prefixes must remain stable."
  }

  assert {
    condition = anytrue([
      for statement in oci_identity_policy.developer_console.statements :
      strcontains(statement, "manage objects") && strcontains(statement, "target.bucket.name = 'aidp-data-test1234'")
    ])
    error_message = "Developer object access must be limited to the single lab bucket."
  }

  assert {
    condition = anytrue([
      for statement in oci_identity_policy.developer_console.statements :
      strcontains(statement, "read buckets") && strcontains(statement, "target.bucket.name = 'aidp-data-test1234'")
    ])
    error_message = "Developer bucket metadata access must be limited to the single lab bucket."
  }

  assert {
    condition = anytrue([
      for statement in oci_identity_policy.developer_console.statements :
      strcontains(statement, "Allow group Administrators to manage ai-data-platforms in compartment id ocid1.compartment.oc1..test")
    ])
    error_message = "The deployment operator needs scoped AIDP administration for catalog reconciliation."
  }

  assert {
    condition = anytrue([
      for statement in oci_identity_policy.aidp_service.statements :
      strcontains(statement, "target.bucket.system-tag.orcl-aidp.governingAidpId")
      ]) && anytrue([
      for statement in oci_identity_policy.aidp_service.statements :
      strcontains(statement, "TAG_NAMESPACE_USE")
    ])
    error_message = "AIDP service access must use the official governing tag conditions."
  }

  assert {
    condition = anytrue([
      for statement in oci_identity_policy.aidp_service.statements :
      strcontains(statement, "read objectstorage-namespaces in compartment id ocid1.compartment.oc1..test")
    ])
    error_message = "AIDP namespace inspection must remain scoped to the target compartment."
  }

  assert {
    condition = alltrue([
      for statement in oci_identity_policy.aidp_service.statements :
      !strcontains(statement, "manage vnics") &&
      !strcontains(statement, "use subnets") &&
      !strcontains(statement, "use network-security-groups") &&
      !strcontains(statement, "Allow service objectstorage-") &&
      !strcontains(statement, "to manage object-family")
    ])
    error_message = "Optional private-network and Object Storage deletion permissions must not be part of the required AIDP policy."
  }

  assert {
    condition     = length(oci_identity_policy.aidp_service.statements) == 9
    error_message = "The required AIDP policy must remain complete without unused Generative AI access."
  }

  assert {
    condition = anytrue([
      for statement in oci_identity_policy.vm_bootstrap.statements :
      strcontains(statement, "Allow dynamic-group aidp-lab-test1234-vm to use instance-agent-command-execution-family") &&
      strcontains(statement, "use instance-agent-command-execution-family") &&
      strcontains(statement, "compartment id ocid1.compartment.oc1..test") &&
      strcontains(statement, "request.instance.id=target.instance.id")
    ])
    error_message = "VM Run Command must be restricted to the lab VM in its compartment."
  }

  assert {
    condition = length(oci_identity_policy.vm_bootstrap.statements) == 2 && anytrue([
      for statement in oci_identity_policy.vm_bootstrap.statements :
      strcontains(statement, "manage objects") &&
      strcontains(statement, "target.bucket.name = 'oci_landing'") &&
      strcontains(statement, "target.object.name = '.bootstrap/operator-credentials.json'")
    ])
    error_message = "The registration VM needs exact-object access to consume encrypted credentials."
  }

  assert {
    condition = (
      oci_identity_dynamic_group.vm.matching_rule ==
      "ALL {instance.compartment.id = 'ocid1.compartment.oc1..test', tag.ctn_aidp_lab_test1234.run_command.value = 'test1234'}"
    )
    error_message = "The VM dynamic group must exist before launch and match only the deployment-scoped defined tag."
  }

  assert {
    condition = (
      oci_core_instance.lab.defined_tags["ctn_aidp_lab_test1234.run_command"] == "test1234" &&
      length(oci_identity_policy.vm_run_command.statements) == 1
    )
    error_message = "The tagged VM must retain a separate operator policy scoped to its concrete instance OCID."
  }

  assert {
    condition     = oci_core_instance.lab.shape == "VM.Standard.E5.Flex"
    error_message = "The APPLY must use the explicitly requested shape without pretending to fall back."
  }

  assert {
    condition     = output.operator_user_ocid == "ocid1.user.oc1..operator"
    error_message = "The deployment contract must preserve the Deploy Studio operator OCID."
  }
}

run "explicit_e4_retry_contract" {
  command = plan

  variables {
    tenancy_ocid            = "ocid1.tenancy.oc1..test"
    home_region             = "us-ashburn-1"
    operator_user_ocid      = "ocid1.user.oc1..operator"
    compartment_ocid        = "ocid1.compartment.oc1..test"
    objectstorage_namespace = "testnamespace"
    deployment_suffix       = "test1234"
    admin_password_hash     = "pbkdf2_sha256$600000$salt$digest"
    registration_code_hash  = "pbkdf2_sha256$600000$salt$digest"
    source_commit_sha       = "0123456789abcdef0123456789abcdef01234567"
    preferred_vm_shape      = "VM.Standard.E4.Flex"
  }

  assert {
    condition     = oci_core_instance.lab.shape == "VM.Standard.E4.Flex"
    error_message = "A retry can switch to E4 using only a non-secret Terraform variable."
  }
}

run "rejects_duplicate_new_and_existing_medallion_bucket_names" {
  command = plan

  override_data {
    target = data.oci_objectstorage_bucket.medallion["bronze"]
    values = {
      name           = "shared-medallion"
      compartment_id = "ocid1.compartment.oc1..test"
    }
  }

  variables {
    tenancy_ocid                = "ocid1.tenancy.oc1..test"
    home_region                 = "us-ashburn-1"
    operator_user_ocid          = "ocid1.user.oc1..operator"
    compartment_ocid            = "ocid1.compartment.oc1..test"
    objectstorage_namespace     = "testnamespace"
    deployment_suffix           = "test1234"
    admin_password_hash         = "pbkdf2_sha256$600000$salt$digest"
    registration_code_hash      = "pbkdf2_sha256$600000$salt$digest"
    source_commit_sha           = "0123456789abcdef0123456789abcdef01234567"
    landing_new_bucket_name     = "shared-medallion"
    bronze_bucket_mode          = "existing"
    bronze_existing_bucket_name = "shared-medallion"
  }

  expect_failures = [terraform_data.validate_medallion_buckets]
}

run "rejects_existing_medallion_bucket_from_another_compartment" {
  command = plan

  override_data {
    target = data.oci_objectstorage_bucket.medallion["gold"]
    values = {
      name           = "gold-other-lab"
      compartment_id = "ocid1.compartment.oc1..other"
    }
  }
  variables {
    tenancy_ocid              = "ocid1.tenancy.oc1..test"
    home_region               = "us-ashburn-1"
    operator_user_ocid        = "ocid1.user.oc1..operator"
    compartment_ocid          = "ocid1.compartment.oc1..test"
    objectstorage_namespace   = "testnamespace"
    deployment_suffix         = "test1234"
    admin_password_hash       = "pbkdf2_sha256$600000$salt$digest"
    registration_code_hash    = "pbkdf2_sha256$600000$salt$digest"
    source_commit_sha         = "0123456789abcdef0123456789abcdef01234567"
    gold_bucket_mode          = "existing"
    gold_existing_bucket_name = "gold-other-lab"
  }

  expect_failures = [terraform_data.validate_resolved_medallion_buckets]
}

run "authorized_e3_fallback_contract" {
  command = plan

  variables {
    tenancy_ocid            = "ocid1.tenancy.oc1..test"
    home_region             = "us-ashburn-1"
    operator_user_ocid      = "ocid1.user.oc1..operator"
    compartment_ocid        = "ocid1.compartment.oc1..test"
    objectstorage_namespace = "testnamespace"
    deployment_suffix       = "test1234"
    admin_password_hash     = "pbkdf2_sha256$600000$salt$digest"
    registration_code_hash  = "pbkdf2_sha256$600000$salt$digest"
    source_commit_sha       = "0123456789abcdef0123456789abcdef01234567"
    preferred_vm_shape      = "VM.Standard.E3.Flex"
  }

  assert {
    condition     = oci_core_instance.lab.shape == "VM.Standard.E3.Flex"
    error_message = "The authorized fallback can use E3 with the same flexible configuration."
  }
}

run "registration_code_is_required" {
  command = plan

  variables {
    tenancy_ocid            = "ocid1.tenancy.oc1..test"
    home_region             = "us-ashburn-1"
    operator_user_ocid      = "ocid1.user.oc1..operator"
    compartment_ocid        = "ocid1.compartment.oc1..test"
    objectstorage_namespace = "testnamespace"
    deployment_suffix       = "test1234"
    admin_password_hash     = "pbkdf2_sha256$600000$salt$digest"
    registration_code_hash  = ""
    source_commit_sha       = "0123456789abcdef0123456789abcdef01234567"
  }

  expect_failures = [var.registration_code_hash]
}

run "existing_aidp_is_referenced_without_owning_it" {

  command = plan
  variables {
    tenancy_ocid                   = "ocid1.tenancy.oc1..test"
    home_region                    = "us-ashburn-1"
    operator_user_ocid             = "ocid1.user.oc1..operator"
    compartment_ocid               = "ocid1.compartment.oc1..test"
    objectstorage_namespace        = "testnamespace"
    deployment_suffix              = "test1234"
    admin_password_hash            = "pbkdf2_sha256$600000$salt$digest"
    registration_code_hash         = "pbkdf2_sha256$600000$salt$digest"
    source_commit_sha              = "0123456789abcdef0123456789abcdef01234567"
    existing_workspace_key         = "chosen-workspace-key"
    existing_workspace_name        = "chosen-workspace"
    aidp_mode                      = "existing"
    existing_ai_data_platform_ocid = "ocid1.aidataplatform.oc1.us-chicago-1.existing"
  }

  override_data {
    target = data.oci_ai_data_platform_ai_data_platform.existing[0]
    values = {
      id                     = "ocid1.aidataplatform.oc1.us-chicago-1.existing"
      state                  = "ACTIVE"
      ai_data_platform_type  = "STRUCTURED"
      compartment_id         = "ocid1.compartment.oc1..test"
      default_workspace_name = "shared-workspace"
      web_socket_endpoint    = "shared.datalake.oci.oraclecloud.com"
      alias_key              = "shared"
    }
  }

  assert {
    condition     = length(oci_ai_data_platform_ai_data_platform.lab) == 0 && output.ai_data_platform_id == "ocid1.aidataplatform.oc1.us-chicago-1.existing"
    error_message = "Existing mode must not create or own the selected platform."
  }
  assert {
    condition     = output.workspace_name == "chosen-workspace" && output.existing_workspace_key == "chosen-workspace-key"
    error_message = "The explicitly selected existing workspace must be propagated."
  }
}

run "existing_aidp_rejects_wrong_compartment" {

  command = plan
  variables {
    tenancy_ocid                   = "ocid1.tenancy.oc1..test"
    home_region                    = "us-ashburn-1"
    operator_user_ocid             = "ocid1.user.oc1..operator"
    compartment_ocid               = "ocid1.compartment.oc1..test"
    objectstorage_namespace        = "testnamespace"
    deployment_suffix              = "test1234"
    admin_password_hash            = "pbkdf2_sha256$600000$salt$digest"
    registration_code_hash         = "pbkdf2_sha256$600000$salt$digest"
    source_commit_sha              = "0123456789abcdef0123456789abcdef01234567"
    existing_workspace_key         = "chosen-workspace-key"
    existing_workspace_name        = "chosen-workspace"
    aidp_mode                      = "existing"
    existing_ai_data_platform_ocid = "ocid1.aidataplatform.oc1.us-chicago-1.existing"
  }

  override_data {
    target = data.oci_ai_data_platform_ai_data_platform.existing[0]
    values = {
      id                     = "ocid1.aidataplatform.oc1.us-chicago-1.existing"
      state                  = "ACTIVE"
      ai_data_platform_type  = "STRUCTURED"
      compartment_id         = "ocid1.compartment.oc1..other"
      default_workspace_name = "shared-workspace"
      web_socket_endpoint    = "shared.datalake.oci.oraclecloud.com"
      alias_key              = "shared"
    }
  }
  expect_failures = [data.oci_ai_data_platform_ai_data_platform.existing[0]]
}

run "existing_aidp_requires_an_identifier" {

  command = plan
  variables {
    tenancy_ocid                   = "ocid1.tenancy.oc1..test"
    home_region                    = "us-ashburn-1"
    operator_user_ocid             = "ocid1.user.oc1..operator"
    compartment_ocid               = "ocid1.compartment.oc1..test"
    objectstorage_namespace        = "testnamespace"
    deployment_suffix              = "test1234"
    admin_password_hash            = "pbkdf2_sha256$600000$salt$digest"
    registration_code_hash         = "pbkdf2_sha256$600000$salt$digest"
    source_commit_sha              = "0123456789abcdef0123456789abcdef01234567"
    existing_workspace_key         = "chosen-workspace-key"
    existing_workspace_name        = "chosen-workspace"
    aidp_mode                      = "existing"
    existing_ai_data_platform_ocid = ""
  }

  override_data {
    target = data.oci_ai_data_platform_ai_data_platform.existing[0]
    values = {
      id                     = "ocid1.aidataplatform.oc1.us-chicago-1.existing"
      state                  = "ACTIVE"
      ai_data_platform_type  = "STRUCTURED"
      compartment_id         = "ocid1.compartment.oc1..other"
      default_workspace_name = "shared-workspace"
      web_socket_endpoint    = "shared.datalake.oci.oraclecloud.com"
      alias_key              = "shared"
    }
  }
  expect_failures = [data.oci_ai_data_platform_ai_data_platform.existing[0]]
}
