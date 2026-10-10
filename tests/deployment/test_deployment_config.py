from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import unittest

from troubleshooter_deployment.deployment_config import (
    ConfigurationError, DeploymentTarget, from_environment, parse_tfvars,
)
from troubleshooter_deployment.records import Scope


COMPARTMENT = "ocid1.compartment.oc1..synthetic"
VALUES = {"region": "us-ashburn-1", "compartment_ocid": COMPARTMENT}
BODY = json.dumps(VALUES)


class DeploymentConfigurationTests(unittest.TestCase):
    def test_same_document_produces_scope_and_resource_manager_inputs(self):
        target = parse_tfvars(BODY)
        scope = target.scope(101, "demo", "synthetic-tenancy")
        self.assertEqual(scope, Scope(101, "demo", "synthetic-tenancy", COMPARTMENT, "us-ashburn-1"))
        self.assertEqual(target.resource_manager_variables(), VALUES)
        self.assertEqual(parse_tfvars(json.dumps(VALUES, indent=2)), target)
        self.assertEqual(parse_tfvars(json.dumps(dict(reversed(list(VALUES.items()))))), target)

    def test_environment_reader_uses_only_the_one_explicit_document(self):
        class Environment:
            def get(self, key, default):
                if key != "DEPLOYMENT_TFVARS_JSON":
                    raise AssertionError("unrelated environment access")
                return BODY

        self.assertEqual(from_environment(Environment()), parse_tfvars(BODY))
        self.assertEqual(from_environment({
            "DEPLOYMENT_TFVARS_JSON": BODY,
            "OCI_REGION": "other", "TF_VAR_region": "other",
            "OCI_PRIVATE_KEY": "synthetic-secret",
        }).resource_manager_variables(), VALUES)

    def test_missing_document_does_not_fall_back_to_per_field_values(self):
        with self.assertRaises(ConfigurationError):
            from_environment({"region": "us-ashburn-1", "compartment_ocid": COMPARTMENT})

    def test_target_is_immutable_and_returned_map_is_an_independent_copy(self):
        target = parse_tfvars(BODY)
        with self.assertRaises(FrozenInstanceError):
            target.region = "us-phoenix-1"
        copy = target.resource_manager_variables()
        copy["region"] = "us-phoenix-1"
        copy["presenter_password"] = "synthetic-secret"
        self.assertEqual(target.resource_manager_variables(), VALUES)
        self.assertNotIn(COMPARTMENT, repr(target))

    def test_requires_an_exact_object_of_two_strings(self):
        invalid = [None, [], True, 1, "text", {}, {"region": VALUES["region"]},
                   {"compartment_ocid": COMPARTMENT}]
        for key in ["private_key", "presenter_password", "compartment_id", "regionn",
                    "stack_id", "troubleshooter_cleanup_receipt_0", "synthetic-secret"]:
            invalid.append({**VALUES, key: "synthetic-secret"})
        for key in VALUES:
            for value in [None, True, 1, 1.5, [], {}, ""]:
                invalid.append({**VALUES, key: value})
        for value in invalid:
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(ConfigurationError):
                    parse_tfvars(json.dumps(value))

    def test_duplicate_keys_are_rejected_even_when_values_match(self):
        for extra in ['"region":"us-ashburn-1"', '"region":"us-phoenix-1"',
                      '"reg\\u0069on":"us-ashburn-1"',
                      '"compartment_ocid":"ocid1.compartment.oc1..synthetic"']:
            with self.assertRaises(ConfigurationError):
                parse_tfvars(BODY[:-1] + "," + extra + "}")

    def test_malformed_nonstandard_and_deep_json_are_rejected(self):
        invalid = ["", " ", "{", BODY + BODY, BODY[:-1] + ",}", "//comment\n" + BODY,
                   "[" * 1500 + "0" + "]" * 1500, "\ufeff" + BODY,
                   BODY.replace('"us-ashburn-1"', "NaN"),
                   BODY.replace('"us-ashburn-1"', "Infinity"),
                   BODY.replace('"us-ashburn-1"', "-Infinity")]
        for value in invalid:
            with self.assertRaises(ConfigurationError):
                parse_tfvars(value)

    def test_input_type_encoding_and_byte_bound(self):
        for value in [None, True, 1, {}, BODY.encode(), "\ud800", "x" * 4097,
                      "é" * 2049, BODY + " " * 4096]:
            with self.assertRaises(ConfigurationError):
                parse_tfvars(value)
        self.assertEqual(parse_tfvars(BODY + " " * (4096 - len(BODY))), parse_tfvars(BODY))
        with self.assertRaises(ConfigurationError):
            parse_tfvars(BODY.replace("us-ashburn-1", "us-ashburn-\\ud800"))

    def test_invalid_region_is_not_silently_normalized(self):
        for value in ["iad", "US-ASHBURN-1", " us-ashburn-1", "us-ashburn-1\n",
                      "us-ashburn-01", "us-ashburn-0", "us-ashburn-1.oraclecloud.com",
                      "us-ashburn-1/path", "us--1", "${region}", "$(echo secret)",
                      "us-" + "a" * 60 + "-1", "us-ashburn-١"]:
            with self.assertRaises(ConfigurationError):
                parse_tfvars(json.dumps({**VALUES, "region": value}))
        self.assertEqual(parse_tfvars(json.dumps({**VALUES, "region": "us-phoenix-1"})).region,
                         "us-phoenix-1")

    def test_wrong_realm_resource_or_compartment_format_is_rejected(self):
        for value in ["demo-compartment", "REPLACE_WITH_COMPARTMENT_OCID",
                      "ocid1.tenancy.oc1..synthetic", "ocid1.compartment.oc2..synthetic",
                      "ocid1.compartment.oc1..", "ocid1.compartment.oc1..synthetic\n",
                      "ocid1.compartment.oc1..${secret}", " " + COMPARTMENT,
                      "ocid1.compartment.oc1.." + "x" * 256]:
            with self.assertRaises(ConfigurationError):
                parse_tfvars(json.dumps({**VALUES, "compartment_ocid": value}))

    def test_direct_construction_cannot_bypass_validation(self):
        for region, compartment in [(None, COMPARTMENT), ("bad", COMPARTMENT),
                                    (VALUES["region"], None), (VALUES["region"], "bad")]:
            with self.assertRaises(ConfigurationError):
                DeploymentTarget(region, compartment)

    def test_scope_context_is_validated_without_echoing_values(self):
        target = parse_tfvars(BODY)
        for args in [(True, "demo", "tenancy"), (0, "demo", "tenancy"),
                     (101, "BAD ENVIRONMENT", "tenancy"), (101, "demo", "secret\n")]:
            with self.assertRaises(ConfigurationError) as error:
                target.scope(*args)
            self.assertNotIn("secret", str(error.exception))

    def test_errors_exclude_raw_values_and_untrusted_field_names(self):
        for text in ["synthetic-secret", json.dumps({**VALUES, "synthetic-secret": "secret"}),
                     json.dumps({**VALUES, "region": "synthetic-secret"}),
                     json.dumps({**VALUES, "compartment_ocid": "synthetic-secret"})]:
            with self.assertRaises(ConfigurationError) as error:
                parse_tfvars(text)
            self.assertNotIn("synthetic-secret", str(error.exception))
            self.assertIn("DEPLOYMENT_TFVARS_JSON", str(error.exception))

    def test_public_example_requires_only_replacing_the_compartment(self):
        example = (Path(__file__).resolve().parents[2] /
                   "terraform/deployment.tfvars.json.example").read_text()
        with self.assertRaises(ConfigurationError):
            parse_tfvars(example)
        target = parse_tfvars(example.replace("REPLACE_WITH_COMPARTMENT_OCID", COMPARTMENT))
        self.assertEqual(target.resource_manager_variables(), VALUES)


if __name__ == "__main__":
    unittest.main()
