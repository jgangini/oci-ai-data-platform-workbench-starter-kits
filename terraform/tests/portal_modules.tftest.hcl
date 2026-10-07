# Mock state transition: installing a module must leave the central portal untouched.
mock_provider "time" {}

mock_provider "oci" {
  mock_resource "oci_database_autonomous_database" {
    defaults = { connection_urls = [{ sql_dev_web_url = "https://database.example.test/ords/sql-developer" }] }
  }
  mock_resource "oci_ai_data_platform_ai_data_platform" {
    defaults = { alias_key = "testalias", web_socket_endpoint = "" }
  }
}
mock_provider "oci" { alias = "home" }
mock_provider "random" {}

override_data {
  target = data.oci_identity_domains.default
  values = { domains = [{ id = "ocid1.domain.test", display_name = "Default", url = "https://identity.example.test", state = "ACTIVE" }] }
}
override_data {
  target = data.oci_identity_availability_domains.lab
  values = { availability_domains = [{ name = "AD-1" }] }
}
override_data {
  target = data.oci_identity_tenancy.current
  values = { name = "test-tenancy" }
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

variables {
  tenancy_ocid                        = "ocid1.tenancy.oc1..test"
  home_region                         = "us-ashburn-1"
  operator_user_ocid                  = "ocid1.user.oc1..operator"
  compartment_ocid                    = "ocid1.compartment.oc1..test"
  objectstorage_namespace             = "testnamespace"
  deployment_suffix                   = "test1234"
  admin_password_hash                 = "pbkdf2_sha256$600000$salt$digest"
  registration_code_hash              = "pbkdf2_sha256$600000$salt$digest"
  source_commit_sha                   = "0123456789abcdef0123456789abcdef01234567"
  agent_model_id                      = "ocid1.generativeaimodel.oc1..test"
  autonomous_database_admin_password  = "TestRootPassword123"
  autonomous_database_wallet_password = "TestWalletPassword123"
  operator_username                   = "test.operator@example.test"
}

run "base_portal" {
  command = apply
  assert {
    condition     = output.application_login_url == "https://192.0.2.10/admin/login" && output.database_actions_url == "https://database.example.test/ords/sql-developer"
    error_message = "The deployment email must use the portal login and the native OCI Database Actions URL."
  }
  assert {
    condition     = output.portal_managed_modules && output.public_ip_tls_enabled && length(oci_core_instance.gods_eye_view) == 0 && length(oci_core_nat_gateway.gods_eye_view) == 1
    error_message = "A new deployment must prepare shared infrastructure and trusted HTTPS without installing the module VM."
  }
  assert {
    condition     = strcontains(base64decode(oci_core_instance.lab.metadata.user_data), "GODS_EYE_VIEW_URL=http://10.10.0.138:8081") && strcontains(base64decode(oci_core_instance.lab.metadata.user_data), "PORTAL_MANAGED_MODULES=true")
    error_message = "The central portal must already know its stable private module endpoint and installation capability."
  }
  assert {
    condition = [
      oci_core_nat_gateway.gods_eye_view[0].display_name,
      oci_core_security_list.gods_eye_view[0].display_name,
      oci_core_subnet.gods_eye_view[0].display_name,
      oci_core_network_security_group.gods_eye_view_proxy[0].display_name,
      oci_core_network_security_group.gods_eye_view[0].display_name,
    ] == [for suffix in ["nat", "egress", "private", "proxy", "viewer"] : "aidp-lab-test1234-gods-eye-view-${suffix}"]
    error_message = "New portal-managed networks must use the canonical module name."
  }
}

run "install_gods_eye_view" {
  command = plan
  variables { enabled_vm_modules = ["gods_eye_view"] }
  assert {
    condition     = output.gods_eye_view_enabled && oci_core_instance.gods_eye_view[0].create_vnic_details[0].private_ip == "10.10.0.138"
    error_message = "Portal installation must create the single viewer at its reserved private address."
  }
  assert {
    condition = oci_identity_dynamic_group.gods_eye_view[0].name == "aidp-lab-test1234-gods-eye-view" && [
      oci_core_instance.gods_eye_view[0].display_name,
      oci_identity_policy.gods_eye_view[0].name,
      oci_identity_policy.gods_eye_view_run_command[0].name,
    ] == [for suffix in ["viewer", "read", "update"] : "aidp-lab-test1234-gods-eye-view-${suffix}"]
    error_message = "The new module VM and IAM resources must use the canonical name."
  }
}

run "reject_unknown_module" {
  command = plan
  variables { enabled_vm_modules = ["unknown-module"] }
  expect_failures = [var.enabled_vm_modules]
}
