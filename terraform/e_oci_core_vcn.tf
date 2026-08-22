resource "oci_core_vcn" "lab" {
  compartment_id = local.target_compartment
  cidr_blocks    = [var._oci_vcn.cidr_block, var._oci_governance.vcn_cidr]
  display_name   = "${local.name_prefix}-vcn"
  dns_label      = "aidplab"
}

data "oci_core_services" "all" {
  filter {
    name   = "name"
    regex  = true
    values = ["All .* Services In Oracle Services Network"]
  }
}

resource "oci_core_subnet" "public" {
  cidr_block                 = var._oci_vcn.cidr_block
  compartment_id             = local.target_compartment
  vcn_id                     = oci_core_vcn.lab.id
  display_name               = "${local.name_prefix}-public-subnet"
  dns_label                  = "public"
  prohibit_internet_ingress  = false
  prohibit_public_ip_on_vnic = false
  route_table_id             = oci_core_route_table.public.id
  security_list_ids          = [oci_core_security_list.web.id]
}

resource "oci_core_security_list" "web" {
  compartment_id = local.target_compartment
  vcn_id         = oci_core_vcn.lab.id
  display_name   = "${local.name_prefix}-web"

  dynamic "ingress_security_rules" {
    for_each = var._oci_vcn.ingress_tcp_ports
    content {
      protocol    = "6"
      source      = "0.0.0.0/0"
      description = "Allow TCP port ${ingress_security_rules.value}"

      tcp_options {
        min = ingress_security_rules.value
        max = ingress_security_rules.value
      }
    }
  }

  egress_security_rules {
    protocol    = "all"
    destination = "0.0.0.0/0"
  }
}

resource "oci_core_internet_gateway" "lab" {
  compartment_id = local.target_compartment
  vcn_id         = oci_core_vcn.lab.id
  display_name   = "${local.name_prefix}-igw"
  enabled        = true
}

resource "oci_core_route_table" "public" {
  compartment_id = local.target_compartment
  vcn_id         = oci_core_vcn.lab.id
  display_name   = "${local.name_prefix}-public-routes"

  route_rules {
    destination       = "0.0.0.0/0"
    destination_type  = "CIDR_BLOCK"
    network_entity_id = oci_core_internet_gateway.lab.id
  }
}

resource "oci_core_nat_gateway" "governance" {
  count          = var.enable_ai_data_governance ? 1 : 0
  compartment_id = local.target_compartment
  vcn_id         = oci_core_vcn.lab.id
  display_name   = "${local.name_prefix}-governance-nat"
}

resource "oci_core_service_gateway" "governance" {
  count          = var.enable_ai_data_governance ? 1 : 0
  compartment_id = local.target_compartment
  vcn_id         = oci_core_vcn.lab.id
  display_name   = "${local.name_prefix}-governance-services"

  services {
    service_id = data.oci_core_services.all.services[0].id
  }
}

resource "oci_core_route_table" "governance_private" {
  count          = var.enable_ai_data_governance ? 1 : 0
  compartment_id = local.target_compartment
  vcn_id         = oci_core_vcn.lab.id
  display_name   = "${local.name_prefix}-governance-private-routes"

  route_rules {
    destination       = "0.0.0.0/0"
    destination_type  = "CIDR_BLOCK"
    network_entity_id = oci_core_nat_gateway.governance[0].id
  }

  route_rules {
    destination       = data.oci_core_services.all.services[0].cidr_block
    destination_type  = "SERVICE_CIDR_BLOCK"
    network_entity_id = oci_core_service_gateway.governance[0].id
  }
}

resource "oci_core_security_list" "governance_endpoint" {
  count          = var.enable_ai_data_governance ? 1 : 0
  compartment_id = local.target_compartment
  vcn_id         = oci_core_vcn.lab.id
  display_name   = "${local.name_prefix}-governance-endpoint"

  ingress_security_rules {
    protocol = "6"
    source   = var._oci_governance.worker_subnet_cidr
    tcp_options {
      min = 6443
      max = 6443
    }
  }

  ingress_security_rules {
    protocol = "6"
    source   = var._oci_governance.endpoint_subnet_cidr
    tcp_options {
      min = 6443
      max = 6443
    }
  }

  ingress_security_rules {
    protocol = "6"
    source   = var._oci_vcn.cidr_block
    tcp_options {
      min = 6443
      max = 6443
    }
  }

  egress_security_rules {
    protocol    = "all"
    destination = "0.0.0.0/0"
  }
}

