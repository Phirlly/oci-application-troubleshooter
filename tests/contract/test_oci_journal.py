from dataclasses import replace
import os
import unittest
from unittest.mock import patch

from urllib3.exceptions import HeaderParsingError

from troubleshooter_deployment.codec import encode
from troubleshooter_deployment.journal import Journal, WriteConflict, WriteUncertain
from troubleshooter_deployment.oci_journal import OciJournalStore, StoreError
from troubleshooter_deployment.policy import bootstrap, begin_generation
from tests.deployment.fixtures import ATTEMPT, GENERATION, OPERATION, SHA
from tests.contract.oci_responses import OCI_IDENTITY, CONFIG, BUCKET, response, signing_stub


class OciJournalTests(unittest.TestCase):
    def setUp(self):
        signing_stub().start()
        self.addCleanup(patch.stopall)
        self.store = OciJournalStore(CONFIG, OCI_IDENTITY, "synthetic-ns", "synthetic-bucket")
        self.addCleanup(self.store.close)
        self.record = bootstrap(OCI_IDENTITY, OCI_IDENTITY, ATTEMPT)
        self.body = encode(self.record)
        self.send = patch("oci.base_client.OCIHTTPAdapter.send").start()

    def sequence(self, middle, before=None, after=None):
        replies = [response() if before is None else before, middle, response() if after is None else after]
        self.send.reset_mock()
        self.send.side_effect = replies
        return replies

    def test_exact_get_and_conditional_put_requests_through_real_sdk(self):
        for expected in [None, '"revision-1"']:
            replies = self.sequence(response(body=b""))
            self.assertEqual(self.store.write(expected, self.body), '"revision-1"')
            request = self.send.call_args_list[1].args[0]
            self.assertEqual(request.method, "PUT")
            self.assertEqual(request.url, "https://objectstorage.us-ashburn-1.oraclecloud.com/n/synthetic-ns/b/synthetic-bucket/o/journal%2F" + OCI_IDENTITY.epoch + ".json")
            self.assertEqual(request.body, self.body)
            self.assertEqual(request.headers["Content-Length"], str(len(self.body)))
            self.assertEqual(request.headers["content-type"], "application/json")
            self.assertEqual(request.headers["authorization"], "synthetic-signature")
            self.assertEqual(request.headers.get("if-none-match"), "*" if expected is None else None)
            self.assertEqual(request.headers.get("if-match"), expected)
            self.assertTrue(all(item.raw.closed for item in replies))
        replies = self.sequence(response(body=self.body))
        self.assertEqual(self.store.read(), ('"revision-1"', self.body))
        self.assertEqual(self.send.call_args_list[1].args[0].method, "GET")
        self.assertTrue(all(item.raw.closed for item in replies))

    def test_unknown_read_or_failed_precheck_never_becomes_absence_or_write(self):
        for status in [401, 403, 404, 429, 500]:
            self.sequence(response({"code": "NotAuthorizedOrNotFound", "message": "synthetic-secret"}, status=status))
            with self.assertRaises(StoreError) as error:
                self.store.read()
            self.assertNotIn("synthetic-secret", str(error.exception))
            self.assertEqual(self.send.call_count, 2)
        self.send.reset_mock()
        self.send.side_effect = [response({"code": "NotAuthorizedOrNotFound"}, status=404)]
        with self.assertRaises(StoreError):
            self.store.write(None, self.body)
        self.assertEqual(self.send.call_count, 1)

    def test_each_ownership_and_bucket_mode_mismatch_blocks_before_put(self):
        changes = {"id": "replacement", "name": "other", "namespace": "other", "compartmentId": "other",
                   "metadata": {"deployment_epoch": "9" * 32}, "publicAccessType": "ObjectRead",
                   "storageTier": "Archive", "versioning": "Enabled", "bucketScope": "REGION",
                   "isReadOnly": True, "kmsKeyId": "custom-key"}
        for key, value in changes.items():
            with self.subTest(key=key):
                self.sequence(response(body=b""), before=response(dict(BUCKET, **{key: value})))
                with self.assertRaises(StoreError):
                    self.store.write(None, self.body)
                self.assertEqual(self.send.call_count, 1)

    def test_postwrite_replacement_lost_reply_and_invalid_ack_are_uncertain(self):
        for middle, after in [
            (OSError("synthetic-secret"), response()),
            (response(body=b"", headers={"etag": ""}), response()),
            (response(body=b""), response(dict(BUCKET, id="replacement"))),
            (response({"code": "InternalError"}, status=500), response()),
        ]:
            self.sequence(middle, after=after)
            with self.assertRaises(WriteUncertain) as error:
                self.store.write(None, self.body)
            self.assertNotIn("synthetic-secret", str(error.exception))
            self.assertEqual(sum(call.args[0].method == "PUT" for call in self.send.call_args_list), 1)

    def test_sdk_coercion_cannot_turn_invalid_raw_types_into_ownership(self):
        for value in [0, "", None, []]:
            self.sequence(response(body=b""), before=response(dict(BUCKET, isReadOnly=value)))
            with self.assertRaises(StoreError):
                self.store.write(None, self.body)
            self.assertEqual(self.send.call_count, 1)

    def test_put412_is_conflict_but_postcheck412_is_uncertain(self):
        self.sequence(response({"code": "IfNoneMatchFailed", "message": "synthetic"}, status=412))
        with self.assertRaises(WriteConflict):
            self.store.write(None, self.body)
        self.sequence(response(body=b""), after=response({"code": "IfMatchFailed"}, status=412))
        with self.assertRaises(WriteUncertain):
            self.store.write(None, self.body)

    def test_header_error_never_retries_and_restores_environment(self):
        with patch.dict(os.environ, {"OCI_HEADER_PARSING_ERROR_MAX_RETRIES": "2"}):
            self.sequence(HeaderParsingError(["synthetic-secret"], "synthetic"))
            with self.assertRaises(WriteUncertain):
                self.store.write(None, self.body)
            self.assertEqual(self.send.call_count, 2)
            self.assertEqual(os.environ["OCI_HEADER_PARSING_ERROR_MAX_RETRIES"], "2")
        # SDK copies the session on the error; next call must retain all bounds.
        self.send.side_effect = [response(body=b"x" * 262145)]
        with self.assertRaises(StoreError):
            self.store.read()

    def test_session_reset_after412_and413_preserves_body_limits(self):
        for status, code in [(412, "IfNoneMatchFailed"), (413, "RequestEntityTooLarge")]:
            self.sequence(response({"code": code}, status=status))
            with self.assertRaises((WriteConflict, WriteUncertain)):
                self.store.write(None, self.body)
            self.send.side_effect = [response(body=b"x" * 262145)]
            with self.assertRaises(StoreError):
                self.store.read()

    def test_bad_body_and_etag_are_rejected_before_requests(self):
        for revision in ["*", '"v1","v2"', 'W/"v1"', "bad\r\nvalue", "", "v 1"]:
            with self.assertRaises(StoreError):
                self.store.write(revision, self.body)
        for body in [b"{}", encode(replace(self.record, identity=replace(OCI_IDENTITY, epoch="9" * 32)))]:
            with self.assertRaises((ValueError, StoreError)):
                self.store.write(None, body)
        self.send.assert_not_called()

    def test_read_rejects_bad_etag_foreign_record_and_postcheck_change(self):
        for middle, after in [
            (response(body=self.body, headers={"etag": "*"}), response()),
            (response(body=encode(replace(self.record, identity=replace(OCI_IDENTITY, epoch="9" * 32)))), response()),
            (response(body=b"{}"), response()),
            (response(body=self.body), response(dict(BUCKET, id="replacement"))),
        ]:
            self.sequence(middle, after=after)
            with self.assertRaises(StoreError):
                self.store.read()

    def test_late_put_cannot_return_acknowledgement(self):
        self.sequence(response(body=b""))
        with patch("troubleshooter_deployment.request_budget.monotonic", side_effect=[0, 21]):
            with self.assertRaises(WriteUncertain):
                self.store.write(None, self.body)

    def test_config_scope_paths_and_secrets_never_escape_errors(self):
        for config, identity, namespace, bucket in [
            (dict(CONFIG, region="us-phoenix-1"), OCI_IDENTITY, "synthetic-ns", "synthetic-bucket"),
            (dict(CONFIG, tenancy="other"), OCI_IDENTITY, "synthetic-ns", "synthetic-bucket"),
            (dict(CONFIG, key_file="synthetic-secret"), OCI_IDENTITY, "synthetic-ns", "synthetic-bucket"),
            (CONFIG, OCI_IDENTITY, "../bad", "synthetic-bucket"),
            (CONFIG, OCI_IDENTITY, "synthetic-ns", "../bad"),
        ]:
            with self.assertRaises(StoreError) as error:
                OciJournalStore(config, identity, namespace, bucket)
            self.assertNotIn("synthetic-secret", str(error.exception))
        self.send.assert_not_called()

    def test_journal_lost_write_reloads_without_new_reservation(self):
        record = begin_generation(self.record, GENERATION, SHA)
        snapshot_body = encode(record)
        self.sequence(response(body=snapshot_body))
        journal = Journal(self.store, OCI_IDENTITY, ATTEMPT)
        before = journal.load()
        captured = []

        def accept_then_lose(request, **kwargs):
            if request.method == "PUT":
                captured.append(request.body)
                raise OSError("synthetic lost reply")
            return response()

        self.send.side_effect = accept_then_lose
        with self.assertRaises(WriteUncertain):
            journal.reserve(before, GENERATION, OPERATION, "provision")
        self.sequence(response(body=captured[0]))
        loaded = journal.load()
        self.send.reset_mock()
        _, decision = journal.reserve(loaded, GENERATION, OPERATION, "provision")
        self.assertEqual(decision, "reconcile")
        self.send.assert_not_called()
