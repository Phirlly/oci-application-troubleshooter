from dataclasses import replace
from hashlib import sha256
import unittest
from unittest.mock import Mock, patch

from troubleshooter_deployment.bucket_deletion import BucketDeleteIntent, KEY
from troubleshooter_deployment.codec import encode_receipt
from troubleshooter_deployment.oci_receipt import ReceiptUncertain
from troubleshooter_deployment.oci_recovery_bucket import BucketError
from tests.live.proof_services import ProofServices
from tests.live.proof_stack import ProofStack, StackError, fixture_zip
from tests.live.proof_flow import advance, observe
from tests.live.proof_state import new_state, validate_state
from tests.contract.test_recovery_proof import MemoryProof, Services
from tests.contract.test_oci_recovery_bucket import ACK
from tests.contract.oci_responses import CONFIG, OCI_IDENTITY
from tests.deployment.fixtures import ATTEMPT, REMOVAL
from troubleshooter_deployment.cleanup import prepare_purge
from troubleshooter_deployment.policy import bootstrap

LEGACY = "98748d87f14e9a5a9dc351aabbb41176ad144f1850d66528b78c8229d54b586f"


class FrozenProofFlowTests(unittest.TestCase):
    def setUp(self):
        self.state = new_state(ACK.request.scope, ACK.request.principal, ACK.request.namespace)
        self.store = MemoryProof(self.state)
        self.services = Services(self.store)

    def test_failed_intent_persistence_prevents_delete_and_state_transition(self):
        self.services.fail = "persist_deletion"
        with self.assertRaises(RuntimeError):
            advance(self.store, self.state, self.services, cleanup=False)
        self.assertEqual(self.store.state["bucket_status"], "acknowledged")
        self.assertNotIn("delete_bucket", self.services.calls)

    def test_failed_uncertainty_save_prevents_delete_after_intent_readback(self):
        write = self.store.write
        def fail_before_delete(state):
            if state["bucket_status"] == "delete_uncertain":
                raise OSError("disk")
            write(state)
        with patch.object(self.store, "write", side_effect=fail_before_delete):
            with self.assertRaises(OSError):
                advance(self.store, self.state, self.services, cleanup=False)
        self.assertIn("persist_deletion", self.services.calls)
        self.assertNotIn("delete_bucket", self.services.calls)
        self.assertEqual(self.store.state["bucket_status"], "acknowledged")

    def test_legacy_record_remains_observable_but_cannot_advance(self):
        advance(self.store, self.state, self.services, cleanup=False)
        self.state["zip_sha256"] = LEGACY
        validate_state(self.state)
        previous = len(self.services.calls)
        observe(self.store, self.state, self.services)
        self.assertEqual(self.services.calls[previous:], ["audit", "audit"])
        for cleanup in (False, True):
            previous = len(self.services.calls)
            with self.assertRaises(StackError):
                advance(self.store, self.state, self.services, cleanup=cleanup)
            self.assertEqual(len(self.services.calls), previous)
        stack = object.__new__(ProofStack)
        stack.client = Mock()
        with self.assertRaises(StackError):
            stack.create(self.state)
        stack.client.assert_not_called()
        self.assertEqual(stack.client.mock_calls, [])
        self.assertIn(KEY.encode(), fixture_zip())
        self.assertNotEqual(sha256(fixture_zip()).hexdigest(), LEGACY)


class ProofDeletionServicesTests(unittest.TestCase):
    def setUp(self):
        self.record = prepare_purge(bootstrap(OCI_IDENTITY, OCI_IDENTITY, ATTEMPT), REMOVAL)
        self.intent = BucketDeleteIntent(ACK, REMOVAL, sha256(encode_receipt(self.record.receipt)).hexdigest(),
                                         "b" * 32, '"v1"')
        self.services = ProofServices(CONFIG, OCI_IDENTITY.scope)
        self.services.bucket = Mock()
        self.services.bucket.prepare_delete.return_value = self.intent
        self.state = {"stack_id": "synthetic-stack", "delete_correlation": "b" * 32}
        self.store = patch("tests.live.proof_services.OciReceiptStore").start().return_value
        self.addCleanup(patch.stopall)

    def test_lost_put_then_readback_uses_original_intent_without_preparing_again(self):
        self.store.read_deletion.side_effect = [None, self.intent]
        self.store.persist_deletion.side_effect = ReceiptUncertain()
        with self.assertRaises(ReceiptUncertain):
            self.services.prepare_deletion(self.state, ACK, self.record, self.record.receipt)
        self.assertEqual(self.services.prepare_deletion(self.state, ACK, self.record, self.record.receipt),
                         self.intent)
        self.services.bucket.prepare_delete.assert_called_once()
        self.store.persist_deletion.assert_called_once_with(self.record, self.intent)
        self.assertEqual(self.store.close.call_count, 2)

    def test_wrong_saved_ack_or_correlation_is_not_used_or_replaced(self):
        for intent in (replace(self.intent, correlation="f" * 32),
                       replace(self.intent, ack=replace(ACK, request=replace(ACK.request, name="other")))):
            self.store.read_deletion.return_value = intent
            with self.assertRaises(BucketError):
                self.services.prepare_deletion(self.state, ACK, self.record, self.record.receipt)
        self.services.bucket.prepare_delete.assert_not_called()
        self.store.persist_deletion.assert_not_called()