resource "oci_core_security_list" "governance_workers" {
  count          = var.enable_ai_data_governance ? 1 : 0
  compartment_id = local.target_compartment
  vcn_id         = oci_core_vcn.lab.id
  display_name   = "${local.name_prefix}-governance-workers"

  ingress_security_rules {
    protocol = "all"
    source   = var._oci_governance.vcn_cidr
  }

  egress_security_rules {
    protocol    = "all"
    destination = "0.0.0.0/0"
  }
}

resource "oci_core_security_list" "governance_service" {
  count          = var.enable_ai_data_governance ? 1 : 0
  compartment_id = local.target_compartment
  vcn_id         = oci_core_vcn.lab.id
  display_name   = "${local.name_prefix}-governance-service"

  ingress_security_rules {
    protocol = "6"
    source   = var._oci_governance.gateway_subnet_cidr
    tcp_options {
      min = 8080
      max = 8080
    }
  }

  egress_security_rules {
    protocol    = "all"
    destination = "0.0.0.0/0"
  }
}

resource "oci_core_security_list" "governance_gateway" {
  count          = var.enable_ai_data_governance ? 1 : 0
  compartment_id = local.target_compartment
  vcn_id         = oci_core_vcn.lab.id
  display_name   = "${local.name_prefix}-governance-gateway"

  ingress_security_rules {
    protocol = "6"
    source   = "0.0.0.0/0"
    tcp_options {
      min = 443
      max = 443
    }
  }

  egress_security_rules {
    protocol    = "6"
    destination = var._oci_governance.service_subnet_cidr
    tcp_options {
      min = 8080
      max = 8080
    }
  }

  egress_security_rules {
    protocol    = "6"
    destination = "0.0.0.0/0"
    tcp_options {
      min = 443
      max = 443
    }
  }
}

resource "oci_core_subnet" "governance_endpoint" {
  count                      = var.enable_ai_data_governance ? 1 : 0
  cidr_block                 = var._oci_governance.endpoint_subnet_cidr
  compartment_id             = local.target_compartment
  vcn_id                     = oci_core_vcn.lab.id
  display_name               = "${local.name_prefix}-governance-endpoint"
  dns_label                  = "govapi"
  prohibit_internet_ingress  = true
  prohibit_public_ip_on_vnic = true
  route_table_id             = oci_core_route_table.governance_private[0].id
  security_list_ids          = [oci_core_security_list.governance_endpoint[0].id]
}

resource "oci_core_subnet" "governance_workers" {
  count                      = var.enable_ai_data_governance ? 1 : 0
  cidr_block                 = var._oci_governance.worker_subnet_cidr
  compartment_id             = local.target_compartment
  vcn_id                     = oci_core_vcn.lab.id
  display_name               = "${local.name_prefix}-governance-workers"
  dns_label                  = "govnodes"
  prohibit_internet_ingress  = true
  prohibit_public_ip_on_vnic = true
  route_table_id             = oci_core_route_table.governance_private[0].id
  security_list_ids          = [oci_core_security_list.governance_workers[0].id]
}

resource "oci_core_subnet" "governance_service" {
  count                      = var.enable_ai_data_governance ? 1 : 0
  cidr_block                 = var._oci_governance.service_subnet_cidr
  compartment_id             = local.target_compartment
  vcn_id                     = oci_core_vcn.lab.id
  display_name               = "${local.name_prefix}-governance-service"
  dns_label                  = "govsvc"
  prohibit_internet_ingress  = true
  prohibit_public_ip_on_vnic = true
  route_table_id             = oci_core_route_table.governance_private[0].id
  security_list_ids          = [oci_core_security_list.governance_service[0].id]
}

resource "oci_core_subnet" "governance_gateway" {
  count                      = var.enable_ai_data_governance ? 1 : 0
  cidr_block                 = var._oci_governance.gateway_subnet_cidr
  compartment_id             = local.target_compartment
  vcn_id                     = oci_core_vcn.lab.id
  display_name               = "${local.name_prefix}-governance-gateway"
  dns_label                  = "govedge"
  prohibit_internet_ingress  = false
  prohibit_public_ip_on_vnic = false
  route_table_id             = oci_core_route_table.public.id
  security_list_ids          = [oci_core_security_list.governance_gateway[0].id]
}
