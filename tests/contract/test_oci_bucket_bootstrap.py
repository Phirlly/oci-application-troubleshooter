from copy import deepcopy
from dataclasses import replace
import json
from unittest.mock import patch

from troubleshooter_deployment.bucket_bootstrap import PREFIX, acknowledge, enroll
from troubleshooter_deployment.oci_bucket_bootstrap import OciBucketBootstrapStore, BootstrapStoreError
from troubleshooter_deployment.recovery_bucket import BucketAcknowledgement
from troubleshooter_deployment.records import Identity
from tests.contract.recovery_store_service import RecoveryStoreService
from tests.contract.oci_responses import CONFIG, response
from tests.contract.test_oci_stack_bootstrap import STACK_ID
from tests.deployment.fixtures import ATTEMPT
from tests.deployment.test_bucket_bootstrap import NOW


class OciBucketBootstrapTests(RecoveryStoreService):
    def client(self):
        client = OciBucketBootstrapStore(CONFIG, self.anchor)
        self.addCleanup(client.close)
        return client

    def test_conditional_tag_only_write_preserves_every_unrelated_field(self):
        client = self.client()
        before = deepcopy(self.stack)
        observed = client.read()
        state = enroll(self.initial, STACK_ID, "synthetic-ns", ATTEMPT, NOW)
        saved = client.persist(observed, state)
        self.assertEqual(saved.record, state)
        self.assertNotEqual(saved.revision, observed.revision)
        sent = self.mutations()[0]
        self.assertEqual(sent.headers["if-match"], observed.revision)
        self.assertEqual(json.loads(sent.body), {"freeformTags": self.stack["freeformTags"]})
        self.assertEqual({k: v for k, v in self.stack.items() if k != "freeformTags"},
                         {k: v for k, v in before.items() if k != "freeformTags"})
        self.assertEqual(self.stack["freeformTags"]["Department"], "Demo")
        self.assertEqual(self.calls[-1][1], "GET")

    def test_stale_revision_or_record_prevents_update_and_service_412_is_not_confirmation(self):
        client = self.client()
        observed = client.read()
        state = enroll(self.initial, STACK_ID, "synthetic-ns", ATTEMPT, NOW)
        self.stack_version += 1
        with self.assertRaises(BootstrapStoreError):
            client.persist(observed, state)
        self.assertEqual(self.mutations(), [])
        observed = client.read()
        self.fault = "cas_conflict"
        with self.assertRaises(BootstrapStoreError):
            client.persist(observed, state)
        self.assertIsNone(self.state())
        self.assertEqual(len(self.mutations()), 1)

    def test_incomplete_requires_empty_complete_jobs_and_no_cleanup_state(self):
        client = self.client()
        for jobs, headers in [([{"id": "job"}], {}), ([], {"opc-next-page": ""})]:
            self.jobs, self.job_headers = jobs, headers
            with self.assertRaises(BootstrapStoreError):
                client.read()
        self.jobs, self.job_headers = [], {}
        self.stack["variables"]["troubleshooter_cleanup_receipt_0"] = "partial"
        with self.assertRaises(BootstrapStoreError):
            client.read()
        self.assertEqual(self.mutations(), [])

    def test_read_rejects_wrong_identity_reserved_metadata_and_raw_types(self):
        client = self.client()
        original = deepcopy(self.stack)
        for delta in [{"id": "other"}, {"compartmentId": "other"}, {"displayName": "other"},
                      {"lifecycleState": "CREATING"}, {"variables": {}}, {"variables": []},
                      {"freeformTags": {}}, {"freeformTags": []}, {"definedTags": []},
                      {"variables": {**original["variables"], "wrong": 1}},
                      {"freeformTags": {**original["freeformTags"], PREFIX + "wrong": "x"}}]:
            self.stack = {**original, **delta}
            with self.assertRaises(BootstrapStoreError):
                client.read()
        self.assertEqual(self.mutations(), [])

    def test_lost_write_only_confirms_exact_readback_and_preserved_metadata(self):
        client = self.client()
        observed = client.read()
        state = enroll(self.initial, STACK_ID, "synthetic-ns", ATTEMPT, NOW)
        self.fault = "create_uncertain_after"
        self.assertEqual(client.persist(observed, state).record, state)
        initializing = acknowledge(state, BucketAcknowledgement(state.request,
            Identity(state.request.scope, "synthetic-bucket", state.request.epoch)))
        observed = client.read()
        original_send = self.send

        def changed_after_write(request, **kwargs):
            result = original_send(request, **kwargs)
            if request.method == "PUT":
                self.stack["variables"]["unrelated"] = "raced"
            return result

        self.fault = None
        self.transport.side_effect = changed_after_write
        with self.assertRaises(BootstrapStoreError):
            client.persist(observed, initializing)
        self.assertEqual(self.state().phase, "journal_initializing")

    def test_invalid_anchor_config_read_and_close_are_sanitized(self):
        for config, anchor in [(CONFIG, None), ({**CONFIG, "user": "other"}, self.anchor),
                               (CONFIG, replace(self.anchor, stack_id="bad/path")),
                               (CONFIG, replace(self.anchor, etag="*"))]:
            with self.assertRaises(BootstrapStoreError):
                OciBucketBootstrapStore(config, anchor)
        client = self.client()
        for result in [response([], status=200), response(self.stack, headers={"etag": "W/weak"}),
                       response(self.stack, status=404)]:
            self.transport.side_effect = [result]
            with self.assertRaises(BootstrapStoreError):
                client.read()
        with patch.object(client._client.base_client.session, "close", side_effect=OSError("secret")):
            with self.assertRaisesRegex(BootstrapStoreError, "bucket_bootstrap"):
                client.close()
