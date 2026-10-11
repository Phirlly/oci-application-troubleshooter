resource "oci_core_vcn" "demo" {
  compartment_id = var.compartment_ocid
  cidr_blocks    = ["10.78.0.0/16"]
  dns_label      = "troubleshooter"
  display_name   = local.name
  freeform_tags  = local.tags
}

resource "oci_core_dhcp_options" "demo" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.demo.id
  display_name   = local.name
  freeform_tags  = local.tags
  options {
    type        = "DomainNameServer"
    server_type = "VcnLocalPlusInternet"
  }
}

# Security lists and NSGs are additive. Do not inherit the default list.
resource "oci_core_security_list" "empty" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.demo.id
  display_name   = "${local.name}-empty"
  freeform_tags  = local.tags
}

resource "oci_core_internet_gateway" "demo" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.demo.id
  enabled        = true
  display_name   = local.name
  freeform_tags  = local.tags
}

resource "oci_core_nat_gateway" "demo" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.demo.id
  block_traffic  = false
  display_name   = local.name
  freeform_tags  = local.tags
}

resource "oci_core_route_table" "public" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.demo.id
  display_name   = "${local.name}-public"
  freeform_tags  = local.tags
  route_rules {
    destination       = "0.0.0.0/0"
    destination_type  = "CIDR_BLOCK"
    network_entity_id = oci_core_internet_gateway.demo.id
  }
}

resource "oci_core_route_table" "private" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.demo.id
  display_name   = "${local.name}-private"
  freeform_tags  = local.tags
  route_rules {
    destination       = "0.0.0.0/0"
    destination_type  = "CIDR_BLOCK"
    network_entity_id = oci_core_nat_gateway.demo.id
  }
}

resource "oci_core_subnet" "gateway" {
  compartment_id             = var.compartment_ocid
  vcn_id                     = oci_core_vcn.demo.id
  cidr_block                 = "10.78.0.0/24"
  dns_label                  = "gateway"
  prohibit_public_ip_on_vnic = false
  route_table_id             = oci_core_route_table.public.id
  dhcp_options_id            = oci_core_dhcp_options.demo.id
  security_list_ids          = [oci_core_security_list.empty.id]
  display_name               = "${local.name}-gateway"
  freeform_tags              = local.tags
}

resource "oci_core_subnet" "application" {
  compartment_id             = var.compartment_ocid
  vcn_id                     = oci_core_vcn.demo.id
  cidr_block                 = "10.78.1.0/24"
  dns_label                  = "application"
  prohibit_public_ip_on_vnic = true
  route_table_id             = oci_core_route_table.private.id
  dhcp_options_id            = oci_core_dhcp_options.demo.id
  security_list_ids          = [oci_core_security_list.empty.id]
  display_name               = "${local.name}-application"
  freeform_tags              = local.tags
}
