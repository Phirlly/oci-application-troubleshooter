from dataclasses import replace
from unittest.mock import patch

from troubleshooter_deployment.cleanup import prepare_purge
from troubleshooter_deployment.codec import decode, encode
from troubleshooter_deployment.oci_owned_stack import OwnedStackObservation
from troubleshooter_deployment.policy import begin_generation, reserve
from troubleshooter_deployment.runner_bootstrap import BootstrapPending
from troubleshooter_deployment.runner_stack_journal import StackJournalReady
from troubleshooter_deployment.stack_journal_variables import merge_journal, read_journal
from tests.contract.stack_journal_service import StackJournalService
from tests.deployment.fixtures import ATTEMPT, NEXT_ATTEMPT, GENERATION, SHA, OPERATION, REMOVAL


class RunnerStackJournalTests(StackJournalService):
    def test_empty_journal_handover_then_same_release_resumes_evolved_history(self):
        original = self.run_bootstrap()
        later = replace(ATTEMPT, run_id=200, source_sha="b" * 40, workflow_sha="b" * 40)
        result = self.run_bootstrap(later)
        self.assertIsInstance(result, StackJournalReady)
        self.assertEqual(result.snapshot.record, replace(original.snapshot.record, owner=later))
        record = begin_generation(result.snapshot.record, GENERATION, SHA)
        evolved, _ = reserve(record, GENERATION, OPERATION, "provision")
        self.stack["variables"] = merge_journal(self.stack["variables"], encode(evolved))
        self.stack_version += 1
        retry = replace(later, number=2)
        resumed = self.run_bootstrap(retry)
        self.assertIsInstance(resumed, StackJournalReady)
        self.assertEqual(resumed.snapshot.record, replace(evolved, owner=retry))

    def test_changed_source_or_workflow_cannot_take_populated_or_closed_journal(self):
        original = self.run_bootstrap().snapshot.record
        populated = begin_generation(original, GENERATION, SHA)
        uncertain, _ = reserve(populated, GENERATION, OPERATION, "provision")
        for record in (populated, uncertain, prepare_purge(original, REMOVAL)):
            for field in ("source_sha", "workflow_sha"):
                with self.subTest(record=record, field=field):
                    self.stack["variables"] = merge_journal(self.stack["variables"], encode(record))
                    self.calls.clear()
                    later = replace(ATTEMPT, run_id=200, **{field: "b" * 40})
                    self.assertIsInstance(self.run_bootstrap(later), BootstrapPending)
                    self.assertEqual(self.mutations(), [])
                    self.assertEqual(decode(read_journal(self.stack["variables"])), record)

    def test_cross_release_takeover_requires_completed_owner_and_cas(self):
        original = self.run_bootstrap().snapshot.record
        later = replace(ATTEMPT, run_id=200, source_sha="b" * 40, workflow_sha="b" * 40)
        self.statuses[(ATTEMPT.run_id, ATTEMPT.number)] = "in_progress"
        self.calls.clear()
        self.assertIsInstance(self.run_bootstrap(later), BootstrapPending)
        self.assertEqual(self.mutations(), [])
        self.statuses.clear()
        self.fault = "conflict"
        self.assertIsInstance(self.run_bootstrap(later), BootstrapPending)
        self.assertEqual(decode(read_journal(self.stack["variables"])), original)
        self.fault = "after"
        self.assertIsInstance(self.run_bootstrap(later), BootstrapPending)
        self.assertEqual(decode(read_journal(self.stack["variables"])), replace(original, owner=later))
        self.fault = None
        self.calls.clear()
        self.assertIsInstance(self.run_bootstrap(later), StackJournalReady)
        self.assertEqual(self.mutations(), [])

    def test_one_invocation_is_ready_without_bucket_or_manual_second_run(self):
        result = self.run_bootstrap()
        self.assertIsInstance(result, StackJournalReady)
        self.assertEqual(result.snapshot.record.owner, ATTEMPT)
        self.assertEqual(len(self.mutations()), 1)
        self.assertIsNone(self.bucket)
        self.assertNotIn("synthetic", repr(result))
        self.calls.clear()
        self.assertEqual(self.run_bootstrap(), result)
        self.assertEqual(self.mutations(), [])

    def test_completed_original_initializes_then_transfers_to_new_attempt(self):
        self.anchor = OwnedStackObservation(self.initial, self.anchor.stack_id, '"old"')
        result = self.run_bootstrap(NEXT_ATTEMPT)
        self.assertIsInstance(result, StackJournalReady)
        self.assertEqual(result.snapshot.record.owner, NEXT_ATTEMPT)
        self.assertEqual(len(self.mutations()), 2)

    def test_unfinished_original_current_or_different_release_never_mutates(self):
        self.statuses[(100, 1)] = "in_progress"
        self.assertIsInstance(self.run_bootstrap(NEXT_ATTEMPT), BootstrapPending)
        self.statuses[(100, 1)] = "completed"
        self.assertIsInstance(self.run_bootstrap(), BootstrapPending)
        self.statuses.clear()
        self.assertIsInstance(self.run_bootstrap(replace(NEXT_ATTEMPT, source_sha="b" * 40)), BootstrapPending)
        self.assertEqual(self.mutations(), [])
        self.anchor = BootstrapPending("saved_reason")
        self.assertEqual(self.run_bootstrap(), self.anchor)

    def test_failed_or_lost_initial_put_resumes_using_only_conditional_updates(self):
        for fault in ("before", "after"):
            # Separate synthetic service state per subcase.
            self.fault = fault
            self.assertIsInstance(self.run_bootstrap(), BootstrapPending)
            self.fault = None
            result = self.run_bootstrap(NEXT_ATTEMPT)
            self.assertIsInstance(result, StackJournalReady)
            self.assertEqual(result.snapshot.record.owner, NEXT_ATTEMPT)
            self.assertTrue(all(r.method == "PUT" for r in self.mutations()))
            self.stack["variables"] = {k: v for k, v in self.stack["variables"].items()
                                       if not k.startswith("troubleshooter_journal_")}
            self.stack["freeformTags"].pop("troubleshooter_journal_protocol")

    def test_evolved_history_uncertain_operation_and_closed_receipt_are_preserved(self):
        result = self.run_bootstrap()
        record = begin_generation(result.snapshot.record, GENERATION, SHA)
        evolved, _ = reserve(record, GENERATION, OPERATION, "provision")
        self.stack["variables"] = merge_journal(self.stack["variables"], encode(evolved))
        self.stack_version += 1
        self.jobs = [{"id": "historical-job"}]
        result = self.run_bootstrap(NEXT_ATTEMPT)
        self.assertEqual(result.snapshot.record.operations, evolved.operations)
        self.assertEqual(result.snapshot.record.generations, evolved.generations)
        # A separate closed empty record proves takeover cannot reopen its receipt.
        closed = prepare_purge(replace(evolved, generations=(), operations=()), REMOVAL)
        self.stack["variables"] = merge_journal(self.stack["variables"], encode(closed))
        result = self.run_bootstrap(NEXT_ATTEMPT)
        self.assertEqual(result.snapshot.record.receipt, closed.receipt)

    def test_lost_takeover_ack_is_read_on_next_invocation_without_rewriting(self):
        self.run_bootstrap()
        self.fault = "after"
        self.assertIsInstance(self.run_bootstrap(NEXT_ATTEMPT), BootstrapPending)
        self.assertEqual(decode(read_journal(self.stack["variables"])).owner, NEXT_ATTEMPT)
        self.fault = None
        self.calls.clear()
        self.assertIsInstance(self.run_bootstrap(NEXT_ATTEMPT), StackJournalReady)
        self.assertEqual(self.mutations(), [])

    def test_current_attempt_stopping_after_read_prevents_initialization_or_takeover(self):
        original = self.send

        def stopped(request, **kwargs):
            result = original(request, **kwargs)
            self.statuses[(self.current.run_id, self.current.number)] = "completed"
            return result

        self.transport.side_effect = stopped
        self.assertIsInstance(self.run_bootstrap(), BootstrapPending)
        self.assertEqual(self.mutations(), [])
        self.statuses.clear()
        self.transport.side_effect = original
        self.run_bootstrap()
        self.calls.clear()
        self.transport.side_effect = stopped
        self.assertIsInstance(self.run_bootstrap(NEXT_ATTEMPT), BootstrapPending)
        self.assertEqual(self.mutations(), [])

    def test_changed_takeover_readback_cannot_return_ready(self):
        self.run_bootstrap()
        original = self.send

        def competing(request, **kwargs):
            if request.method == "GET" and sum(r.method == "PUT" for r in self.mutations()) >= 2:
                saved = decode(read_journal(self.stack["variables"]))
                self.stack["variables"] = merge_journal(self.stack["variables"],
                    encode(replace(saved, owner=replace(NEXT_ATTEMPT, run_id=300))))
            return original(request, **kwargs)

        self.transport.side_effect = competing
        self.assertIsInstance(self.run_bootstrap(NEXT_ATTEMPT), BootstrapPending)
