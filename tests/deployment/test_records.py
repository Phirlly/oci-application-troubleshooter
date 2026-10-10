from dataclasses import FrozenInstanceError, replace
import json
import unittest

from troubleshooter_deployment.codec import RecordError, decode, encode
from troubleshooter_deployment.policy import bootstrap, begin_generation, reserve
from tests.deployment.fixtures import ATTEMPT, IDENTITY, GENERATION, OPERATION, SHA


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.record = begin_generation(bootstrap(IDENTITY, IDENTITY, ATTEMPT), GENERATION, SHA)

    def test_roundtrip_is_immutable_and_deterministic(self):
        pending, _ = reserve(self.record, GENERATION, OPERATION, "provision")
        self.assertEqual(decode(encode(pending)), pending)
        self.assertEqual(encode(decode(encode(pending))), encode(pending))
        self.assertIsInstance(pending.generations, tuple)
        with self.assertRaises(FrozenInstanceError):
            pending.owner = ATTEMPT

    def test_closed_schema_has_no_secret_payload_field(self):
        data = json.loads(encode(self.record))
        for key in ["password", "verifier", "private_key", "api_key", "payload", "credential_digest"]:
            poisoned = dict(data, **{key: "synthetic-sensitive-value"})
            with self.assertRaises(RecordError) as failure:
                decode(json.dumps(poisoned).encode())
            self.assertNotIn("synthetic-sensitive-value", str(failure.exception))

    def test_rejects_duplicate_unknown_malformed_and_oversized_json(self):
        for body in [b'{"schema":1,"schema":1}', b'{', b'[]', b'null', b'NaN', b'"' + b'a' * 262145 + b'"', b'[' * 2000 + b']' * 2000]:
            with self.subTest(length=len(body)), self.assertRaises(RecordError):
                decode(body)

    def test_rejects_schema_and_exact_type_mismatches(self):
        original = json.loads(encode(self.record))
        for field, value in [("schema", 2), ("schema", True), ("generations", {}), ("operations", None), ("receipt", [])]:
            with self.subTest(field=field), self.assertRaises(RecordError):
                decode(json.dumps(dict(original, **{field: value})).encode())
        for value in [True, 0, -1, "42", 1.5]:
            data = json.loads(encode(self.record))
            data["identity"]["scope"]["repository_id"] = value
            with self.assertRaises(RecordError):
                decode(json.dumps(data).encode())

    def test_rejects_unknown_nested_fields_and_invalid_identity(self):
        for path in [("owner",), ("identity",), ("identity", "scope")]:
            data = json.loads(encode(self.record))
            node = data
            for key in path:
                node = node[key]
            node["unexpected"] = "value"
            with self.assertRaises(RecordError):
                decode(json.dumps(data).encode())
        for identity in [replace(IDENTITY, epoch="bad"), replace(IDENTITY, bucket_id="token with whitespace"), replace(IDENTITY, scope=replace(IDENTITY.scope, environment=""))]:
            with self.assertRaises(RecordError):
                encode(replace(self.record, identity=identity))

    def test_rejects_invalid_cross_record_relationships(self):
        pending, _ = reserve(self.record, GENERATION, OPERATION, "provision")
        for broken in [
            replace(self.record, generations=self.record.generations * 2),
            replace(pending, operations=pending.operations * 2),
            replace(pending, generations=()),
            replace(pending, operations=(replace(pending.operations[0], release_sha="b" * 40),)),
            replace(self.record, generations=(replace(self.record.generations[0], phase="removing"),)),
        ]:
            with self.assertRaises(RecordError):
                encode(broken)
