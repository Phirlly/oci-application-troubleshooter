terraform {
  required_version = "= 1.5.7"
  required_providers {
    oci = {
      source  = "oracle/oci"
      version = "= 9.8.0"
    }
  }
}

# Resource Manager supplies authentication. Never put credentials in variables.
provider "oci" {
  region = var.region
}

provider "oci" {
  alias  = "home"
  region = var.automation.home_region
}
