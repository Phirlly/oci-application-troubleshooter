"""Acceptance checks of real Terraform plan JSON and dependency graph."""
from workload_fixture import VALUES

EXPECTED = {
    "oci_core_vcn.demo", "oci_core_dhcp_options.demo", "oci_core_security_list.empty",
    "oci_core_internet_gateway.demo", "oci_core_nat_gateway.demo",
    "oci_core_route_table.public", "oci_core_route_table.private",
    "oci_core_subnet.gateway", "oci_core_subnet.application",
    "oci_core_network_security_group.gateway", "oci_core_network_security_group.application",
    "oci_core_network_security_group_security_rule.https_ingress",
    "oci_core_network_security_group_security_rule.gateway_to_application",
    "oci_core_network_security_group_security_rule.application_from_gateway",
    "oci_core_network_security_group_security_rule.application_https_egress",
    "oci_core_volume.application", "oci_core_instance.application",
    "oci_core_volume_attachment.application", "oci_apigateway_gateway.demo",
    "oci_apigateway_deployment.demo", "oci_identity_dynamic_group.application",
    "oci_identity_policy.application",
}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def verify_plan(plan, graph):
    resources = {r["address"]: r for r in plan["resource_changes"]}
    require(set(resources) == EXPECTED, "exact owned resource inventory")
    require(all(r["mode"] == "managed" and r["change"]["actions"] == ["create"]
                for r in resources.values()), "only new managed resources")
    configuration = plan["configuration"]["root_module"]["resources"]
    require(all(not r.get("provisioners") for r in configuration), "no local/remote provisioners")
    providers = plan["configuration"]["provider_config"]
    require(set(providers) == {"oci", "oci.home"}, "only region/home providers")
    require(all(set(p["expressions"]) == {"region"} for p in providers.values()), "no provider credentials")
    configs = {r["address"]: r for r in configuration}
    for name in ("oci_identity_dynamic_group.application", "oci_identity_policy.application"):
        require(configs[name]["provider_config_key"] == "oci.home", "IAM home region")

    def value(name):
        return resources[name]["change"]["after"]

    def reference(name, field, expected):
        require(expected in configs[name]["expressions"][field]["references"],
                f"{name} {field} dependency")

    vm = value("oci_core_instance.application")
    require(vm["state"] == "STOPPED" and vm["preserve_boot_volume"] is False, "unfinished host state")
    require(vm["shape"] == VALUES["automation"]["shape"], "frozen x86 shape")
    require(vm["shape_config"][0]["ocpus"] == 1 and vm["shape_config"][0]["memory_in_gbs"] == 8, "VM allocation")
    require(vm["source_details"][0]["source_id"] == VALUES["automation"]["image_ocid"], "frozen image")
    require(vm["instance_options"][0]["are_legacy_imds_endpoints_disabled"] is True, "IMDSv2 only")
    require(vm["create_vnic_details"][0]["assign_public_ip"] == "false", "private VNIC")
    require(not any(key in vm.get("metadata", {}) for key in ("user_data", "ssh_authorized_keys")), "no unfinished startup")
    require(value("oci_core_volume.application")["size_in_gbs"] == "50", "data allocation")
    attachment = value("oci_core_volume_attachment.application")
    require(attachment["device"] == "/dev/oracleoci/oraclevdb"
            and attachment["attachment_type"] == "paravirtualized"
            and attachment["is_pv_encryption_in_transit_enabled"] is True
            and attachment["is_read_only"] is False and attachment["is_shareable"] is False, "data attachment")
    for name in ("application", "gateway"):
        subnet = value("oci_core_subnet." + name)
        require(subnet["prohibit_public_ip_on_vnic"] == (name == "application"), "subnet exposure")
        require(not subnet.get("availability_domain"), "regional subnet")
        reference("oci_core_subnet." + name, "security_list_ids", "oci_core_security_list.empty.id")
    empty = value("oci_core_security_list.empty")
    require(not empty["ingress_security_rules"] and not empty["egress_security_rules"], "no additive default access")
    rule_prefix = "oci_core_network_security_group_security_rule."
    rules = {
        "https_ingress": ("INGRESS", 443, "source", "0.0.0.0/0", "CIDR_BLOCK"),
        "gateway_to_application": ("EGRESS", 8080, "destination", None, "NETWORK_SECURITY_GROUP"),
        "application_from_gateway": ("INGRESS", 8080, "source", None, "NETWORK_SECURITY_GROUP"),
        "application_https_egress": ("EGRESS", 443, "destination", "0.0.0.0/0", "CIDR_BLOCK"),
    }
    for name, (direction, port, field, address, kind) in rules.items():
        rule = value(rule_prefix + name)
        ports = rule["tcp_options"][0]["destination_port_range"][0]
        require(rule["direction"] == direction and rule["protocol"] == "6" and rule["stateless"] is False
                and ports == {"min": port, "max": port} and rule[field + "_type"] == kind, "restricted stateful TCP")
        if address:
            require(rule[field] == address, "CIDR rule")
    reference(rule_prefix + "gateway_to_application", "destination", "oci_core_network_security_group.application.id")
    reference(rule_prefix + "application_from_gateway", "source", "oci_core_network_security_group.gateway.id")
    require(value("oci_apigateway_gateway.demo")["endpoint_type"] == "PUBLIC", "public gateway")
    deployment = value("oci_apigateway_deployment.demo")
    require(deployment["path_prefix"] == "/", "one root deployment")
    routes = deployment["specification"][0]["routes"]
    require(len(routes) == 1 and routes[0]["path"] == "/{path*}"
            and set(routes[0]["methods"]) == {"GET", "HEAD", "POST"}
            and routes[0]["backend"][0]["type"] == "HTTP_BACKEND", "same-origin route contract")
    output = plan["output_changes"]["workload"]["after"]
    require(output["ready"] is False and "not implemented" in output["reason"], "honest readiness")
    require(plan["variables"] == {key: {"value": value} for key, value in VALUES.items()}, "synthetic inputs only")
    for name in EXPECTED:
        require(name + " (expand)" in graph, "nonempty graph resource " + name)
    for start, end in [
        ("oci_core_instance.application", "local.host_metadata"),
        ("local.host_metadata", "oci_apigateway_gateway.demo"),
        ("local.host_metadata", "oci_core_volume.application"),
        ("oci_identity_dynamic_group.application", "oci_core_instance.application"),
        ("oci_identity_policy.application", "oci_identity_dynamic_group.application"),
        ("oci_apigateway_deployment.demo", "oci_core_instance.application"),
        ("oci_core_volume_attachment.application", "oci_core_instance.application"),
    ]:
        require(f'"[root] {start} (expand)" -> "[root] {end} (expand)"' in graph, "graph edge " + start + " -> " + end)
