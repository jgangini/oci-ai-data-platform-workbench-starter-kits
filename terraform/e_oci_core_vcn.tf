resource "oci_core_vcn" "lab" {
  compartment_id = local.target_compartment
  cidr_blocks    = [var._oci_vcn.cidr_block]
  display_name   = "${local.name_prefix}-vcn"
  dns_label      = "aidplab"
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
