from copy import deepcopy
import unittest
from unittest.mock import patch

from troubleshooter_host.inputs import HostError, HostInputs, encoded
from troubleshooter_host.network import ensure_network, firewall_objects, validate_firewall, NETWORK, BRIDGE, SUBNET
from tests.host.test_inputs import metadata


def network(inputs):
    return [{"name": NETWORK, "driver": "bridge", "network_interface": BRIDGE,
             "subnets": [{"subnet": SUBNET, "gateway": "10.89.42.1"}],
             "ipv6_enabled": False, "internal": False, "dns_enabled": True,
             "labels": {"troubleshooter-generation": inputs.generation_id}}]


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.inputs = HostInputs.from_imds(metadata())

    def test_metadata_guard_exact_chain_rule_and_no_offload(self):
        values = firewall_objects()
        validate_firewall({"nftables": [{"metainfo": {}}, *values]})
        for mutate in [lambda v: v.pop(), lambda v: v[1]["chain"].update(hook="input"),
                       lambda v: v[2]["rule"].update(expr=[]),
                       lambda v: v.append({"flowtable": {"name": "bypass"}})]:
            changed = deepcopy(values)
            mutate(changed)
            with self.assertRaises(HostError):
                validate_firewall({"nftables": changed})
        expression = values[2]["rule"]["expr"]
        self.assertEqual(expression[0]["match"]["right"], BRIDGE)
        self.assertEqual(expression[1]["match"]["right"], "169.254.169.254")
        self.assertEqual(expression[2]["match"]["right"], {"set": [80, 443]})
        self.assertEqual(expression[-1], {"drop": None})

    def test_existing_exact_network_and_guard_are_reused_without_writes(self):
        calls = []
        def run(argv, **options):
            calls.append(argv)
            if argv[0] == "getenforce":
                return b"Enforcing\n"
            if argv[:3] == ["podman", "network", "ls"]:
                return (NETWORK + "\n").encode()
            if argv[:3] == ["podman", "network", "inspect"]:
                return encoded(network(self.inputs))
            return encoded({"nftables": firewall_objects()})
        with patch("troubleshooter_host.network.command", side_effect=run):
            ensure_network(self.inputs)
        self.assertFalse(any("create" in argv or "--file" in argv for argv in calls))

    def test_new_network_and_guard_created_then_independently_checked(self):
        calls, reads = [], 0
        def run(argv, **options):
            nonlocal reads
            calls.append((argv, options))
            if argv[0] == "getenforce":
                return b"Enforcing\n"
            if argv[:3] == ["podman", "network", "ls"]:
                return b"podman\n"
            if argv[:3] == ["podman", "network", "inspect"]:
                return encoded(network(self.inputs))
            if argv[0] == "ip":
                return encoded([{"dst": "default"}, {"dst": "10.78.0.0/24"}])
            if argv == ["nft", "--json", "list", "ruleset"]:
                reads += 1
                return encoded({"nftables": [] if reads == 1 else firewall_objects()})
            return b""
        with patch("troubleshooter_host.network.command", side_effect=run):
            ensure_network(self.inputs)
        self.assertTrue(any(argv[:3] == ["podman", "network", "create"] for argv, _ in calls))
        mutation = [options["stdin"] for argv, options in calls if "--file" in argv]
        self.assertEqual(len(mutation), 1)
        self.assertNotIn(b"flush", mutation[0])
        self.assertEqual(reads, 2)

    def test_foreign_network_selinux_or_conflicting_route_stops_startup(self):
        with patch("troubleshooter_host.network.command", return_value=b"Permissive\n"):
            with self.assertRaises(HostError):
                ensure_network(self.inputs)
        wrong = network(self.inputs)
        wrong[0]["labels"] = {}
        with patch("troubleshooter_host.network.command", side_effect=[b"Enforcing", NETWORK.encode(), encoded(wrong)]):
            with self.assertRaises(HostError):
                ensure_network(self.inputs)
        with patch("troubleshooter_host.network.command", side_effect=[b"Enforcing", b"", encoded([{"dst": "10.89.0.0/16"}])]):
            with self.assertRaises(HostError):
                ensure_network(self.inputs)
