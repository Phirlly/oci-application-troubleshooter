from dataclasses import asdict, replace
from hashlib import sha256
import json
import unittest

from troubleshooter_deployment.codec import decode_receipt, encode_receipt, RecordError
from troubleshooter_deployment.receipt_variables import (
    ReceiptVariablesError, merge_receipt, read_receipt,
)
from troubleshooter_deployment.records import Receipt
from tests.deployment.fixtures import ATTEMPT, GENERATION, IDENTITY, REMOVAL

PREFIX = "troubleshooter_cleanup_receipt_"
RECEIPT = Receipt(IDENTITY, ATTEMPT, REMOVAL, (GENERATION,))


class ReceiptVariablesTests(unittest.TestCase):
    def test_receipt_roundtrip_and_unchanged_unrelated_variables(self):
        original = {"region": "us-ashburn-1", "label": "日本語"}
        self.assertIsNone(read_receipt(original))
        variables = merge_receipt(original, RECEIPT)
        self.assertEqual(decode_receipt(encode_receipt(RECEIPT)), RECEIPT)
        self.assertEqual(read_receipt(variables), RECEIPT)
        self.assertEqual(merge_receipt(variables, RECEIPT), variables)
        self.assertEqual({k: variables[k] for k in original}, original)
        self.assertEqual(original, {"region": "us-ashburn-1", "label": "日本語"})
        self.assertEqual(len(variables), 5)

    def test_maximal_receipt_fits_and_preserves_all_256_generations(self):
        scope = replace(IDENTITY.scope, repository_id=2**63 - 1, environment="e" * 63,
                        tenancy_id="t" * 256, compartment_id="c" * 256, region="r" * 64)
        identity = replace(IDENTITY, scope=scope, bucket_id="b" * 256)
        attempt = replace(ATTEMPT, run_id=2**63 - 1, number=2**63 - 1)
        receipt = Receipt(identity, attempt, REMOVAL, tuple(f"{i:032x}" for i in range(256)))
        body = encode_receipt(receipt)
        self.assertGreater(len(body), 8960)
        self.assertLessEqual(len(body), 12000)
        variables = merge_receipt({}, receipt)
        self.assertEqual(len(variables[PREFIX + "0"]), 6000)
        self.assertGreater(len(variables[PREFIX + "1"]), 0)
        self.assertTrue(all(len(k.encode()) + len(v.encode()) <= 8192 for k, v in variables.items()))
        self.assertEqual(read_receipt(variables), receipt)

    def test_closed_canonical_receipt_rejects_wrong_types_duplicates_and_overflow(self):
        data = {"schema": 1, "receipt": asdict(RECEIPT)}
        invalid = [b"{}", b"x" * 12001, "not-bytes", b'{"schema":1,"schema":1}',
                   json.dumps(data).encode(), encode_receipt(RECEIPT) + b"\n"]
        for changed in [dict(data, schema=True), dict(data, schema=2), dict(data, extra=1),
                        dict(data, receipt=dict(data["receipt"], generation_ids=[GENERATION, GENERATION])),
                        dict(data, receipt=dict(data["receipt"], removal_id=True))]:
            invalid.append(json.dumps(changed, sort_keys=True, separators=(",", ":")).encode())
        for body in invalid:
            with self.subTest(body=str(body)[:60]), self.assertRaises(RecordError):
                decode_receipt(body)
        for receipt in [True, replace(RECEIPT, generation_ids=(GENERATION, GENERATION)),
                        replace(RECEIPT, generation_ids=tuple(f"{i:032x}" for i in range(257))),
                        replace(RECEIPT, attempt=replace(ATTEMPT, run_id=True))]:
            with self.assertRaises(RecordError):
                encode_receipt(receipt)

    def test_partial_unknown_corrupt_and_noncanonical_carriers_fail_closed(self):
        good = merge_receipt({}, RECEIPT)
        cases = [{k: v for k, v in good.items() if k != missing} for missing in good]
        cases += [dict(good, **{PREFIX + "unexpected": "x"}),
                  {PREFIX + "unexpected": "x"},
                  dict(good, **{PREFIX + "sha256": "0" * 64}),
                  dict(good, **{PREFIX + "0": "x" * 6001}),
                  dict(good, **{PREFIX + "1": "x"}),
                  dict(good, **{PREFIX + "0": good[PREFIX + "0"][:-1],
                                PREFIX + "1": good[PREFIX + "0"][-1:]}),
                  {PREFIX + "0": "{}", PREFIX + "1": "", PREFIX + "sha256": sha256(b"{}").hexdigest()}]
        for variables in cases:
            with self.subTest(keys=tuple(variables)), self.assertRaises(ReceiptVariablesError):
                read_receipt(variables)
            with self.assertRaises(ReceiptVariablesError):
                merge_receipt(variables, RECEIPT)

    def test_conflicting_receipt_cannot_be_overwritten(self):
        variables = merge_receipt({}, RECEIPT)
        with self.assertRaises(ReceiptVariablesError):
            merge_receipt(variables, replace(RECEIPT, removal_id="9" * 32))

    def test_provider_capacity_and_utf8_limits(self):
        allowed = {f"v{i}": "" for i in range(247)}
        self.assertEqual(len(merge_receipt(allowed, RECEIPT)), 250)
        with self.assertRaises(ReceiptVariablesError):
            merge_receipt({f"v{i}": "" for i in range(248)}, RECEIPT)
        self.assertIsNone(read_receipt({"x": "é" * 4095 + "a"}))
        for variables in [None, [], {1: "x"}, {"": "x"}, {"x": None}, {"x": True},
                          {"x": "é" * 4096}, {"x": "\ud800"},
                          {f"v{i}": "" for i in range(251)}]:
            with self.subTest(variables=str(variables)[:40]), self.assertRaises(ReceiptVariablesError):
                read_receipt(variables)
