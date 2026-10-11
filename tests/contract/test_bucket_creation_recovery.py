from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from oci.object_storage.models import CreateBucketDetails

from troubleshooter_deployment.oci_audit import AuditError, OciBucketAudit
from troubleshooter_deployment.oci_recovery_bucket import BucketError, BucketUncertain, OciRecoveryBucket
from troubleshooter_deployment.recovery_bucket import (
    BucketCreationEvidence, creation_body_v1, creation_digest_v1, metadata,
)
from tests.contract.oci_responses import CONFIG, SyntheticSigner, response, signing_stub
from tests.contract.test_oci_audit import START, END, event, page
from tests.contract.test_oci_recovery_bucket import ACK, REQUEST, bucket


def creation_event():
    item = event()
    item["data"]["request"]["headers"]["x-content-sha256"] = [creation_digest_v1(REQUEST)]
    return item


class BucketCreationRecoveryTests(unittest.TestCase):
    def setUp(self):
        patch("oci.loggingsearch.log_search_client.Signer", return_value=SyntheticSigner()).start()
        signing_stub().start()
        self.addCleanup(patch.stopall)
        self.audit = OciBucketAudit(CONFIG, REQUEST.scope)
        self.store = OciRecoveryBucket(CONFIG, REQUEST.scope)
        self.addCleanup(self.audit.close)
        self.addCleanup(self.store.close)
        self.send = patch("oci.base_client.OCIHTTPAdapter.send").start()
        self.evidence = BucketCreationEvidence(REQUEST, creation_digest_v1(REQUEST))

    def scan(self, request=REQUEST, start=START, end=END):
        return self.audit.reconcile_creation(request, start, end)

    def test_frozen_body_matches_pinned_model_and_actual_prepared_request(self):
        details = CreateBucketDetails(
            name=REQUEST.name, compartment_id=REQUEST.scope.compartment_id, metadata=metadata(REQUEST),
            public_access_type="NoPublicAccess", storage_tier="Standard", object_events_enabled=False,
            versioning="Disabled", auto_tiering="Disabled", bucket_scope="NAMESPACE",
        )
        previous = json.dumps(self.store._client.base_client.sanitize_for_serialization(details)).encode()
        self.assertEqual(creation_body_v1(REQUEST), previous)
        self.send.side_effect = [response(bucket())]
        self.assertEqual(self.store.create(REQUEST), ACK)
        self.assertEqual(self.send.call_args.args[0].body, previous)
        self.assertEqual(self.send.call_count, 1)

    def test_positive_audit_produces_request_evidence_not_bucket_identity(self):
        for header in ("x-content-sha256", "X-Content-SHA256"):
            item = creation_event()
            value = item["data"]["request"]["headers"].pop("x-content-sha256")
            item["data"]["request"]["headers"][header] = value
            self.send.side_effect = [page([item])]
            evidence = self.scan()
            self.assertEqual(evidence, self.evidence)
            self.assertFalse(hasattr(evidence, "identity"))
        self.assertTrue(all(call.args[0].url.startswith("https://logging.us-ashburn-1.")
                            for call in self.send.call_args_list))

    def test_missing_malformed_wrong_or_duplicate_digest_never_confirms(self):
        digest = creation_digest_v1(REQUEST)
        variants = [{}, {"x-content-sha256": digest}, {"x-content-sha256": []},
                    {"x-content-sha256": [digest, digest]}, {"x-content-sha256": [None]},
                    {"x-content-sha256": [digest], "X-Content-SHA256": [digest]},
                    {"x-content-sha256": ["not-the-body"]},
                    {"x-content-sha256": [creation_digest_v1(replace(REQUEST, name="other"))]}]
        for headers in variants:
            item = creation_event()
            item["data"]["request"]["headers"] = {"opc-client-request-id": [REQUEST.correlation], **headers}
            self.send.side_effect = [page([item])]
            self.assertIsNone(self.scan())

    def test_all_original_event_fields_remain_required(self):
        changes = [(["data", "identity", "principalId"], "other"),
                   (["data", "identity", "tenantId"], "other"),
                   (["data", "compartmentId"], "other"),
                   (["data", "additionalDetails", "namespace"], "other"),
                   (["data", "additionalDetails", "bucketName"], "other"),
                   (["data", "request", "path"], "/other"),
                   (["data", "request", "action"], "GET"),
                   (["data", "request", "headers", "opc-client-request-id"], ["f" * 32]),
                   (["data", "response", "status"], "409"), (["data", "response", "status"], 200),
                   (["data", "resourceId"], ACK.identity.bucket_id),
                   (["data", "eventName"], "DeleteBucket"), (["type"], "other"),
                   (["specversion"], "other"), (["dataschema"], "other"), (["time"], "invalid"),
                   (["id"], ""), (["source"], ""), (["datacontenttype"], "text/plain")]
        for path, value in changes:
            item = creation_event()
            target = item
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            self.send.side_effect = [page([item])]
            self.assertIsNone(self.scan(), path)

    def test_no_duplicate_partial_denied_or_excessive_scan_yields_evidence(self):
        for events in [[], [creation_event(), creation_event()]]:
            self.send.side_effect = [page(events)]
            self.assertIsNone(self.scan())
        self.send.side_effect = [page([creation_event()], headers={"opc-next-page": "n"}), page([creation_event()])]
        self.assertIsNone(self.scan())
        for replies in [
            [page([creation_event()], headers={"opc-next-page": "next"}), OSError("secret")],
            [response({"code": "Denied"}, status=403)],
            [page([creation_event()], headers={"opc-next-page": str(i)}) for i in range(5)],
            [page([creation_event()] * 11)], [response(body=b"x" * 262145)],
        ]:
            self.send.reset_mock()
            self.send.side_effect = replies
            with self.assertRaises(AuditError) as caught:
                self.scan()
            self.assertNotIn("secret", str(caught.exception))
            self.assertLessEqual(self.send.call_count, 5)

    def test_invalid_creation_query_stops_before_network(self):
        for request in [None, True, ACK, replace(REQUEST, name="bad/path"),
                        replace(REQUEST, principal="other"),
                        replace(REQUEST, scope=replace(REQUEST.scope, compartment_id="other"))]:
            with self.assertRaises(AuditError) as caught:
                self.scan(request)
            self.assertEqual(caught.exception.scan_reason, "query")
        with self.assertRaises(AuditError):
            self.scan(end=START)
        self.send.assert_not_called()

    def test_two_independent_gets_establish_same_identity_and_revision(self):
        self.send.side_effect = [response(bucket()), response(bucket())]
        self.assertEqual(self.store.reconcile_creation(self.evidence), ACK)
        requests = [call.args[0] for call in self.send.call_args_list]
        self.assertEqual([request.method for request in requests], ["GET", "GET"])
        self.assertEqual([request.url for request in requests], [
            "https://objectstorage.us-ashburn-1.oraclecloud.com/n/synthetic-ns/b/synthetic-bucket?fields=autoTiering"
        ] * 2)

    def test_changed_id_revision_configuration_or_creator_cannot_reconcile(self):
        for second in [response(bucket(id="replacement")), response(bucket(), headers={"etag": '"changed"'}),
                       response(bucket(createdBy="other")), response(bucket(metadata={})),
                       response(bucket(publicAccessType="ObjectRead")), response(bucket(versioning="Enabled")),
                       response(bucket(bucketScope="REGION")), response(bucket(kmsKeyId="other")),
                       response(bucket(autoTiering="Auto")), response(bucket(objectEventsEnabled=True)),
                       response(bucket(compartmentId="other")), response(bucket(isReadOnly=True)),
                       response(bucket(storageTier="Archive"))]:
            self.send.reset_mock()
            self.send.side_effect = [response(bucket()), second]
            with self.assertRaises(BucketError):
                self.store.reconcile_creation(self.evidence)
            self.assertEqual([c.args[0].method for c in self.send.call_args_list], ["GET", "GET"])

    def test_unavailable_or_malformed_reads_never_authorize(self):
        for replies in [
            [OSError("secret")], [response({"code": "Denied"}, status=403)],
            [response({"code": "NotAuthorizedOrNotFound"}, status=404)],
            [response(bucket(), headers={"etag": "*"})], [response(bucket(id=None))],
            [response(bucket()), OSError("secret")],
            [response(bucket()), response({"code": "NotAuthorizedOrNotFound"}, status=404)],
        ]:
            self.send.reset_mock()
            self.send.side_effect = replies
            with self.assertRaises(BucketError) as caught:
                self.store.reconcile_creation(self.evidence)
            self.assertNotIn("secret", str(caught.exception))
            self.assertLessEqual(self.send.call_count, 2)

    def test_invalid_or_wrong_scope_evidence_does_not_make_requests(self):
        other = replace(REQUEST, scope=replace(REQUEST.scope, compartment_id="other"))
        for evidence in [None, ACK, REQUEST, creation_event(), replace(self.evidence, content_sha256="bad"),
                         replace(self.evidence, request=replace(REQUEST, principal="other")),
                         BucketCreationEvidence(other, creation_digest_v1(other))]:
            with self.assertRaises(BucketError):
                self.store.reconcile_creation(evidence)
        self.send.assert_not_called()

    def test_late_success_is_rejected_by_operation_budget(self):
        self.send.side_effect = [response(bucket()), response(bucket())]
        with patch("troubleshooter_deployment.request_budget.monotonic", side_effect=[0, 21]):
            with self.assertRaises(BucketError):
                self.store.reconcile_creation(self.evidence)

    def test_lost_create_reply_recovers_from_fresh_adapters_without_repeating_create(self):
        self.send.side_effect = [OSError("reply lost after accepted request")]
        with self.assertRaises(BucketUncertain):
            self.store.create(REQUEST)
        sent_body = self.send.call_args.args[0].body
        self.assertEqual(sent_body, creation_body_v1(REQUEST))
        self.store.close()
        fresh = OciRecoveryBucket(CONFIG, REQUEST.scope)
        self.addCleanup(fresh.close)
        self.send.side_effect = [page([creation_event()]), response(bucket()), response(bucket())]
        evidence = self.scan()
        self.assertEqual(fresh.reconcile_creation(evidence), ACK)
        calls = [call.args[0] for call in self.send.call_args_list]
        self.assertEqual([call.method for call in calls], ["POST", "POST", "GET", "GET"])
        self.assertTrue(calls[1].url.startswith("https://logging.us-ashburn-1."))
        self.assertEqual(sum(call.method == "POST" and "/n/" in call.url for call in calls), 1)
