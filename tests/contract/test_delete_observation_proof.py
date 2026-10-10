from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from troubleshooter_deployment.bucket_deletion import BucketDeleteIntent, merge_deletion
from troubleshooter_deployment.cleanup import prepare_purge
from troubleshooter_deployment.codec import encode, encode_receipt
from troubleshooter_deployment.policy import bootstrap
from troubleshooter_deployment.receipt_variables import merge_receipt
from troubleshooter_deployment.records import Scope
from tests.contract.oci_responses import CONFIG, SyntheticSigner, response
from tests.contract.test_audit_delete_binding import deletion_event
from tests.contract.test_frozen_delete_proof import LEGACY
from tests.contract.test_oci_audit import START, END, page
from tests.contract.test_oci_receipt import stack
from tests.contract.test_oci_recovery_bucket import REQUEST
from tests.live import proof_flow, proof_worker
from tests.live.proof_stack import StackError
from tests.live.proof_state import ProofStateError, ProofStore, new_state
from tests.live.recovery_proof import audit_diagnostic_exits


def saved_deletion():
    scope = Scope(1, "recovery-proof", CONFIG["tenancy"], REQUEST.scope.compartment_id, CONFIG["region"])
    state = new_state(scope, REQUEST.principal, REQUEST.namespace)
    state.update(started=START.isoformat(), stack_status="acknowledged", stack_id="synthetic-stack",
                 bucket_status="delete_uncertain", bucket_id="synthetic-bucket", receipt_status="confirmed",
                 audit_counts=[1, 1], audit_window=[START.isoformat(), END.isoformat()])
    ack = proof_flow.acknowledged_bucket(state)
    record = prepare_purge(bootstrap(ack.identity, ack.identity, proof_flow.PROOF_ATTEMPT), state["removal"])
    state["record"] = encode(record).decode("ascii")
    intent = BucketDeleteIntent(ack, state["removal"], sha256(encode_receipt(record.receipt)).hexdigest(),
                                state["delete_correlation"], '"saved-revision"')
    return state, record, intent


class DeleteObservationFlowTests(unittest.TestCase):
    def test_valid_deleted_or_uncertain_bucket_never_changes_saved_state(self):
        for status in ("deleted", "delete_uncertain"):
            state, record, intent = saved_deletion()
            state["bucket_status"] = status
            previous = deepcopy(state)
            services = Mock()
            services.audit_deletion.return_value = Mock(matches=(object(),))
            self.assertEqual(proof_flow.observe_deletion(state, services, search_start=START,
                                                        search_minutes=30), 1)
            self.assertEqual(state, previous)
            services.audit_deletion.assert_called_once_with(state, intent.ack, record.receipt, START, END)
            self.assertEqual(len(services.mock_calls), 1)

    def test_ineligible_state_or_invalid_duration_blocks_all_services(self):
        changes = [(StackError, {"zip_sha256": LEGACY}),
                   (ValueError, {"stack_status": "deleted", "bucket_status": "deleted"}),
                   (ValueError, {"stack_status": "delete_uncertain", "bucket_status": "deleted"}),
                   (ValueError, {"bucket_status": "acknowledged"}), (ValueError, {"receipt_status": "uncertain"}),
                   (ProofStateError, {"record": None, "receipt_status": "new"})]
        for expected, change in changes:
            state, _, _ = saved_deletion()
            state.update(change)
            services = Mock()
            with self.assertRaises(expected):
                proof_flow.observe_deletion(state, services, search_start=START)
            self.assertEqual(services.mock_calls, [])
        for duration in (0, 61, True, None):
            state, _, _ = saved_deletion()
            services = Mock()
            with self.assertRaises(ValueError):
                proof_flow.observe_deletion(state, services, search_minutes=duration)
            self.assertEqual(services.mock_calls, [])


class DeleteObservationWorkerTests(unittest.TestCase):
    def setUp(self):
        for name in ("object_storage.object_storage_client", "resource_manager.resource_manager_client",
                     "loggingsearch.log_search_client"):
            patch("oci." + name + ".Signer", return_value=SyntheticSigner()).start()
        self.addCleanup(patch.stopall)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name).resolve() / "proof"
        self.state, self.record, self.intent = saved_deletion()
        self.variables = merge_deletion(merge_receipt({}, self.record.receipt), self.intent)

    def worker(self, store, replies):
        argv = ["worker", "observe-delete", "--allow-live", "--profile", "synthetic",
                "--search-start", START.isoformat(), "--search-minutes", "30",
                "--lock-fd", str(os.dup(store.lock_fd))]
        with patch.object(sys, "argv", argv), patch.object(proof_worker, "STATE_PATH", self.path), patch(
                "tests.live.proof_worker.credentials", return_value=CONFIG), patch(
                "oci.base_client.OCIHTTPAdapter.send", side_effect=replies) as send, patch.object(
                ProofStore, "write", side_effect=AssertionError("observation must not write")):
            code = proof_worker.main()
        return code, [call.args[0] for call in send.call_args_list]

    def test_worker_reads_retained_intent_and_observes_without_mutation_or_state_write(self):
        with ProofStore(self.path) as store:
            store.write(self.state)
            original = (self.path / "state.json").read_bytes()
            for matches, expected in ((0, 3), (1, 0), (2, 4)):
                replies = [response(stack(self.variables)), page([deletion_event(self.intent)] * matches)]
                code, calls = self.worker(store, replies)
                self.assertEqual(code, expected)
                self.assertEqual([(c.method, urlsplit(c.url).path) for c in calls],
                                 [("GET", "/20180917/stacks/synthetic-stack"), ("POST", "/20190909/search")])
                self.assertEqual((self.path / "state.json").read_bytes(), original)
                self.assertTrue(all(reply.raw.closed for reply in replies))
            code, calls = self.worker(store, [response(stack(self.variables)), response(body=b"secret")])
            self.assertEqual(code, audit_diagnostic_exits()["json_invalid"])
            self.assertEqual([c.method for c in calls], ["GET", "POST"])
            self.assertEqual((self.path / "state.json").read_bytes(), original)

    def test_missing_mismatched_or_unreadable_intent_prevents_search_and_preserves_state(self):
        receipt_vars = merge_receipt({}, self.record.receipt)
        wrong_ack = replace(self.intent.ack, request=replace(self.intent.ack.request, name="other"))
        variables = [receipt_vars,
                     merge_deletion(receipt_vars, replace(self.intent, correlation="f" * 32)),
                     merge_deletion(receipt_vars, replace(self.intent, ack=wrong_ack)),
                     merge_receipt({}, replace(self.record.receipt, removal_id="f" * 32))]
        replies = [response(stack(v)) for v in variables]
        replies += [response(stack(self.variables, id="other")), response({"code": "Denied"}, status=403)]
        with ProofStore(self.path) as store:
            store.write(self.state)
            original = (self.path / "state.json").read_bytes()
            for reply in replies:
                code, calls = self.worker(store, [reply])
                self.assertEqual(code, 1)
                self.assertEqual([c.method for c in calls], ["GET"])
                self.assertEqual((self.path / "state.json").read_bytes(), original)
