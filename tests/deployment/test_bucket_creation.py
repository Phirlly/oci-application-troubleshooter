from base64 import b64encode
from dataclasses import replace
from hashlib import sha256
import unittest

from troubleshooter_deployment.recovery_bucket import (
    BucketCreationEvidence, BucketRequest, creation_body_v1, creation_digest_v1,
    validate_creation_evidence,
)
from tests.deployment.fixtures import IDENTITY

REQUEST = BucketRequest(IDENTITY.scope, "synthetic-ns", "synthetic-bucket", IDENTITY.epoch,
                        "synthetic-principal", "a" * 32)
BODY = (b'{"name": "synthetic-bucket", "compartmentId": "synthetic-compartment", '
        b'"metadata": {"deployment_epoch": "11111111111111111111111111111111", '
        b'"deployment_repository": "42", "deployment_environment": "demo", '
        b'"deployment_tenancy": "synthetic-tenancy"}, "publicAccessType": "NoPublicAccess", '
        b'"storageTier": "Standard", "objectEventsEnabled": false, "versioning": "Disabled", '
        b'"autoTiering": "Disabled", "bucketScope": "NAMESPACE"}')


class BucketCreationTests(unittest.TestCase):
    def test_frozen_v1_bytes_and_exact_base64_digest(self):
        self.assertEqual(creation_body_v1(REQUEST), BODY)
        self.assertEqual(creation_digest_v1(REQUEST), b64encode(sha256(BODY).digest()).decode())
        evidence = BucketCreationEvidence(REQUEST, creation_digest_v1(REQUEST))
        validate_creation_evidence(evidence)
        self.assertFalse(hasattr(evidence, "identity"))
        self.assertNotIn(REQUEST.principal, repr(evidence))

    def test_invalid_request_or_changed_body_cannot_be_evidence(self):
        evidence = BucketCreationEvidence(REQUEST, creation_digest_v1(REQUEST))
        for invalid in [None, True, REQUEST, replace(evidence, request=None),
                        replace(evidence, content_sha256=True), replace(evidence, content_sha256="bad"),
                        replace(evidence, request=replace(REQUEST, name="different")),
                        replace(evidence, request=replace(REQUEST, epoch="2" * 32)),
                        replace(evidence, request=replace(REQUEST, scope=replace(REQUEST.scope, environment="other")))]:
            with self.assertRaises(ValueError):
                validate_creation_evidence(invalid)
        for request in [None, True, replace(REQUEST, name="bad/path"), replace(REQUEST, namespace=""),
                        replace(REQUEST, correlation=False), replace(REQUEST, principal="bad\nprincipal")]:
            with self.assertRaises(ValueError):
                creation_body_v1(request)

    def test_maximum_valid_request_stays_bounded(self):
        scope = replace(REQUEST.scope, repository_id=2**63 - 1, environment="e" * 63,
                        tenancy_id="t" * 256, compartment_id="c" * 256, region="r" * 64)
        request = replace(REQUEST, scope=scope, namespace="n" * 256, name="b" * 256, principal="p" * 256)
        self.assertLess(len(creation_body_v1(request)), 2048)
        validate_creation_evidence(BucketCreationEvidence(request, creation_digest_v1(request)))
