from copy import deepcopy
from dataclasses import replace
import json
from unittest.mock import patch

from troubleshooter_deployment.codec import encode, decode
from troubleshooter_deployment.journal import Journal, WriteConflict, WriteUncertain
from troubleshooter_deployment.oci_stack_journal import OciStackJournalStore, StackJournalError, PROTOCOL_TAG
from troubleshooter_deployment.oci_bucket_bootstrap import OciBucketBootstrapStore, BootstrapStoreError
from troubleshooter_deployment.policy import bootstrap, begin_generation
from troubleshooter_deployment.stack_journal_variables import KEYS, merge_journal
from troubleshooter_deployment.records import Identity
from tests.contract.oci_responses import CONFIG, response
from tests.contract.stack_journal_service import StackJournalService
from tests.deployment.fixtures import ATTEMPT, GENERATION, SHA


class OciStackJournalTests(StackJournalService):
    def adapter(self, anchor=None, config=None):
        store = OciStackJournalStore(config or CONFIG, anchor or self.anchor)
        self.addCleanup(store.close)
        return store

    def test_absent_read_pre_put_stop_and_fresh_conditional_initialization(self):
        store = self.adapter()
        self.assertIsNone(store.read())
        self.assertEqual(self.mutations(), [])  # A stop here leaves no bucket intent.
        fresh = self.adapter()
        journal = Journal(fresh, fresh.identity, ATTEMPT)
        saved = journal.initialize(fresh.identity)
        self.assertEqual(journal.load(), saved)
        self.assertEqual(saved.record.schema, 2)
        mutations = self.mutations()
        self.assertEqual(len(mutations), 1)
        self.assertEqual(mutations[0].headers["if-match"], '"s1"')
        self.assertEqual(set(json.loads(mutations[0].body)), {"variables", "freeformTags", "configSource"})
        self.assertEqual(self.stack["variables"]["unrelated"], "preserve")
        self.assertEqual(self.stack["definedTags"], {"Department": {"Name": "Demo"}})

    def test_later_write_preserves_configuration_and_independently_reads(self):
        store = self.adapter()
        journal = Journal(store, store.identity, ATTEMPT)
        initial = journal.initialize(store.identity)
        self.stack["configSource"] = {"configSourceType": "GIT_CONFIG_SOURCE", "repositoryUrl": "synthetic"}
        self.jobs = [{"id": "historical-job"}]
        before = deepcopy(self.stack)
        self.calls.clear()
        updated = journal.commit(initial, begin_generation(initial.record, GENERATION, SHA))
        self.assertEqual(journal.load(), updated)
        self.assertEqual(set(json.loads(self.mutations()[0].body)), {"variables"})
        for key in ("configSource", "freeformTags", "definedTags", "terraformVersion"):
            self.assertEqual(self.stack[key], before[key])
        self.assertFalse(any(c[2].endswith("/jobs") for c in self.calls))

    def test_lost_replies_are_uncertain_and_fresh_reader_determines_actual_state(self):
        for fault in ("before", "after"):
            with self.subTest(fault=fault):
                self.stack["variables"] = json.loads(self.initial.body)["variables"]
                self.stack["freeformTags"] = json.loads(self.initial.body)["freeformTags"]
                store = self.adapter()
                body = encode(bootstrap(store.identity, store.identity, ATTEMPT))
                self.fault = fault
                with self.assertRaises(WriteUncertain):
                    store.write(None, body)
                self.fault = None
                observed = self.adapter().read()
                self.assertEqual(None if observed is None else observed[1], None if fault == "before" else body)

    def test_conflict_and_stale_revision_never_overwrite(self):
        store = self.adapter()
        journal = Journal(store, store.identity, ATTEMPT)
        initial = journal.initialize(store.identity)
        with self.assertRaises(WriteConflict):
            store.write(None, encode(initial.record))
        self.stack_version += 1
        before = deepcopy(self.stack)
        with self.assertRaises(WriteConflict):
            journal.commit(initial, begin_generation(initial.record, GENERATION, SHA))
        self.assertEqual(self.stack, before)
        self.fault = "conflict"
        with self.assertRaises(WriteConflict):
            store.write(f'"s{self.stack_version}"', encode(initial.record))

    def test_wrong_identity_principal_and_untrusted_anchor_fail_without_io(self):
        for anchor, config in [(replace(self.anchor, stack_id="bad/id"), CONFIG),
                               (replace(self.anchor, etag="W/weak"), CONFIG),
                               (None, CONFIG), (self.anchor, {**CONFIG, "user": "other"}),
                               (self.anchor, {**CONFIG, "region": "us-phoenix-1"})]:
            with self.assertRaises(StackJournalError):
                OciStackJournalStore(config, anchor)
        self.assertEqual(self.calls, [])

    def test_invalid_stack_or_legacy_state_never_authorizes_put(self):
        original = deepcopy(self.stack)
        variants = [{"id": "other"}, {"compartmentId": "other"}, {"displayName": "other"},
                    {"lifecycleState": "DELETED"}, {"variables": {}}, {"freeformTags": {}},
                    {"terraformVersion": "other"}, {"configSource": []},
                    {"configSource": {"configSourceType": "ZIP_UPLOAD", "workingDirectory": "nested"}},
                    {"configSource": {"configSourceType": "GIT_CONFIG_SOURCE"}},
                    {"variables": {**original["variables"], "troubleshooter_cleanup_receipt_0": ""}},
                    {"variables": {**original["variables"], "troubleshooter_bucket_delete_intent": ""}},
                    {"freeformTags": {**original["freeformTags"], "troubleshooter_bucket_v1_intent": "bad"}},
                    {"freeformTags": {**original["freeformTags"], PROTOCOL_TAG: "unknown"}}]
        for change in variants:
            self.stack = {**deepcopy(original), **change}
            with self.assertRaises(StackJournalError):
                self.adapter().read()
        self.assertEqual(self.mutations(), [])

    def test_nonempty_partial_and_denied_job_observations_block_initialization(self):
        for jobs, headers in [([{"id": "job"}], {}), ({}, {}), ([], {"opc-next-page": "next"})]:
            self.jobs, self.job_headers = jobs, headers
            with self.assertRaises(StackJournalError):
                self.adapter().read()
        self.assertEqual(self.mutations(), [])

    def test_missing_or_partial_initialized_carrier_never_means_absence(self):
        store = self.adapter()
        store.write(None, encode(bootstrap(store.identity, store.identity, ATTEMPT)))
        original = deepcopy(self.stack)
        for missing in KEYS:
            self.stack = deepcopy(original)
            del self.stack["variables"][missing]
            with self.assertRaises(StackJournalError):
                self.adapter().read()
        self.stack = deepcopy(original)
        self.stack["variables"] = {k: v for k, v in self.stack["variables"].items() if k not in KEYS}
        with self.assertRaises(StackJournalError):
            self.adapter().read()
        self.stack = deepcopy(original)
        del self.stack["freeformTags"][PROTOCOL_TAG]
        with self.assertRaises(StackJournalError):
            self.adapter().read()

    def test_legacy_bucket_bootstrap_cannot_enroll_a_stack_journal(self):
        store = self.adapter()
        store.write(None, encode(bootstrap(store.identity, store.identity, ATTEMPT)))
        legacy = OciBucketBootstrapStore(CONFIG, self.anchor)
        self.addCleanup(legacy.close)
        self.calls.clear()
        with self.assertRaises(BootstrapStoreError):
            legacy.read()
        self.assertEqual(self.mutations(), [])

    def test_request_and_projected_response_limits_block_dispatch(self):
        store = self.adapter()
        body = encode(bootstrap(store.identity, store.identity, ATTEMPT))
        # Below the actual GET limit, above it once the added ZIP/record is included.
        self.stack["variables"].update({f"v{i}": "x" * 8000 for i in range(31)})
        self.stack["description"] = "x" * 10000
        self.assertLess(len(json.dumps(self.stack).encode()), 262144)
        with self.assertRaises(StackJournalError):
            store.write(None, body)
        self.assertEqual(self.mutations(), [])

    def test_changed_readback_and_expired_deadline_never_report_success(self):
        store = self.adapter()
        body = encode(bootstrap(store.identity, store.identity, ATTEMPT))
        original = self.send

        def changed(request, **kwargs):
            result = original(request, **kwargs)
            if request.method == "PUT":
                self.stack["variables"]["unrelated"] = "changed"
            return result

        self.transport.side_effect = changed
        with self.assertRaises(WriteUncertain):
            store.write(None, body)
        self.transport.side_effect = original
        self.calls.clear()
        with patch("troubleshooter_deployment.request_budget.monotonic", side_effect=[0, 1, 21]):
            with self.assertRaises(WriteUncertain):
                store.write(f'"s{self.stack_version}"', body)
        self.assertEqual([c[1] for c in self.calls], ["GET", "PUT"])

    def test_invalid_record_history_initial_owner_and_close_errors_are_sanitized(self):
        store = self.adapter()
        good = bootstrap(store.identity, store.identity, ATTEMPT)
        for body in (b"{}", encode(replace(good, owner=replace(ATTEMPT, number=2))),
                     encode(begin_generation(good, GENERATION, SHA))):
            with self.assertRaises(StackJournalError):
                store.write(None, body)
        self.assertEqual(self.mutations(), [])
        with patch.object(store._client.base_client.session, "close", side_effect=OSError("private")):
            with self.assertRaises(StackJournalError) as error:
                store.close()
            self.assertNotIn("private", str(error.exception))

    def test_initial_readback_must_still_be_a_root_zip(self):
        store = self.adapter()
        original = self.send

        def wrong_source(request, **kwargs):
            result = original(request, **kwargs)
            if request.method == "PUT":
                self.stack["configSource"] = {"configSourceType": "GIT_CONFIG_SOURCE"}
            return result

        self.transport.side_effect = wrong_source
        with self.assertRaises(WriteUncertain):
            store.write(None, encode(bootstrap(store.identity, store.identity, ATTEMPT)))

    def test_foreign_identity_journal_and_legacy_receipt_cannot_be_adopted(self):
        from troubleshooter_deployment.receipt_variables import merge_receipt
        from troubleshooter_deployment.cleanup import prepare_purge
        store = self.adapter()
        record = bootstrap(store.identity, store.identity, ATTEMPT)
        with self.assertRaises(StackJournalError):
            store.write(None, encode(replace(record, identity=replace(store.identity, stack_id="other"))))
        store.write(None, encode(record))
        valid = deepcopy(self.stack)
        self.stack["variables"] = merge_journal(self.stack["variables"],
            encode(replace(record, identity=replace(store.identity, stack_id="other"))))
        with self.assertRaises(StackJournalError):
            store.read()
        self.stack = valid
        bucket = Identity(store.identity.scope, "synthetic-bucket", store.identity.epoch)
        receipt = prepare_purge(bootstrap(bucket, bucket, ATTEMPT), "1" * 32).receipt
        self.stack["variables"] = merge_receipt(self.stack["variables"], receipt)
        with self.assertRaises(StackJournalError):
            store.read()

    def test_http_failure_ack_mismatch_and_bad_etag_never_hide_retries(self):
        store = self.adapter()
        body = encode(bootstrap(store.identity, store.identity, ATTEMPT))
        original = self.send
        for status in (403, 429, 500):
            def rejected(request, **kwargs):
                if request.method == "PUT":
                    self.calls.append(("oci", "PUT", request.url, request))
                    return response({"code": "SyntheticFailure", "message": "private"}, status=status)
                return original(request, **kwargs)
            self.calls.clear()
            self.transport.side_effect = rejected
            with self.assertRaises(WriteUncertain):
                store.write(None, body)
            self.assertEqual(len(self.mutations()), 1)
        self.transport.side_effect = original
        self.transport.return_value = None
        self.stack_version += 1

        def wrong_ack(request, **kwargs):
            result = original(request, **kwargs)
            if request.method == "PUT":
                data = deepcopy(self.stack)
                data["variables"] = merge_journal(data["variables"],
                    encode(begin_generation(decode(body), GENERATION, SHA)))
                return response(data)
            return result

        self.transport.side_effect = wrong_ack
        with self.assertRaises(WriteUncertain):
            store.write(None, body)
        self.transport.side_effect = lambda *args, **kwargs: response(self.stack, headers={"etag": "W/weak"})
        with self.assertRaises(StackJournalError):
            store.read()
