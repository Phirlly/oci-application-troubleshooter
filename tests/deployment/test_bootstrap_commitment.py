from dataclasses import asdict, replace
from hashlib import sha256
import json
import unittest

from troubleshooter_deployment.bootstrap_commitment import (
    CommitmentError, commitment_payload, freeze_stack_request, parse_commitment, scope_digest,
)
from troubleshooter_deployment.codec import RecordError, validate_attempt
from tests.deployment.fixtures import ATTEMPT, IDENTITY


class BootstrapCommitmentTests(unittest.TestCase):
    def setUp(self):
        self.scope = IDENTITY.scope
        self.body = b'{"compartmentId":"synthetic-private-target","variables":{}}'
        self.commitment = freeze_stack_request(self.scope, ATTEMPT, self.body)

    def test_public_payload_is_closed_and_excludes_raw_target_and_request(self):
        payload = commitment_payload(self.commitment)
        self.assertEqual(set(payload), {"schema", "repository_id", "environment", "attempt",
                                       "scope_sha256", "request_sha256"})
        self.assertEqual(payload["attempt"], asdict(ATTEMPT))
        self.assertEqual(payload["request_sha256"], sha256(self.body).hexdigest())
        expected = json.dumps(asdict(self.scope), sort_keys=True, separators=(",", ":")).encode()
        self.assertEqual(payload["scope_sha256"], sha256(expected).hexdigest())
        encoded = json.dumps(payload)
        for private in [self.scope.tenancy_id, self.scope.compartment_id, self.scope.region,
                        "synthetic-private-target", "compartmentId", "variables"]:
            self.assertNotIn(private, encoded)
        self.assertEqual(parse_commitment(json.loads(encoded)), self.commitment)

    def test_reconstruction_is_stable_but_changed_inputs_change_commitment(self):
        self.assertEqual(freeze_stack_request(self.scope, ATTEMPT, self.body), self.commitment)
        variants = [freeze_stack_request(replace(self.scope, region="us-phoenix-1"), ATTEMPT, self.body),
                    freeze_stack_request(self.scope, replace(ATTEMPT, number=2), self.body),
                    freeze_stack_request(self.scope, ATTEMPT, self.body + b" ")]
        self.assertTrue(all(item != self.commitment for item in variants))

    def test_invalid_private_inputs_fail_before_hashing_or_publication(self):
        for body in [None, "body", b"", b"x" * 262145]:
            with self.assertRaises(CommitmentError):
                freeze_stack_request(self.scope, ATTEMPT, body)
        for scope in [None, replace(self.scope, repository_id=True), replace(self.scope, environment="../demo")]:
            with self.assertRaises(CommitmentError):
                freeze_stack_request(scope, ATTEMPT, self.body)
            with self.assertRaises(CommitmentError):
                scope_digest(scope)
        for attempt in [None, asdict(ATTEMPT), replace(ATTEMPT, run_id=True), replace(ATTEMPT, number=0),
                        replace(ATTEMPT, workflow_sha="bad"), replace(ATTEMPT, source_sha="X" * 40)]:
            with self.assertRaises(RecordError):
                validate_attempt(attempt)
            with self.assertRaises(CommitmentError):
                freeze_stack_request(self.scope, attempt, self.body)

    def test_payload_rejects_schema_extras_coercion_and_invalid_digests(self):
        original = commitment_payload(self.commitment)
        variants = [None, [], {}, dict(original, unexpected="private")]
        variants += [dict(original, **{key: value}) for key, value in [
            ("schema", True), ("schema", 2), ("repository_id", True), ("repository_id", 0),
            ("repository_id", 2**63), ("environment", "../demo"), ("environment", None),
            ("attempt", {}), ("attempt", []), ("scope_sha256", "a" * 63),
            ("request_sha256", "A" * 64), ("request_sha256", None),
        ]]
        for payload in variants:
            with self.subTest(payload=payload), self.assertRaises(CommitmentError):
                parse_commitment(payload)
        for commitment in [None, original, replace(self.commitment, schema=True),
                           replace(self.commitment, attempt=None)]:
            with self.assertRaises(CommitmentError):
                commitment_payload(commitment)
