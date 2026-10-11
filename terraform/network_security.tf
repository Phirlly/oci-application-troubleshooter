resource "oci_core_network_security_group" "gateway" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.demo.id
  display_name   = "${local.name}-gateway"
  freeform_tags  = local.tags
}

resource "oci_core_network_security_group" "application" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.demo.id
  display_name   = "${local.name}-application"
  freeform_tags  = local.tags
}

resource "oci_core_network_security_group_security_rule" "https_ingress" {
  network_security_group_id = oci_core_network_security_group.gateway.id
  direction                 = "INGRESS"
  protocol                  = "6"
  source                    = "0.0.0.0/0"
  source_type               = "CIDR_BLOCK"
  stateless                 = false
  tcp_options {
    destination_port_range {
      min = 443
      max = 443
    }
  }
}

resource "oci_core_network_security_group_security_rule" "gateway_to_application" {
  network_security_group_id = oci_core_network_security_group.gateway.id
  direction                 = "EGRESS"
  protocol                  = "6"
  destination               = oci_core_network_security_group.application.id
  destination_type          = "NETWORK_SECURITY_GROUP"
  stateless                 = false
  tcp_options {
    destination_port_range {
      min = 8080
      max = 8080
    }
  }
}

resource "oci_core_network_security_group_security_rule" "application_from_gateway" {
  network_security_group_id = oci_core_network_security_group.application.id
  direction                 = "INGRESS"
  protocol                  = "6"
  source                    = oci_core_network_security_group.gateway.id
  source_type               = "NETWORK_SECURITY_GROUP"
  stateless                 = false
  tcp_options {
    destination_port_range {
      min = 8080
      max = 8080
    }
  }
}

resource "oci_core_network_security_group_security_rule" "application_https_egress" {
  network_security_group_id = oci_core_network_security_group.application.id
  direction                 = "EGRESS"
  protocol                  = "6"
  destination               = "0.0.0.0/0"
  destination_type          = "CIDR_BLOCK"
  stateless                 = false
  tcp_options {
    destination_port_range {
      min = 443
      max = 443
    }
  }
}
