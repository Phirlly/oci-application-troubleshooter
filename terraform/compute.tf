locals {
  # Host settings are individual bounded fields, not the app's RuntimeConfig.
  host_metadata = {
    generation_id            = var.automation.generation_id
    release_sha              = var.automation.release_sha
    public_origin            = "https://${oci_apigateway_gateway.demo.hostname}"
    data_volume_ocid         = oci_core_volume.application.id
    data_device              = "/dev/oracleoci/oraclevdb"
    artifact_namespace       = var.automation.artifact_namespace
    artifact_bucket          = var.automation.artifact_bucket
    artifact_object          = var.automation.artifact_object
    artifact_sha256          = var.automation.artifact_sha256
    presenter_secret_ocid    = var.automation.presenter_secret_ocid
    presenter_secret_version = tostring(var.automation.presenter_secret_version)
  }
}

resource "oci_core_volume" "application" {
  compartment_id      = var.compartment_ocid
  availability_domain = var.automation.availability_domain
  size_in_gbs         = "50"
  display_name        = "${local.name}-data"
  freeform_tags       = local.tags
}

resource "oci_core_instance" "application" {
  compartment_id      = var.compartment_ocid
  availability_domain = var.automation.availability_domain
  shape               = var.automation.shape
  display_name        = local.name
  freeform_tags       = local.tags
  # Development graph: provider launches first, then requests STOP.
  # Do not dispatch until the host bootstrap is implemented and verified.
  state                = "STOPPED"
  preserve_boot_volume = false
  metadata             = local.host_metadata

  shape_config {
    ocpus         = 1
    memory_in_gbs = 8
  }
  source_details {
    source_type             = "image"
    source_id               = var.automation.image_ocid
    boot_volume_size_in_gbs = 50
  }
  create_vnic_details {
    subnet_id        = oci_core_subnet.application.id
    assign_public_ip = "false"
    nsg_ids          = [oci_core_network_security_group.application.id]
  }
  instance_options {
    are_legacy_imds_endpoints_disabled = true
  }
  lifecycle {
    precondition {
      condition = length(local.host_metadata) <= 128 && alltrue([
        for key, value in local.host_metadata :
        length(key) <= 255 && length(value) <= 255 && can(regex("^[ -~]*$", value))
      ]) && length(jsonencode(local.host_metadata)) <= 32000
      error_message = "Rendered host metadata exceeds the allowed ASCII field or aggregate limits."
    }
  }
}

resource "oci_core_volume_attachment" "application" {
  attachment_type                     = "paravirtualized"
  instance_id                         = oci_core_instance.application.id
  volume_id                           = oci_core_volume.application.id
  device                              = "/dev/oracleoci/oraclevdb"
  is_read_only                        = false
  is_shareable                        = false
  is_pv_encryption_in_transit_enabled = true
  display_name                        = "${local.name}-data"
}
