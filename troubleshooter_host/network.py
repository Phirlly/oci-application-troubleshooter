"""One owned bridge with an independently verified metadata HTTP deny rule."""
import ipaddress
import json

from .host_io import command
from .inputs import HostError, encoded, unique_fields

NETWORK = "ocitroubleshooter"
BRIDGE = "ocitdemo0"
SUBNET = "10.89.42.0/24"
TABLE = "ocitroubleshooter"


def require(value):
    if not value:
        raise HostError("network_isolation_invalid")


def document(argv):
    return json.loads(command(argv), object_pairs_hook=unique_fields)


def firewall_objects():
    return [
        {"table": {"family": "inet", "name": TABLE}},
        {"chain": {"family": "inet", "table": TABLE, "name": "forward",
                   "type": "filter", "hook": "forward", "prio": -200, "policy": "accept"}},
        {"rule": {"family": "inet", "table": TABLE, "chain": "forward", "expr": [
            {"match": {"op": "==", "left": {"meta": {"key": "iifname"}}, "right": BRIDGE}},
            {"match": {"op": "==", "left": {"payload": {"protocol": "ip", "field": "daddr"}},
                       "right": "169.254.169.254"}},
            {"match": {"op": "==", "left": {"payload": {"protocol": "tcp", "field": "dport"}},
                       "right": {"set": [80, 443]}}},
            {"drop": None},
        ]}},
    ]


def owned_firewall(rules):
    result = []
    for item in rules["nftables"]:
        require("flowtable" not in item)
        for kind, value in item.items():
            if kind == "metainfo":
                continue
            if value.get("family") == "inet" and (value.get("table") == TABLE or
                                                    (kind == "table" and value.get("name") == TABLE)):
                result.append({kind: {key: val for key, val in value.items() if key != "handle"}})
    return result


def validate_firewall(rules):
    require(owned_firewall(rules) == firewall_objects())


def ensure_network(inputs):
    require(command(["getenforce"]).strip() == b"Enforcing")
    names = command(["podman", "network", "ls", "--format", "{{.Name}}"]).decode().splitlines()
    if NETWORK not in names:
        for route in document(["ip", "-json", "route", "show"]):
            if route.get("dst", "default") != "default":
                require(not ipaddress.ip_network(SUBNET).overlaps(ipaddress.ip_network(route["dst"])))
        command(["podman", "network", "create", "--driver", "bridge", "--interface-name", BRIDGE,
                 "--subnet", SUBNET, "--gateway", "10.89.42.1", "--label",
                 "troubleshooter-generation=" + inputs.generation_id, NETWORK])
    networks = document(["podman", "network", "inspect", NETWORK])
    require(len(networks) == 1)
    value = networks[0]
    require(value["name"] == NETWORK and value["driver"] == "bridge" and value["network_interface"] == BRIDGE
            and value["subnets"] == [{"subnet": SUBNET, "gateway": "10.89.42.1"}]
            and value["ipv6_enabled"] is False and value["internal"] is False and value["dns_enabled"] is True
            and value["labels"] == {"troubleshooter-generation": inputs.generation_id})
    rules = document(["nft", "--json", "list", "ruleset"])
    if not owned_firewall(rules):
        command(["nft", "--json", "--file", "-"], stdin=encoded(
            {"nftables": [{"add": value} for value in firewall_objects()]}))
        rules = document(["nft", "--json", "list", "ruleset"])
    validate_firewall(rules)
