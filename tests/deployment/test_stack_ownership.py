from dataclasses import replace
from hashlib import sha256
import json
import unittest

from troubleshooter_deployment.bootstrap_commitment import commitment_payload, freeze_stack_request
from troubleshooter_deployment.stack_ownership import (
    StackOwnershipError, freeze_stack_ownership, ownership_payload, parse_ownership,
)
from tests.deployment.fixtures import ATTEMPT, IDENTITY


class StackOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.bootstrap = freeze_stack_request(IDENTITY.scope, ATTEMPT, b"synthetic-private-request")
        self.stack_id = "test.ormstack.oc1.iad.synthetic"
        self.ownership = freeze_stack_ownership(self.bootstrap, self.stack_id)

    def test_closed_public_record_binds_exact_bootstrap_and_id_without_disclosing_it(self):
        payload = ownership_payload(self.ownership)
        binding = {"schema": 1, "bootstrap": commitment_payload(self.bootstrap), "stack_id": self.stack_id}
        digest = sha256(json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(payload, {"schema": 1, "bootstrap": commitment_payload(self.bootstrap), "stack_sha256": digest})
        self.assertEqual(parse_ownership(json.loads(json.dumps(payload))), self.ownership)
        for private in [self.stack_id, IDENTITY.scope.tenancy_id, IDENTITY.scope.compartment_id, "synthetic-private-request"]:
            self.assertNotIn(private, json.dumps(payload))
        self.assertEqual(freeze_stack_ownership(self.bootstrap, self.stack_id), self.ownership)
        for bootstrap, identifier in [(replace(self.bootstrap, request_sha256="a" * 64), self.stack_id),
                                      (self.bootstrap, self.stack_id + "2")]:
            self.assertNotEqual(freeze_stack_ownership(bootstrap, identifier).stack_sha256, digest)

    def test_invalid_or_coerced_input_cannot_be_published_or_matched(self):
        payload = ownership_payload(self.ownership)
        for value in [None, [], {}, {**payload, "extra": "private"}, {**payload, "schema": True},
                      {**payload, "schema": 2}, {**payload, "bootstrap": {}},
                      {**payload, "stack_sha256": None}, {**payload, "stack_sha256": "A" * 64}]:
            with self.subTest(value=value), self.assertRaises(StackOwnershipError):
                parse_ownership(value)
        for value in [None, payload, replace(self.ownership, schema=True)]:
            with self.assertRaises(StackOwnershipError):
                ownership_payload(value)
        for identifier in [None, "", "bad/id", "x" * 257]:
            with self.assertRaises(StackOwnershipError):
                freeze_stack_ownership(self.bootstrap, identifier)
        with self.assertRaises(StackOwnershipError):
            freeze_stack_ownership(replace(self.bootstrap, schema=True), self.stack_id)
