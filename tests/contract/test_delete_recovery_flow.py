from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import unittest
from unittest.mock import Mock, patch

from troubleshooter_deployment.records import DeleteAcknowledgement
from tests.contract.test_delete_observation_proof import saved_deletion
from tests.contract.test_frozen_delete_proof import LEGACY
from tests.contract.test_oci_audit import START, END
from tests.contract.test_recovery_proof import MemoryProof
from tests.live import proof_flow
from tests.live.proof_state import ProofStateError, validate_state


class DeleteRecoveryFlowTests(unittest.TestCase):
    def setUp(self):
        self.state, self.record, self.intent = saved_deletion()
        self.state["delete_window"] = [START.isoformat(), END.isoformat()]
        self.store = MemoryProof(deepcopy(self.state))
        self.services = Mock()
        self.services.reconcile_deletion.return_value = DeleteAcknowledgement(
            self.intent.ack.identity, self.record.receipt.removal_id)
        self.services.read_receipt.return_value = self.record.receipt
        self.clock = patch("tests.live.proof_flow.datetime", wraps=proof_flow.datetime).start()
        self.clock.now.return_value = END + timedelta(days=1)
        self.addCleanup(patch.stopall)

    def recover(self):
        return proof_flow.recover_deletion(self.store, self.state, self.services)

    def test_delayed_recovery_uses_retained_window_and_repeat_only_reads_receipt(self):
        self.assertTrue(self.recover())
        self.services.reconcile_deletion.assert_called_once_with(
            self.state, self.intent.ack, self.record.receipt, START, END)
        self.assertEqual([c[0] for c in self.services.mock_calls], ["reconcile_deletion", "read_receipt"])
        self.assertEqual(self.store.state["bucket_status"], "deleted")
        self.assertEqual(self.store.state["receipt_status"], "survived")
        self.services.reset_mock()
        self.state = deepcopy(self.store.state)
        self.assertTrue(self.recover())
        self.assertEqual([c[0] for c in self.services.mock_calls], ["read_receipt"])

    def test_unconfirmed_or_wrong_identity_preserves_uncertainty(self):
        original = deepcopy(self.state)
        for result in (None, DeleteAcknowledgement(replace(self.intent.ack.identity, bucket_id="replacement"),
                                                   self.record.receipt.removal_id)):
            self.services.reconcile_deletion.return_value = result
            self.assertFalse(self.recover())
            self.assertEqual(self.state, original)
        self.services.read_receipt.assert_not_called()
        self.assertEqual(self.store.saved, [])

    def test_failed_completion_write_stops_before_survival_or_cleanup(self):
        original = deepcopy(self.store.state)
        with patch.object(self.store, "write", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                self.recover()
        self.assertEqual(self.store.state, original)
        self.services.read_receipt.assert_not_called()
        self.services.delete_stack.assert_not_called()

    def test_survival_failure_retains_positive_deletion_for_read_only_retry(self):
        self.services.read_receipt.return_value = None
        self.assertFalse(self.recover())
        self.assertEqual(self.store.state["bucket_status"], "deleted")
        self.assertEqual(self.store.state["receipt_status"], "confirmed")
        self.services.read_receipt.return_value = self.record.receipt
        self.assertTrue(self.recover())
        self.assertEqual(self.services.reconcile_deletion.call_count, 1)
        self.services.delete_stack.assert_not_called()

    def test_initial_minute_waits_without_services_then_caps_query_at_current_minute(self):
        self.clock.now.return_value = START + timedelta(seconds=50)
        self.assertFalse(self.recover())
        self.assertEqual(self.services.mock_calls, [])
        self.clock.now.return_value = START + timedelta(minutes=2, seconds=40)
        self.assertTrue(self.recover())
        self.assertEqual(self.services.reconcile_deletion.call_args.args[-1], START + timedelta(minutes=2))

    def test_ineligible_or_historical_state_cannot_call_services(self):
        for changes in ({"zip_sha256": LEGACY}, {"bucket_status": "acknowledged"},
                        {"stack_status": "deleted", "bucket_status": "deleted"},
                        {"receipt_status": "uncertain"}):
            self.state.update(changes)
            with self.assertRaises((ValueError, RuntimeError)):
                self.recover()
            self.state, _, _ = saved_deletion()
            self.state["delete_window"] = [START.isoformat(), END.isoformat()]
        del self.state["delete_window"]
        with self.assertRaises(ValueError):
            self.recover()
        self.assertEqual(self.services.mock_calls, [])

    def test_optional_window_keeps_old_schema_readable_but_rejects_invalid_extensions(self):
        old, _, _ = saved_deletion()
        validate_state(old)
        validate_state(self.state)
        for window in (None, [], [START.isoformat()] * 2,
                       [START.isoformat(), (START + timedelta(hours=2)).isoformat()],
                       [START.isoformat(), "secret"], [True, False]):
            with self.assertRaises(ProofStateError):
                validate_state(dict(old, delete_window=window))
        with self.assertRaises(ProofStateError):
            validate_state(dict(self.state, unexpected=True))
