from dataclasses import replace
from hashlib import sha256
import json
import unittest
from unittest.mock import patch

from troubleshooter_deployment.bucket_deletion import BucketDeleteIntent, KEY, merge_deletion
from troubleshooter_deployment.codec import encode_receipt
from troubleshooter_deployment.cleanup import prepare_purge
from troubleshooter_deployment.policy import bootstrap
from troubleshooter_deployment.oci_receipt import OciReceiptStore, ReceiptError, ReceiptConflict, ReceiptUncertain
from troubleshooter_deployment.receipt_variables import merge_receipt
from tests.contract.oci_responses import CONFIG, OCI_IDENTITY, SyntheticSigner, response
from tests.contract.test_oci_recovery_bucket import ACK
from tests.contract.test_oci_receipt import STACK_ID, stack
from tests.deployment.fixtures import ATTEMPT, REMOVAL


class DeletionReceiptTests(unittest.TestCase):
    def setUp(self):
        patch("oci.resource_manager.resource_manager_client.Signer", return_value=SyntheticSigner()).start()
        self.addCleanup(patch.stopall)
        self.store = OciReceiptStore(CONFIG, OCI_IDENTITY, STACK_ID)
        self.addCleanup(self.store.close)
        self.send = patch("oci.base_client.OCIHTTPAdapter.send").start()
        self.record = prepare_purge(bootstrap(OCI_IDENTITY, OCI_IDENTITY, ATTEMPT), REMOVAL)
        self.intent = BucketDeleteIntent(ACK, REMOVAL, sha256(encode_receipt(self.record.receipt)).hexdigest(),
                                         "b" * 32, '"bucket-revision"')
        self.original = merge_receipt({"label": "日本語"}, self.record.receipt)
        self.variables = merge_deletion(self.original, self.intent)

    def test_conditional_update_then_independent_exact_readback(self):
        self.send.side_effect = [response(stack(self.original)), response(stack(self.variables)),
                                 response(stack(self.variables))]
        self.assertEqual(self.store.persist_deletion(self.record, self.intent), self.intent)
        calls = [c.args[0] for c in self.send.call_args_list]
        self.assertEqual([c.method for c in calls], ["GET", "PUT", "GET"])
        self.assertEqual(calls[1].headers["if-match"], '"revision-1"')
        self.assertEqual(json.loads(calls[1].body), {"variables": self.variables})

    def test_lost_put_reconciles_from_get_without_second_put(self):
        self.send.side_effect = [response(stack(self.original)), OSError("secret"),
                                 response(stack(self.variables)), response(stack(self.variables))]
        with self.assertRaises(ReceiptUncertain):
            self.store.persist_deletion(self.record, self.intent)
        self.assertEqual(self.store.read_deletion(self.record.receipt), self.intent)
        self.assertEqual(self.store.persist_deletion(self.record, self.intent), self.intent)
        self.assertEqual([c.args[0].method for c in self.send.call_args_list], ["GET", "PUT", "GET", "GET"])

    def test_absent_intent_is_not_absent_receipt_and_invalid_inputs_do_not_dispatch(self):
        self.send.side_effect = [response(stack(self.original))]
        self.assertIsNone(self.store.read_deletion(self.record.receipt))
        for variables in ({}, merge_receipt({}, replace(self.record.receipt, removal_id="f" * 32))):
            self.send.side_effect = [response(stack(variables))]
            with self.assertRaises(ReceiptError):
                self.store.read_deletion(self.record.receipt)
        self.send.reset_mock()
        for receipt in (None, True, replace(self.record.receipt, identity=replace(OCI_IDENTITY, bucket_id="other"))):
            with self.assertRaises(ReceiptError):
                self.store.read_deletion(receipt)
        for intent in (None, True, replace(self.intent, receipt_sha256="0" * 64)):
            with self.assertRaises(ReceiptError):
                self.store.persist_deletion(self.record, intent)
        self.send.assert_not_called()

    def test_conflict_missing_receipt_and_capacity_never_overwrite(self):
        cases = [{}, merge_deletion(self.original, replace(self.intent, if_match='"other"')),
                 dict(self.original, **{f"v{i}": "" for i in range(246)}),
                 dict(self.original, **{f"v{i}": "a" * 4015 for i in range(65)})]
        for variables in cases:
            self.send.reset_mock()
            self.send.side_effect = [response(stack(variables))]
            with self.assertRaises(ReceiptError):
                self.store.persist_deletion(self.record, self.intent)
            self.assertEqual(self.send.call_count, 1)

    def test_failed_or_changed_readback_never_returns_deletion_authority(self):
        for after in (OSError("secret"), response(stack(self.original)),
                      response(stack(dict(self.variables, label="changed"))),
                      response(stack(merge_deletion(self.original, replace(self.intent, if_match='"other"')))),
                      response({"code": "NotFound"}, status=404)):
            self.send.reset_mock()
            self.send.side_effect = [response(stack(self.original)), response(stack(self.variables)), after]
            with self.assertRaises(ReceiptUncertain):
                self.store.persist_deletion(self.record, self.intent)
            self.assertEqual(self.send.call_count, 3)
        for status in (412, 429, 500):
            self.send.reset_mock()
            self.send.side_effect = [response(stack(self.original)), response({"code": "Failure"}, status=status)]
            with self.assertRaises(ReceiptConflict if status == 412 else ReceiptUncertain):
                self.store.persist_deletion(self.record, self.intent)
            self.assertEqual(self.send.call_count, 2)

    def test_ordinary_receipt_read_rejects_invalid_deletion_carrier(self):
        self.send.side_effect = [response(stack(dict(self.variables, **{KEY: "{}"})))]
        with self.assertRaises(ReceiptError):
            self.store.read()
