from dataclasses import replace
import json
from unittest.mock import patch

from troubleshooter_deployment.codec import decode, encode
from troubleshooter_deployment.oci_owned_stack import OwnedStackObservation
from troubleshooter_deployment.policy import PolicyError, begin_generation, reserve
from troubleshooter_deployment.records import Receipt
from troubleshooter_deployment.receipt_variables import merge_receipt
from troubleshooter_deployment.runner_bootstrap import BootstrapPending
from troubleshooter_deployment.runner_recovery_store import RecoveryStoreReady
from tests.contract.recovery_store_service import RecoveryStoreService
from tests.contract.test_oci_stack_bootstrap import STACK_ID
from tests.deployment.fixtures import ATTEMPT, NEXT_ATTEMPT, GENERATION, OPERATION, REMOVAL, SHA
from tests.deployment.test_bucket_bootstrap import NOW


class RunnerRecoveryStoreTests(RecoveryStoreService):
    def test_one_invocation_enrolls_creates_initializes_and_returns_confirmed_store(self):
        result = self.run_bootstrap()
        self.assertIsInstance(result, RecoveryStoreReady)
        self.initial_call.assert_called_once()
        self.assertEqual(result.snapshot.record.owner, ATTEMPT)
        self.assertEqual(result.snapshot.record, decode(self.body))
        self.assertEqual(self.state().phase, "ready")
        mutations = self.mutations()
        self.assertEqual([r.method for r in mutations], ["PUT", "POST", "PUT", "PUT", "PUT"])
        self.assertEqual(mutations[3].headers["if-none-match"], "*")
        self.assertEqual(self.stack["variables"]["unrelated"], "preserve")
        self.assertNotIn("synthetic", repr(result))

    def test_owned_observation_also_continues_without_second_setup_run(self):
        self.anchor = OwnedStackObservation(self.initial, STACK_ID, '"old"')
        self.assertIsInstance(self.run_bootstrap(NEXT_ATTEMPT), RecoveryStoreReady)
        self.assertEqual(decode(self.body).owner, NEXT_ATTEMPT)
        self.assertEqual(self.state().attempt, NEXT_ATTEMPT)
        self.assertTrue(any("/attempts/1?" in c[2] for c in self.calls if c[0] == "github"))

    def test_unfinished_original_or_current_attempt_and_pending_anchor_never_mutate(self):
        self.statuses[(100, 1)] = "in_progress"
        self.assertIsInstance(self.run_bootstrap(NEXT_ATTEMPT), BootstrapPending)
        self.assertEqual(self.mutations(), [])
        self.statuses[(100, 1)] = "completed"
        self.assertIsInstance(self.run_bootstrap(), BootstrapPending)
        self.assertEqual(self.mutations(), [])
        self.anchor = BootstrapPending("saved_reason")
        self.assertEqual(self.run_bootstrap(), self.anchor)

    def lost_marker(self, phase):
        self.fault = phase + "_after"
        self.assertIsInstance(self.run_bootstrap(), RecoveryStoreReady)
        self.assertEqual(sum(r.method == "POST" for r in self.mutations()), 1)

    def test_lost_intent_write_reply_confirms_in_same_invocation(self):
        self.lost_marker("create_uncertain")

    def test_lost_identity_write_reply_confirms_in_same_invocation(self):
        self.lost_marker("journal_initializing")

    def test_lost_ready_write_reply_confirms_in_same_invocation(self):
        self.lost_marker("ready")

    def test_unaccepted_enrollment_can_retry_but_persisted_intent_never_redispatches(self):
        self.fault = "create_uncertain_before"
        self.assertIsInstance(self.run_bootstrap(), BootstrapPending)
        self.assertIsNone(self.state())
        self.assertIsNone(self.bucket)
        self.fault = "bucket_before"
        self.assertIsInstance(self.run_bootstrap(NEXT_ATTEMPT), BootstrapPending)
        self.assertEqual(self.state().phase, "create_uncertain")
        self.fault, self.audit_count = None, 0
        before = len(self.mutations())
        self.assertIsInstance(self.run_bootstrap(NEXT_ATTEMPT), BootstrapPending)
        self.assertIsInstance(self.run_bootstrap(replace(NEXT_ATTEMPT, number=3)), BootstrapPending)
        self.assertEqual(len(self.mutations()), before)

    def test_lost_create_reconciles_original_digest_and_window_without_second_post(self):
        self.fault = "bucket_after"
        self.assertIsInstance(self.run_bootstrap(), BootstrapPending)
        original = self.state()
        self.fault = None
        self.now += 86400
        result = self.run_bootstrap(NEXT_ATTEMPT)
        self.assertIsInstance(result, RecoveryStoreReady)
        self.assertEqual(self.state().started, original.started)
        self.assertEqual(self.state().attempt, ATTEMPT)
        self.assertEqual(result.snapshot.record.owner, NEXT_ATTEMPT)
        self.assertEqual(sum(r.method == "POST" for r in self.mutations()), 1)
        query = next(json.loads(c[3].body) for c in self.calls if c[2].endswith("/search"))
        self.assertEqual(query["timeStart"], original.started)
        self.assertEqual(query["timeEnd"], "2026-10-11T13:00:00Z")

    def test_missing_or_duplicate_audit_stays_pending_without_any_second_create(self):
        self.fault = "bucket_after"
        self.run_bootstrap()
        self.fault = None
        for count in (0, 2):
            self.audit_count = count
            self.assertIsInstance(self.run_bootstrap(NEXT_ATTEMPT), BootstrapPending)
        self.assertEqual(self.state().phase, "create_uncertain")
        self.assertIsNone(self.body)
        self.assertEqual(sum(r.method == "POST" for r in self.mutations()), 1)

    def test_identity_marker_failure_recovers_via_audit_then_initializes(self):
        self.fault = "journal_initializing_before"
        self.assertIsInstance(self.run_bootstrap(), BootstrapPending)
        self.assertEqual(self.state().phase, "create_uncertain")
        self.fault = None
        self.assertIsInstance(self.run_bootstrap(NEXT_ATTEMPT), RecoveryStoreReady)
        self.assertEqual(sum(r.method == "POST" for r in self.mutations()), 1)

    def test_unaccepted_initialization_reuses_original_owner_and_conditional_bytes(self):
        self.fault = "object_before"
        self.assertIsInstance(self.run_bootstrap(), BootstrapPending)
        original_put = self.mutations()[-1]
        self.assertEqual(self.state().phase, "journal_initializing")
        self.fault = None
        self.assertIsInstance(self.run_bootstrap(NEXT_ATTEMPT), RecoveryStoreReady)
        initial_puts = [r for r in self.mutations() if "if-none-match" in r.headers]
        self.assertEqual(len(initial_puts), 2)
        self.assertEqual(initial_puts[1].body, original_put.body)
        self.assertEqual(decode(original_put.body).owner, ATTEMPT)

    def test_lost_initialization_reply_is_confirmed_without_reset(self):
        self.fault = "object_after"
        self.assertIsInstance(self.run_bootstrap(), RecoveryStoreReady)
        self.assertEqual(sum("if-none-match" in r.headers for r in self.mutations()), 1)

    def test_ready_marker_interruption_reconciles_initial_object_412(self):
        self.fault = "ready_before"
        self.assertIsInstance(self.run_bootstrap(), BootstrapPending)
        original = self.body
        self.fault = None
        self.assertIsInstance(self.run_bootstrap(NEXT_ATTEMPT), RecoveryStoreReady)
        initial_puts = [r for r in self.mutations() if "if-none-match" in r.headers]
        self.assertEqual([r.body for r in initial_puts], [original, original])

    def test_ready_allows_jobs_and_preserves_evolved_record_and_uncertain_operation(self):
        self.assertIsInstance(self.run_bootstrap(), RecoveryStoreReady)
        record = begin_generation(decode(self.body), GENERATION, SHA)
        evolved, _ = reserve(record, GENERATION, OPERATION, "provision")
        self.body = encode(evolved)
        self.object_version += 1
        self.jobs = [{"id": "historical-job"}]
        self.stack["variables"]["workload"] = "changed"
        self.calls.clear()
        result = self.run_bootstrap(NEXT_ATTEMPT)
        self.assertIsInstance(result, RecoveryStoreReady)
        self.assertEqual(result.snapshot.record.operations, evolved.operations)
        self.assertEqual(result.snapshot.record.generations, evolved.generations)
        self.assertEqual(len(self.mutations()), 1)
        self.assertNotIn("if-none-match", self.mutations()[0].headers)
        self.assertFalse(any(c[2].endswith("/jobs") or c[2] == "/n" for c in self.calls))
        self.calls.clear()
        self.assertIsInstance(self.run_bootstrap(NEXT_ATTEMPT), RecoveryStoreReady)
        self.assertEqual(self.mutations(), [])

    def test_evolved_initializing_record_conflicts_without_overwrite(self):
        self.fault = "ready_before"
        self.run_bootstrap()
        self.body = encode(begin_generation(decode(self.body), GENERATION, SHA))
        self.fault = None
        self.assertIsInstance(self.run_bootstrap(NEXT_ATTEMPT), BootstrapPending)
        self.assertEqual(self.state().phase, "journal_initializing")
        self.assertEqual(decode(self.body).generations[0].generation_id, GENERATION)

    def test_ready_missing_object_never_reinitializes(self):
        self.run_bootstrap()
        self.body = None
        self.calls.clear()
        self.assertIsInstance(self.run_bootstrap(NEXT_ATTEMPT), BootstrapPending)
        self.assertEqual(self.mutations(), [])

    def test_lost_takeover_preserves_record_and_next_invocation_does_not_repeat_put(self):
        self.run_bootstrap()
        self.fault = "object_after"
        self.assertIsInstance(self.run_bootstrap(NEXT_ATTEMPT), BootstrapPending)
        self.assertEqual(decode(self.body).owner, NEXT_ATTEMPT)
        self.fault = None
        self.calls.clear()
        self.assertIsInstance(self.run_bootstrap(NEXT_ATTEMPT), RecoveryStoreReady)
        self.assertEqual(self.mutations(), [])

    def test_expired_dispatch_window_never_posts_bucket(self):
        with patch("troubleshooter_deployment.runner_recovery_store.time", side_effect=[NOW, NOW + 3600]):
            self.assertIsInstance(self.run_bootstrap(), BootstrapPending)
        self.assertEqual(self.state().phase, "create_uncertain")
        self.assertIsNone(self.bucket)
        self.assertEqual(len(self.mutations()), 1)

    def test_current_attempt_rechecked_after_enrollment_before_dispatch(self):
        original = self.send

        def stopped(request, **kwargs):
            result = original(request, **kwargs)
            if request.method == "PUT":
                self.statuses[(100, 1)] = "completed"
            return result

        self.transport.side_effect = stopped
        self.assertIsInstance(self.run_bootstrap(), BootstrapPending)
        self.assertIsNone(self.bucket)
        self.assertEqual(len(self.mutations()), 1)

    def test_unconfirmed_original_cannot_initialize(self):
        self.fault = "object_before"
        self.run_bootstrap()
        self.statuses[(100, 1)] = "in_progress"
        self.calls.clear()
        self.fault = None
        self.assertIsInstance(self.run_bootstrap(NEXT_ATTEMPT), BootstrapPending)
        self.assertEqual(self.mutations(), [])

    def test_ready_takeover_preserves_purge_receipt_and_cannot_restart_workload(self):
        self.run_bootstrap()
        record = decode(self.body)
        receipt = Receipt(record.identity, ATTEMPT, REMOVAL, ())
        self.body = encode(replace(record, receipt=receipt))
        self.stack["variables"] = merge_receipt(self.stack["variables"], receipt)
        result = self.run_bootstrap(NEXT_ATTEMPT)
        self.assertIsInstance(result, RecoveryStoreReady)
        self.assertEqual(result.snapshot.record.receipt, receipt)
        with self.assertRaises(PolicyError):
            begin_generation(result.snapshot.record, GENERATION, SHA)

    def test_lost_enrollment_readback_never_grants_dispatch_on_later_invocation(self):
        original = self.send
        enrolled = False

        def unavailable(request, **kwargs):
            nonlocal enrolled
            if enrolled and request.method == "GET":
                raise OSError("synthetic-private-read-failure")
            result = original(request, **kwargs)
            if request.method == "PUT":
                enrolled = True
            return result

        self.transport.side_effect = unavailable
        result = self.run_bootstrap()
        self.assertIsInstance(result, BootstrapPending)
        self.assertNotIn("private", result.reason)
        self.assertIsNone(self.bucket)
        self.transport.side_effect, self.audit_count = original, 0
        self.assertIsInstance(self.run_bootstrap(NEXT_ATTEMPT), BootstrapPending)
        self.assertIsNone(self.bucket)
        self.assertEqual(len(self.mutations()), 1)

    def test_changed_stack_before_initialization_prevents_object_write(self):
        original = self.send
        count = 0

        def changed(request, **kwargs):
            nonlocal count
            result = original(request, **kwargs)
            if request.method == "GET" and "/b/" in request.url:
                count += 1
                if count == 2:
                    self.stack_version += 1
            return result

        self.transport.side_effect = changed
        self.assertIsInstance(self.run_bootstrap(), BootstrapPending)
        self.assertEqual(self.state().phase, "journal_initializing")
        self.assertIsNone(self.body)

    def test_takeover_readback_change_cannot_return_ready_snapshot(self):
        self.run_bootstrap()
        original = self.send

        def competing(request, **kwargs):
            result = original(request, **kwargs)
            if request.method == "PUT" and "/o/" in request.url:
                self.body = encode(replace(decode(self.body), owner=replace(NEXT_ATTEMPT, run_id=300)))
                self.object_version += 1
            return result

        self.transport.side_effect = competing
        self.assertIsInstance(self.run_bootstrap(NEXT_ATTEMPT), BootstrapPending)
        self.assertEqual(decode(self.body).owner.run_id, 300)
