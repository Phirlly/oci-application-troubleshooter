output "workload" {
  description = "Nonsecret ownership evidence; provisioning is not demo readiness."
  value = {
    generation_id          = var.automation.generation_id
    release_sha            = var.automation.release_sha
    instance_ocid          = oci_core_instance.application.id
    data_volume_ocid       = oci_core_volume.application.id
    volume_attachment_ocid = oci_core_volume_attachment.application.id
    dynamic_group_ocid     = oci_identity_dynamic_group.application.id
    runtime_policy_ocid    = oci_identity_policy.application.id
    gateway_ocid           = oci_apigateway_gateway.demo.id
    api_deployment_ocid    = oci_apigateway_deployment.demo.id
    public_origin          = "https://${oci_apigateway_gateway.demo.hostname}"
    ready                  = false
    reason                 = "Host bootstrap and deployment verification are not implemented."
  }
}
