locals {
  governance_gateway_audience = var.enable_ai_data_governance ? "https://${oci_apigateway_gateway.governance[0].hostname}/governance" : ""
  governance_gateway_scope    = "${local.governance_gateway_audience}/governance.all"
  governance_jdbc_object      = ".governance/aidp-jdbc-driver.zip"
}

resource "oci_identity_domains_app" "governance_public_client" {
  count         = var.enable_ai_data_governance ? 1 : 0
  provider      = oci.home
  idcs_endpoint = local.default_domain.url
  schemas       = ["urn:ietf:params:scim:schemas:oracle:idcs:App"]
  display_name  = "${local.name_prefix} VS Code Governance"
  name          = "aidp_lab_${local.suffix}_governance"
  description   = "Public PKCE client for the AI Data Governance Gateway"
  active        = true

  is_oauth_client   = true
  is_oauth_resource = true
  client_type       = "public"
  audience          = local.governance_gateway_audience
  allowed_grants    = ["authorization_code", "refresh_token"]
  allow_offline     = true
  bypass_consent    = false
  trust_scope       = "Explicit"

  redirect_uris = [
    "vscode://AICloudTech.oci-ai-data-platform-workbench-aidp-developer-extension/governance/auth/callback",
    "vscode-insiders://AICloudTech.oci-ai-data-platform-workbench-aidp-developer-extension/governance/auth/callback",
  ]
  all_url_schemes_allowed = true

  based_on_template {
    value = "CustomBrowserMobileTemplateId"
  }

  scopes {
    value            = "governance.all"
    fqs              = local.governance_gateway_scope
    display_name     = "AI Data Governance Gateway"
    description      = "Execute governed queries and inspect effective policy"
    requires_consent = true
  }

  allowed_scopes {
    fqs = local.governance_gateway_scope
  }

  force_delete = true

  lifecycle {
    ignore_changes = [schemas]
  }
}

resource "oci_apigateway_gateway" "governance" {
  count          = var.enable_ai_data_governance ? 1 : 0
  compartment_id = local.target_compartment
  endpoint_type  = "PUBLIC"
  subnet_id      = oci_core_subnet.governance_gateway[0].id
  display_name   = "${local.name_prefix}-governance-edge"

  freeform_tags = {
    managed-by = "deploy-studio"
    workload   = "ai-data-governance"
  }
}

resource "oci_apigateway_deployment" "governance" {
  count          = var.enable_ai_data_governance ? 1 : 0
  compartment_id = local.target_compartment
  gateway_id     = oci_apigateway_gateway.governance[0].id
  path_prefix    = "/governance"
  display_name   = "${local.name_prefix}-governance"

  specification {
    request_policies {
      authentication {
        type                        = "TOKEN_AUTHENTICATION"
        token_header                = "Authorization"
        token_auth_scheme           = "Bearer"
        is_anonymous_access_allowed = false

        validation_policy {
          type = "STATIC_KEYS"

          dynamic "keys" {
            for_each = jsondecode(var.governance_gateway_oidc_static_jwks_json)
            content {
              format  = "JSON_WEB_KEY"
              alg     = keys.value.alg
              e       = keys.value.e
              kid     = keys.value.kid
              kty     = keys.value.kty
              n       = keys.value.n
              use     = try(keys.value.use, "sig")
              key_ops = ["verify"]
            }
          }

          additional_validation_policy {
            issuers   = [var.governance_gateway_oidc_issuer]
            audiences = [local.governance_gateway_audience]
          }
        }
      }
    }

    routes {
      path = "/{req*}"

      backend {
        type                       = "HTTP_BACKEND"
        url                        = "http://${var._oci_governance.gateway_backend_ip}:8080/$${request.path[req]}"
        connect_timeout_in_seconds = 10
        read_timeout_in_seconds    = 300
        send_timeout_in_seconds    = 300
      }

      request_policies {
        authorization {
          type          = "ANY_OF"
          allowed_scope = [local.governance_gateway_scope]
        }
      }
    }
  }
}

resource "oci_identity_user" "governance_jdbc" {
  count          = var.enable_ai_data_governance ? 1 : 0
  compartment_id = var.tenancy_ocid
  name           = "${local.name_prefix}-governance-jdbc"
  description    = "Dedicated API-key identity for the AI Data Governance JDBC runtime"
}

resource "oci_kms_vault" "governance" {
  count          = var.enable_ai_data_governance ? 1 : 0
  compartment_id = local.target_compartment
  display_name   = "${local.name_prefix}-governance"
  vault_type     = "DEFAULT"
}

resource "oci_kms_key" "governance" {
  count               = var.enable_ai_data_governance ? 1 : 0
  compartment_id      = local.target_compartment
  display_name        = "${local.name_prefix}-governance"
  management_endpoint = oci_kms_vault.governance[0].management_endpoint
  protection_mode     = "HSM"

  key_shape {
    algorithm = "AES"
    length    = 32
  }
}

resource "oci_vault_secret" "governance_jdbc" {
  count          = var.enable_ai_data_governance ? 1 : 0
  compartment_id = local.target_compartment
  vault_id       = oci_kms_vault.governance[0].id
  key_id         = oci_kms_key.governance[0].id
  secret_name    = "${local.name_prefix}-governance-jdbc"
  description    = "Runtime credential generated after AIDP reconciliation"

  secret_content {
    content_type = "BASE64"
    content      = base64encode("{\"status\":\"bootstrap_pending\"}")
  }

  lifecycle {
    ignore_changes = [secret_content]
  }
}

resource "oci_identity_policy" "governance_jdbc" {
  count          = var.enable_ai_data_governance ? 1 : 0
  provider       = oci.home
  compartment_id = var.tenancy_ocid
  name           = "${local.name_prefix}-governance-jdbc"
  description    = "Allow only the dedicated JDBC identity to connect to this AIDP instance"
  statements = [
    "Allow any-user to use ai-data-platforms in compartment id ${local.target_compartment} where request.principal.id = '${oci_identity_user.governance_jdbc[0].id}'",
  ]
}
