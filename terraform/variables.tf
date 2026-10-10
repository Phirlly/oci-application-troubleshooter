# Deployer target inputs only. The workload resources are not implemented yet.
# Credentials must never be supplied as Terraform variables.
variable "region" {
  description = "OCI deployment region identifier. Access is checked by deployment preflight."
  type        = string
  nullable    = false

  validation {
    condition     = length(var.region) <= 64 && can(regex("^[a-z][a-z0-9]*(-[a-z0-9]+)+-[1-9][0-9]*$", var.region))
    error_message = "Set region to an OCI region identifier, such as us-ashburn-1."
  }
}

variable "compartment_ocid" {
  description = "OC1 compartment OCID. Use an isolated compartment, not the tenancy root."
  type        = string
  nullable    = false

  validation {
    condition     = length(var.compartment_ocid) <= 256 && can(regex("^ocid1\\.compartment\\.oc1\\.\\.[A-Za-z0-9]+$", var.compartment_ocid))
    error_message = "Set compartment_ocid to an OC1 compartment OCID, not its name or a tenancy OCID."
  }
}
