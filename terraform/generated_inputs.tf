# Internal deployment output, not additional presenter setup.
# Syntax checks cannot prove ownership, capacity or image compatibility.
variable "automation" {
  description = "Frozen, nonsecret generation inputs supplied by deployment automation."
  nullable    = false
  type = object({
    generation_id            = string
    release_sha              = string
    tenancy_ocid             = string
    home_region              = string
    availability_domain      = string
    image_ocid               = string
    shape                    = string
    artifact_namespace       = string
    artifact_bucket          = string
    artifact_object          = string
    artifact_sha256          = string
    presenter_secret_ocid    = string
    presenter_secret_version = number
  })

  validation {
    condition = alltrue([
      can(regex("^[a-f0-9]{32}$", var.automation.generation_id)),
      can(regex("^[a-f0-9]{40}$", var.automation.release_sha)),
      can(regex("^[a-f0-9]{64}$", var.automation.artifact_sha256)),
      can(regex("^ocid1\\.tenancy\\.oc1\\.\\.[A-Za-z0-9]+$", var.automation.tenancy_ocid)),
      can(regex("^[a-z][a-z0-9]*(-[a-z0-9]+)+-[1-9][0-9]*$", var.automation.home_region)),
      can(regex("^[A-Za-z0-9:-]{1,128}$", var.automation.availability_domain)),
      can(regex("^ocid1\\.image\\.oc1\\.[a-z0-9-]+\\.[A-Za-z0-9]+$", var.automation.image_ocid)),
      can(regex("^ocid1\\.vaultsecret\\.oc1\\.[a-z0-9-]+\\.[A-Za-z0-9]+$", var.automation.presenter_secret_ocid)),
      can(regex("^[a-z0-9-]{1,64}$", var.automation.artifact_namespace)),
      can(regex("^[a-z0-9][a-z0-9-]{0,62}$", var.automation.artifact_bucket)),
      can(regex("^[a-z0-9][a-z0-9/_.-]{0,254}$", var.automation.artifact_object)),
      contains(["VM.Standard.E4.Flex", "VM.Standard.E5.Flex"], var.automation.shape),
    ])
    error_message = "Invalid automation identity, region, image, shape or artifact input."
  }

  validation {
    condition = try(
      length(var.automation.tenancy_ocid) <= 255 &&
      length(var.automation.home_region) <= 64 &&
      length(var.automation.image_ocid) <= 255 &&
      length(var.automation.presenter_secret_ocid) <= 255 &&
      var.automation.presenter_secret_version >= 1 &&
      var.automation.presenter_secret_version <= 2147483647 &&
      floor(var.automation.presenter_secret_version) == var.automation.presenter_secret_version,
      false
    )
    error_message = "Automation identifiers must fit metadata limits and secret version must be a positive integer."
  }
}

locals {
  name = "troubleshooter-${var.automation.generation_id}"
  tags = {
    "troubleshooter-generation" = var.automation.generation_id
    "troubleshooter-release"    = var.automation.release_sha
  }
}
