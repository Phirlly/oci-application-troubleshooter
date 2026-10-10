from dataclasses import replace
from hashlib import sha256
import json
import unittest

from troubleshooter_deployment.bucket_deletion import (
    BucketDeleteIntent, DeletionIntentError, KEY, PREFIX, encode_intent,
    decode_intent, merge_deletion, read_deletion,
)
from troubleshooter_deployment.codec import encode_receipt
from troubleshooter_deployment.receipt_variables import merge_receipt
from troubleshooter_deployment.recovery_bucket import BucketAcknowledgement, BucketRequest
from tests.deployment.test_receipt_variables import RECEIPT

ACK = BucketAcknowledgement(
    BucketRequest(RECEIPT.identity.scope, "synthetic-ns", "synthetic-bucket",
                  RECEIPT.identity.epoch, "synthetic-principal", "a" * 32), RECEIPT.identity)
INTENT = BucketDeleteIntent(ACK, RECEIPT.removal_id, sha256(encode_receipt(RECEIPT)).hexdigest(),
                           "b" * 32, '"revision-1"')


class BucketDeletionTests(unittest.TestCase):
    def test_canonical_roundtrip_and_preserved_immutable_carrier(self):
        original = merge_receipt({"label": "日本語"}, RECEIPT)
        self.assertIsNone(read_deletion(original))
        merged = merge_deletion(original, INTENT)
        self.assertEqual(decode_intent(encode_intent(INTENT, RECEIPT), RECEIPT), INTENT)
        self.assertEqual(read_deletion(merged), INTENT)
        self.assertEqual(merge_deletion(merged, INTENT), merged)
        self.assertEqual({k: merged[k] for k in original}, original)
        self.assertNotIn(KEY, original)
        with self.assertRaises(DeletionIntentError):
            merge_deletion(merged, replace(INTENT, if_match='"changed"'))

    def test_invalid_bindings_and_strong_etag_rejected(self):
        bad = [None, True, replace(INTENT, ack=None), replace(INTENT, removal_id="f" * 32),
               replace(INTENT, receipt_sha256="0" * 64), replace(INTENT, correlation=True),
               replace(INTENT, correlation=ACK.request.correlation),
               replace(INTENT, ack=replace(ACK, identity=replace(ACK.identity, bucket_id="other")))]
        bad += [replace(INTENT, if_match=v) for v in ("*", "W/v1", "", "x" * 1025, True, "a\nb")]
        for intent in bad:
            with self.subTest(intent=intent), self.assertRaises(DeletionIntentError):
                encode_intent(intent, RECEIPT)
        with self.assertRaises(DeletionIntentError):
            encode_intent(INTENT, replace(RECEIPT, generation_ids=()))

    def test_closed_canonical_json_and_carrier_validation(self):
        body = encode_intent(INTENT, RECEIPT)
        data = json.loads(body)
        malformed = [None, True, b"x" * 6001, b"{}", b"[]", body + b"\n",
                     body.replace(b'"schema":1', b'"schema":1,"schema":1'),
                     json.dumps(data).encode()]
        for change in ({"schema": True}, {"schema": 2}, {"extra": 1}, {"ack": {}},
                       {"ack": dict(data["ack"], extra=1)}, {"correlation": []}):
            malformed.append(json.dumps(dict(data, **change), sort_keys=True, separators=(",", ":")).encode())
        for value in malformed:
            with self.subTest(value=str(value)[:60]), self.assertRaises(DeletionIntentError):
                decode_intent(value, RECEIPT)
        good = merge_deletion(merge_receipt({}, RECEIPT), INTENT)
        for variables in ({KEY: good[KEY]}, dict(good, **{PREFIX + "extra": ""}),
                          dict(good, **{KEY: ""}), dict(good, **{KEY: "é"}), None):
            with self.assertRaises(DeletionIntentError):
                read_deletion(variables)

    def test_maximum_intent_fits_and_total_variable_capacity_is_enforced(self):
        scope = replace(ACK.identity.scope, repository_id=2**63 - 1, environment="e" * 63,
                        tenancy_id="t" * 256, compartment_id="c" * 256, region="r" * 64)
        identity = replace(ACK.identity, scope=scope, bucket_id="b" * 256)
        receipt = replace(RECEIPT, identity=identity)
        ack = replace(ACK, identity=identity, request=replace(ACK.request, scope=scope,
                      namespace="n" * 256, name="b" * 256, principal="p" * 256))
        intent = replace(INTENT, ack=ack, receipt_sha256=sha256(encode_receipt(receipt)).hexdigest(),
                         if_match="x" * 1024)
        body = encode_intent(intent, receipt)
        self.assertLessEqual(len(body), 6000)
        original = merge_receipt({f"v{i}": "" for i in range(246)}, receipt)
        merged = merge_deletion(original, intent)
        self.assertEqual(len(merged), 250)
        self.assertEqual(read_deletion(merged), intent)
        self.assertTrue(all(len(k.encode()) + len(v.encode()) <= 8192 for k, v in merged.items()))
        with self.assertRaises(DeletionIntentError):
            merge_deletion(dict(original, one_more=""), intent)
