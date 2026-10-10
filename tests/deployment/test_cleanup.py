from dataclasses import replace
import unittest

from troubleshooter_deployment.records import OperationResult, Resource, DeleteAcknowledgement
from troubleshooter_deployment.policy import bootstrap, begin_generation, begin_removal, reserve, resolve_operation, PolicyError
from troubleshooter_deployment.cleanup import (
    next_action, observe_resource, mark_workload_removed,
    confirm_external_cleanup, prepare_purge, may_purge, completion,
)
from troubleshooter_deployment.journal import Journal, WriteConflict, WriteUncertain
from troubleshooter_deployment.codec import encode
from tests.deployment.fixtures import ATTEMPT, IDENTITY, GENERATION, SECOND_GENERATION, OPERATION, REMOVAL, SHA, MemoryStore


class CleanupTests(unittest.TestCase):
    def setUp(self):
        record = begin_generation(bootstrap(IDENTITY, IDENTITY, ATTEMPT), GENERATION, SHA)
        record, _ = reserve(record, GENERATION, OPERATION, "provision")
        resources = (Resource("host-1", "host"), Resource("stack-1", "stack"), Resource("secret-1", "secret"), Resource("vault-1", "vault"))
        self.record = resolve_operation(record, OperationResult(OPERATION, ATTEMPT, "succeeded", True, resources))
        self.removing = begin_removal(self.record, GENERATION, REMOVAL)

    def workload_removed(self):
        record = observe_resource(self.removing, GENERATION, "host-1", "terminated")
        record = observe_resource(record, GENERATION, "stack-1", "destroyed")
        return mark_workload_removed(record, GENERATION)

    def ready(self):
        record = confirm_external_cleanup(self.workload_removed(), GENERATION)
        for resource in ["secret-1", "vault-1"]:
            record = observe_resource(record, GENERATION, resource, "deleted")
        return record

    def test_cleanup_orders_reconciliation_fence_destroy_and_external_work(self):
        self.assertEqual(next_action(self.removing, GENERATION), "fence_workload")
        fenced = observe_resource(self.removing, GENERATION, "host-1", "terminated")
        self.assertEqual(next_action(fenced, GENERATION), "destroy_workload")
        removed = self.workload_removed()
        self.assertEqual(next_action(removed, GENERATION), "clean_external")
        cleaned = confirm_external_cleanup(removed, GENERATION)
        self.assertEqual(next_action(cleaned, GENERATION), "schedule_secrets")

    def test_secret_scheduling_precedes_key_and_vault_scheduling(self):
        record = confirm_external_cleanup(self.workload_removed(), GENERATION)
        with self.assertRaises(PolicyError):
            observe_resource(record, GENERATION, "vault-1", "scheduled", "2026-11-01T00:00:00Z")
        record = observe_resource(record, GENERATION, "secret-1", "scheduled", "2026-11-01T00:00:00Z")
        self.assertEqual(next_action(record, GENERATION), "schedule_keys_and_vault")
        record = observe_resource(record, GENERATION, "vault-1", "scheduled", "2026-11-01T00:00:00Z")
        self.assertEqual(next_action(record, GENERATION), "provider_deletion_pending")

    def test_unknown_create_blocks_cleanup_even_if_workflow_is_finished(self):
        record, _ = reserve(self.record, GENERATION, "6" * 32, "provision")
        record = begin_removal(record, GENERATION, REMOVAL)
        self.assertEqual(next_action(record, GENERATION), "reconcile")
        with self.assertRaises(PolicyError):
            mark_workload_removed(record, GENERATION)

    def test_no_vm_ever_created_can_be_removed(self):
        record = begin_generation(bootstrap(IDENTITY, IDENTITY, ATTEMPT), GENERATION, SHA)
        record = begin_removal(record, GENERATION, REMOVAL)
        removed = mark_workload_removed(record, GENERATION)
        self.assertEqual(removed.generations[0].phase, "workload_removed")

    def test_stopped_unknown_or_wrong_host_cannot_satisfy_fence(self):
        for resource_id, state in [("host-1", "stopped"), ("host-1", "unknown"), ("host-1", "404"), ("another-host", "terminated")]:
            with self.subTest(state=state), self.assertRaises(PolicyError):
                observe_resource(self.removing, GENERATION, resource_id, state)
        with self.assertRaises(PolicyError):
            observe_resource(self.removing, GENERATION, "stack-1", "destroyed")
        with self.assertRaises(PolicyError):
            mark_workload_removed(self.removing, GENERATION)

    def test_credentials_are_kept_until_external_cleanup_completes(self):
        for record in [self.removing, self.workload_removed()]:
            with self.assertRaises(PolicyError):
                observe_resource(record, GENERATION, "secret-1", "scheduled", "2026-11-01T00:00:00Z")
        with self.assertRaises(PolicyError):
            confirm_external_cleanup(self.removing, GENERATION)

    def test_scheduled_date_is_not_physical_deletion(self):
        record = confirm_external_cleanup(self.workload_removed(), GENERATION)
        for resource in ["secret-1", "vault-1"]:
            record = observe_resource(record, GENERATION, resource, "scheduled", "2000-01-01T00:00:00Z")
        self.assertEqual(next_action(record, GENERATION), "provider_deletion_pending")
        with self.assertRaises(PolicyError):
            prepare_purge(record, "7" * 32)
        with self.assertRaises(PolicyError):
            observe_resource(record, GENERATION, "secret-1", "404")

    def test_old_cleanup_cannot_touch_new_generation(self):
        record = self.workload_removed()
        record = begin_generation(record, SECOND_GENERATION, "b" * 40)
        new_generation = record.generations[1]
        record = confirm_external_cleanup(record, GENERATION)
        record = observe_resource(record, GENERATION, "secret-1", "deleted")
        self.assertEqual(record.generations[1], new_generation)
        with self.assertRaises(PolicyError):
            observe_resource(record, SECOND_GENERATION, "secret-1", "deleted")
        with self.assertRaises(PolicyError):
            prepare_purge(record, "7" * 32)

    def test_receipt_freezes_scope_and_requires_exact_durable_readback(self):
        record = prepare_purge(self.ready(), "7" * 32)
        receipt = record.receipt
        self.assertEqual(receipt.attempt, ATTEMPT)
        self.assertEqual(receipt.generation_ids, (GENERATION,))
        self.assertFalse(may_purge(record, None))
        self.assertFalse(may_purge(record, replace(receipt, identity=replace(IDENTITY, epoch="9" * 32))))
        self.assertTrue(may_purge(record, receipt))
        with self.assertRaises(PolicyError):
            begin_generation(record, SECOND_GENERATION, SHA)
        self.assertEqual(prepare_purge(record, "7" * 32), record)

    def test_lost_delete_reply_or_new_bucket_never_reports_complete(self):
        record = prepare_purge(self.ready(), "7" * 32)
        receipt = record.receipt
        self.assertEqual(completion(receipt, receipt, None), "cleanup_incomplete")
        wrong = DeleteAcknowledgement(replace(IDENTITY, bucket_id="replacement"), receipt.removal_id)
        self.assertEqual(completion(receipt, receipt, wrong), "cleanup_incomplete")
        ack = DeleteAcknowledgement(IDENTITY, receipt.removal_id)
        self.assertEqual(completion(receipt, None, ack), "cleanup_incomplete")
        self.assertEqual(completion(receipt, replace(receipt, removal_id="8" * 32), ack), "cleanup_incomplete")
        self.assertEqual(completion(receipt, receipt, ack), "complete")

    def test_receipt_journal_write_conflict_or_lost_reply_returns_no_purge_permission(self):
        store = MemoryStore()
        journal = Journal(store, IDENTITY, ATTEMPT)
        journal.initialize(IDENTITY)
        store.value = encode(self.ready())
        current = journal.load()
        prepared = prepare_purge(current.record, "7" * 32)
        store.fail = "after"
        with self.assertRaises(WriteUncertain):
            journal.commit(current, prepared)
        store.fail = None
        with self.assertRaises(WriteConflict):
            journal.commit(current, prepared)
        self.assertFalse(may_purge(journal.load().record, None))

    def test_prepared_receipt_cannot_be_discarded_using_current_revision(self):
        store = MemoryStore()
        store.value = encode(self.ready())
        journal = Journal(store, IDENTITY, ATTEMPT)
        before = journal.load()
        frozen = journal.commit(before, prepare_purge(before.record, "7" * 32))
        with self.assertRaises(PolicyError):
            journal.commit(frozen, before.record)
        with self.assertRaises(PolicyError):
            prepare_purge(frozen.record, "8" * 32)

    def test_terminal_resource_evidence_cannot_regress(self):
        record = self.ready()
        with self.assertRaises(PolicyError):
            observe_resource(record, GENERATION, "secret-1", "scheduled", "2026-11-01T00:00:00Z")
        self.assertEqual(observe_resource(record, GENERATION, "secret-1", "deleted"), record)
        self.assertEqual(next_action(self.record, GENERATION), "begin_removal")

    def test_complete_domain_lifecycle_survives_journal_reloads(self):
        store = MemoryStore()
        journal = Journal(store, IDENTITY, ATTEMPT)
        current = journal.initialize(IDENTITY)
        current = journal.commit(current, begin_generation(current.record, GENERATION, SHA))
        current, disposition = journal.reserve(current, GENERATION, OPERATION, "provision")
        self.assertEqual(disposition, "reserved")
        resources = (Resource("host-1", "host"), Resource("stack-1", "stack"), Resource("secret-1", "secret"))
        current = journal.commit(current, resolve_operation(current.record, OperationResult(OPERATION, ATTEMPT, "succeeded", True, resources)))
        current = journal.commit(current, begin_removal(current.record, GENERATION, REMOVAL))
        for resource_id, state in [("host-1", "terminated"), ("stack-1", "destroyed")]:
            current = journal.load()
            current = journal.commit(current, observe_resource(current.record, GENERATION, resource_id, state))
        current = journal.commit(current, mark_workload_removed(current.record, GENERATION))
        current = journal.commit(current, confirm_external_cleanup(current.record, GENERATION))
        current = journal.commit(current, observe_resource(current.record, GENERATION, "secret-1", "deleted"))
        current = journal.commit(current, prepare_purge(current.record, "7" * 32))
        reloaded = Journal(store, IDENTITY, ATTEMPT).load()
        self.assertEqual(reloaded, current)
        self.assertTrue(may_purge(reloaded.record, reloaded.record.receipt))
        ack = DeleteAcknowledgement(IDENTITY, "7" * 32)
        self.assertEqual(completion(reloaded.record.receipt, reloaded.record.receipt, ack), "complete")
