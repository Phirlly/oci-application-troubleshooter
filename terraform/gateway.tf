resource "oci_apigateway_gateway" "demo" {
  compartment_id             = var.compartment_ocid
  endpoint_type              = "PUBLIC"
  subnet_id                  = oci_core_subnet.gateway.id
  network_security_group_ids = [oci_core_network_security_group.gateway.id]
  display_name               = local.name
  freeform_tags              = local.tags
}

resource "oci_apigateway_deployment" "demo" {
  compartment_id = var.compartment_ocid
  gateway_id     = oci_apigateway_gateway.demo.id
  path_prefix    = "/"
  display_name   = local.name
  freeform_tags  = local.tags
  specification {
    routes {
      path    = "/{path*}"
      methods = ["GET", "HEAD", "POST"]
      backend {
        type                       = "HTTP_BACKEND"
        url                        = "http://${oci_core_instance.application.private_ip}:8080/$${request.path[path]}"
        connect_timeout_in_seconds = 10
        read_timeout_in_seconds    = 30
        send_timeout_in_seconds    = 30
      }
    }
  }
}
