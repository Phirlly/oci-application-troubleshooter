from dataclasses import replace
import unittest

from troubleshooter_deployment.records import OperationResult, Resource
from troubleshooter_deployment.policy import (
    PolicyError, bootstrap, begin_generation, begin_removal, claim,
    reserve, resolve_operation,
)
from troubleshooter_deployment.journal import Journal, WriteConflict, WriteUncertain
from troubleshooter_deployment.codec import RecordError
from tests.deployment.fixtures import (
    ATTEMPT, NEXT_ATTEMPT, IDENTITY, GENERATION, SECOND_GENERATION,
    OPERATION, REMOVAL, SHA, MemoryStore,
)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.record = begin_generation(bootstrap(IDENTITY, IDENTITY, ATTEMPT), GENERATION, SHA)

    def test_unknown_bucket_create_cannot_bootstrap(self):
        for observation in [None, replace(IDENTITY, epoch="9" * 32), replace(IDENTITY, bucket_id="same-name-new-bucket")]:
            with self.subTest(observation=observation), self.assertRaises(PolicyError):
                bootstrap(IDENTITY, observation, ATTEMPT)

    def test_same_generation_reuses_frozen_release(self):
        self.assertEqual(begin_generation(self.record, GENERATION, SHA), self.record)
        for generation, release in [(GENERATION, "b" * 40), (SECOND_GENERATION, SHA)]:
            with self.assertRaises(PolicyError):
                begin_generation(self.record, generation, release)

    def test_exact_completed_attempt_required_for_takeover(self):
        for observed, status in [(ATTEMPT, "in_progress"), (ATTEMPT, "unknown"), (NEXT_ATTEMPT, "completed"), (None, "completed")]:
            with self.subTest(status=status), self.assertRaises(PolicyError):
                claim(self.record, NEXT_ATTEMPT, observed, status)
        taken = claim(self.record, NEXT_ATTEMPT, ATTEMPT, "completed")
        self.assertEqual(taken.owner, NEXT_ATTEMPT)
        self.assertEqual(claim(taken, NEXT_ATTEMPT, None, "unknown"), taken)

    def test_uncertain_creation_is_never_reserved_twice(self):
        reserved, decision = reserve(self.record, GENERATION, OPERATION, "provision")
        self.assertEqual(decision, "reserved")
        replay, decision = reserve(reserved, GENERATION, OPERATION, "provision")
        self.assertEqual(replay, reserved)
        self.assertEqual(decision, "reconcile")
        with self.assertRaises(PolicyError):
            reserve(reserved, GENERATION, "6" * 32, "provision")

    def test_takeover_preserves_original_operation_and_reconciliation(self):
        reserved, _ = reserve(self.record, GENERATION, OPERATION, "provision")
        taken = claim(reserved, NEXT_ATTEMPT, ATTEMPT, "completed")
        self.assertEqual(taken.operations[0].attempt, ATTEMPT)
        self.assertEqual(reserve(taken, GENERATION, OPERATION, "provision")[1], "reconcile")

    def test_changed_operation_identity_is_rejected(self):
        reserved, _ = reserve(self.record, GENERATION, OPERATION, "provision")
        with self.assertRaises(PolicyError):
            reserve(reserved, GENERATION, OPERATION, "cleanup")

    def test_running_unknown_or_unreconciled_result_cannot_release_operation(self):
        reserved, _ = reserve(self.record, GENERATION, OPERATION, "provision")
        for outcome, inventory in [("running", True), ("unknown", True), ("succeeded", False)]:
            result = OperationResult(OPERATION, ATTEMPT, outcome, inventory, ())
            self.assertEqual(resolve_operation(reserved, result), reserved)
        with self.assertRaises(PolicyError):
            resolve_operation(reserved, OperationResult(OPERATION, NEXT_ATTEMPT, "succeeded", True, ()))

    def test_terminal_result_records_exact_resources_and_deduplicates(self):
        reserved, _ = reserve(self.record, GENERATION, OPERATION, "provision")
        result = OperationResult(OPERATION, ATTEMPT, "succeeded", True, (Resource("host-1", "host"), Resource("stack-1", "stack")))
        resolved = resolve_operation(reserved, result)
        self.assertEqual(resolved.generations[0].resources, result.resources)
        self.assertEqual(resolve_operation(resolved, result), resolved)
        self.assertEqual(reserve(resolved, GENERATION, OPERATION, "provision")[1], "resolved")

    def test_removing_generation_cannot_be_restarted(self):
        removing = begin_removal(self.record, GENERATION, REMOVAL)
        self.assertEqual(begin_removal(removing, GENERATION, REMOVAL), removing)
        for action in [lambda: begin_generation(removing, GENERATION, SHA), lambda: reserve(removing, GENERATION, OPERATION, "provision"), lambda: begin_removal(removing, GENERATION, "f" * 32)]:
            with self.assertRaises(PolicyError):
                action()

    def test_unrecognized_operations_and_observations_fail_closed(self):
        with self.assertRaises(PolicyError):
            reserve(self.record, GENERATION, OPERATION, "arbitrary-action")
        with self.assertRaises(PolicyError):
            begin_removal(self.record, SECOND_GENERATION, REMOVAL)
        reserved, _ = reserve(self.record, GENERATION, OPERATION, "provision")
        for outcome, resources in [("404", ()), ("succeeded", []), ("succeeded", (Resource("host-1", "host", "terminated"),))]:
            with self.assertRaises(PolicyError):
                resolve_operation(reserved, OperationResult(OPERATION, ATTEMPT, outcome, True, resources))

    def test_resolved_inventory_cannot_be_rewritten_or_enlarged(self):
        reserved, _ = reserve(self.record, GENERATION, OPERATION, "provision")
        result = OperationResult(OPERATION, ATTEMPT, "failed", True, (Resource("host-1", "host"),))
        resolved = resolve_operation(reserved, result)
        for resources in [(Resource("host-1", "secret"),), (Resource("host-2", "host"),)]:
            with self.assertRaises(PolicyError):
                resolve_operation(resolved, replace(result, resources=resources))

    def test_cleanup_reservation_cannot_add_unrelated_resources(self):
        removing = begin_removal(self.record, GENERATION, REMOVAL)
        reserved, _ = reserve(removing, GENERATION, OPERATION, "cleanup")
        with self.assertRaises(PolicyError):
            resolve_operation(reserved, OperationResult(OPERATION, ATTEMPT, "succeeded", True, (Resource("another-host", "host"),)))


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.journal = Journal(self.store, IDENTITY, ATTEMPT)
        self.snapshot = self.journal.initialize(IDENTITY)
        self.snapshot = self.journal.commit(self.snapshot, begin_generation(self.snapshot.record, GENERATION, SHA))

    def test_stale_writer_conflicts_without_overwriting(self):
        first, decision = self.journal.reserve(self.snapshot, GENERATION, OPERATION, "provision")
        self.assertEqual(decision, "reserved")
        with self.assertRaises(WriteConflict):
            self.journal.reserve(self.snapshot, GENERATION, "6" * 32, "provision")
        self.assertEqual(self.journal.load(), first)

    def test_lost_reservation_write_reply_requires_reconciliation_after_restart(self):
        self.store.fail = "after"
        with self.assertRaises(WriteUncertain):
            self.journal.reserve(self.snapshot, GENERATION, OPERATION, "provision")
        self.store.fail = None
        restarted = Journal(self.store, IDENTITY, ATTEMPT)
        loaded = restarted.load()
        _, decision = restarted.reserve(loaded, GENERATION, OPERATION, "provision")
        self.assertEqual(decision, "reconcile")

    def test_write_failure_before_persistence_returns_no_reservation(self):
        self.store.fail = "before"
        with self.assertRaises(WriteUncertain):
            self.journal.reserve(self.snapshot, GENERATION, OPERATION, "provision")
        self.assertEqual(self.journal.load(), self.snapshot)

    def test_missing_or_denied_journal_is_not_fresh_bootstrap(self):
        for store in [MemoryStore(), self.store]:
            if store is self.store:
                store.fail = "read"
            with self.assertRaises((PolicyError, OSError)):
                Journal(store, IDENTITY, ATTEMPT).load()

    def test_wrong_scope_epoch_and_old_owner_cannot_commit(self):
        for identity in [replace(IDENTITY, epoch="9" * 32), replace(IDENTITY, scope=replace(IDENTITY.scope, repository_id=99))]:
            with self.assertRaises(PolicyError):
                Journal(self.store, identity, ATTEMPT).load()
        next_journal = Journal(self.store, IDENTITY, NEXT_ATTEMPT)
        taken = next_journal.takeover(self.snapshot, ATTEMPT, "completed")
        with self.assertRaises(PolicyError):
            self.journal.commit(taken, taken.record)
        with self.assertRaises(WriteConflict):
            self.journal.commit(self.snapshot, self.snapshot.record)

    def test_initialized_or_unconfirmed_bucket_cannot_reinitialize(self):
        with self.assertRaises(WriteConflict):
            self.journal.initialize(IDENTITY)
        with self.assertRaises(PolicyError):
            self.journal.initialize(None)

    def test_commit_cannot_erase_uncertainty_or_restore_older_valid_state(self):
        reserved, _ = self.journal.reserve(self.snapshot, GENERATION, OPERATION, "provision")
        with self.assertRaises(PolicyError):
            self.journal.commit(reserved, self.snapshot.record)
        removing = self.journal.commit(reserved, begin_removal(reserved.record, GENERATION, REMOVAL))
        with self.assertRaises(PolicyError):
            self.journal.commit(removing, reserved.record)

    def test_invalid_write_acknowledgement_cannot_return_reservation(self):
        original = self.store.write

        def bad_ack(expected, body):
            original(expected, body)
            return None

        self.store.write = bad_ack
        with self.assertRaises(WriteUncertain):
            self.journal.reserve(self.snapshot, GENERATION, OPERATION, "provision")

    def test_invalid_store_revision_cannot_be_used_as_create_if_absent(self):
        self.store.read = lambda: (None, self.store.value)
        with self.assertRaises(PolicyError):
            self.journal.load()

    def test_established_identity_inventory_and_attempt_cannot_be_rewritten(self):
        current, _ = self.journal.reserve(self.snapshot, GENERATION, OPERATION, "provision")
        result = OperationResult(OPERATION, ATTEMPT, "succeeded", True, (Resource("host-1", "host"),))
        current = self.journal.commit(current, resolve_operation(current.record, result))
        generation = current.record.generations[0]
        operation = current.record.operations[0]
        variants = [
            replace(current.record, generations=(), operations=()),
            replace(current.record, generations=(replace(generation, resources=()),)),
            replace(current.record, generations=(replace(generation, resources=(Resource("host-1", "secret"),)),)),
            replace(current.record, generations=(replace(generation, release_sha="b" * 40),), operations=(replace(operation, release_sha="b" * 40),)),
            replace(current.record, operations=(replace(operation, attempt=NEXT_ATTEMPT),)),
            replace(current.record, operations=(replace(operation, state="uncertain"),)),
            replace(current.record, owner=NEXT_ATTEMPT),
        ]
        for changed in variants:
            with self.subTest(changed=changed), self.assertRaises(PolicyError):
                self.journal.commit(current, changed)
        self.assertEqual(self.journal.load(), current)

    def test_bad_initial_acknowledgement_is_uncertain(self):
        store = MemoryStore()
        store.write = lambda expected, body: ""
        with self.assertRaises(WriteUncertain):
            Journal(store, IDENTITY, ATTEMPT).initialize(IDENTITY)

    def test_foreign_takeover_and_malformed_snapshot_revision_are_rejected(self):
        with self.assertRaises(PolicyError):
            Journal(self.store, replace(IDENTITY, epoch="9" * 32), NEXT_ATTEMPT).takeover(self.snapshot, ATTEMPT, "completed")
        for revision in [None, "", "bad\r\nrevision"]:
            with self.assertRaises(PolicyError):
                self.journal.commit(replace(self.snapshot, revision=revision), self.snapshot.record)
