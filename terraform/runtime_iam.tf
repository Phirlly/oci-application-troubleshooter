resource "oci_identity_dynamic_group" "application" {
  provider       = oci.home
  compartment_id = var.automation.tenancy_ocid
  name           = local.name
  description    = "Only the instance owned by this troubleshooter generation."
  matching_rule  = "ALL {instance.id = '${oci_core_instance.application.id}'}"
  freeform_tags  = local.tags
}

# Destroy integration must first terminate and verify the owned instance.
# OCI requires an empty dynamic group before deleting it.
resource "oci_identity_policy" "application" {
  provider       = oci.home
  compartment_id = var.compartment_ocid
  name           = local.name
  description    = "Read the exact application artifact and presenter verifier."
  statements = [
    "Allow dynamic-group id ${oci_identity_dynamic_group.application.id} to read objects in compartment id ${var.compartment_ocid} where all {target.bucket.name = '${var.automation.artifact_bucket}', target.object.name = '${var.automation.artifact_object}', request.permission = 'OBJECT_READ'}",
    "Allow dynamic-group id ${oci_identity_dynamic_group.application.id} to read secret-bundles in compartment id ${var.compartment_ocid} where all {target.secret.id = '${var.automation.presenter_secret_ocid}', request.permission = 'SECRET_BUNDLE_READ'}",
  ]
  freeform_tags = local.tags
}
