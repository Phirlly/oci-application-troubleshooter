from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from troubleshooter_deployment.cleanup import prepare_purge
from troubleshooter_deployment.policy import bootstrap
from troubleshooter_deployment.recovery_bucket import BucketRequest, BucketAcknowledgement, metadata
from troubleshooter_deployment.oci_recovery_bucket import OciRecoveryBucket, BucketError, BucketUncertain
from tests.contract.oci_responses import CONFIG, OCI_IDENTITY, BUCKET, response, signing_stub
from tests.deployment.fixtures import ATTEMPT, REMOVAL

REQUEST = BucketRequest(OCI_IDENTITY.scope, "synthetic-ns", "synthetic-bucket",
                        OCI_IDENTITY.epoch, CONFIG["user"], "a" * 32)
ACK = BucketAcknowledgement(REQUEST, OCI_IDENTITY)


def bucket(**changes):
    return {**BUCKET, "metadata": metadata(REQUEST), "createdBy": CONFIG["user"],
            "autoTiering": "Disabled", "objectEventsEnabled": False, **changes}


class RecoveryBucketTests(unittest.TestCase):
    def setUp(self):
        signing_stub().start()
        self.addCleanup(patch.stopall)
        self.store = OciRecoveryBucket(CONFIG, OCI_IDENTITY.scope)
        self.addCleanup(self.store.close)
        self.send = patch("oci.base_client.OCIHTTPAdapter.send").start()
        self.record = prepare_purge(bootstrap(OCI_IDENTITY, OCI_IDENTITY, ATTEMPT), REMOVAL)

    def test_namespace_create_and_independent_get_exact_requests(self):
        self.send.side_effect = [response("synthetic-ns"), response(bucket()),
                                 response(bucket(extraServiceField={"never": "export"}))]
        self.assertEqual(self.store.namespace(), REQUEST.namespace)
        ack = self.store.create(REQUEST)
        self.assertEqual(ack, ACK)
        observed = self.store.read(ack)
        self.assertEqual(observed.unknown_fields, ("extraServiceField",))
        calls = [c.args[0] for c in self.send.call_args_list]
        self.assertEqual([c.method for c in calls], ["GET", "POST", "GET"])
        self.assertEqual(calls[0].url, "https://objectstorage.us-ashburn-1.oraclecloud.com/n")
        self.assertEqual(calls[1].url, calls[0].url + "/synthetic-ns/b")
        self.assertEqual(calls[2].url, calls[1].url + "/synthetic-bucket")
        self.assertEqual(calls[1].headers["opc-client-request-id"], REQUEST.correlation)
        self.assertNotIn("opc-retry-token", calls[1].headers)
        self.assertEqual(json.loads(calls[1].body), {
            "name": REQUEST.name, "compartmentId": REQUEST.scope.compartment_id,
            "metadata": metadata(REQUEST), "publicAccessType": "NoPublicAccess",
            "storageTier": "Standard", "objectEventsEnabled": False, "versioning": "Disabled",
            "autoTiering": "Disabled", "bucketScope": "NAMESPACE",
        })
        for call in self.send.call_args_list:
            self.assertTrue(call.kwargs["verify"])
            self.assertEqual(call.kwargs["timeout"], (5, 5))

    def test_delete_requires_receipt_identity_then_fresh_etag(self):
        self.send.side_effect = [response(bucket(), headers={"etag": '"new"'}),
                                 response(body=b"", status=204)]
        result = self.store.delete_empty(ACK, self.record, self.record.receipt, "b" * 32)
        self.assertEqual(result.identity, OCI_IDENTITY)
        self.assertEqual(result.removal_id, REMOVAL)
        calls = [c.args[0] for c in self.send.call_args_list]
        self.assertEqual([c.method for c in calls], ["GET", "DELETE"])
        self.assertEqual(calls[1].headers["if-match"], '"new"')
        self.assertEqual(calls[1].headers["opc-client-request-id"], "b" * 32)

    def test_delete_blocks_missing_receipt_or_mismatched_identity_before_requests(self):
        for ack, record, readback, correlation in [
            (ACK, self.record, None, "b" * 32),
            (ACK, self.record, replace(self.record.receipt, removal_id="f" * 32), "b" * 32),
            (replace(ACK, identity=replace(OCI_IDENTITY, epoch="9" * 32)),
             self.record, self.record.receipt, "b" * 32),
            (ACK, self.record, self.record.receipt, True),
            (ACK, self.record, self.record.receipt, REQUEST.correlation),
        ]:
            with self.assertRaises(BucketError):
                self.store.delete_empty(ack, record, readback, correlation)
        self.send.assert_not_called()

    def test_no_adoption_on_create_conflict_denied_or_lost_response(self):
        for reply in [OSError("synthetic-secret")] + [
            response({"code": "Failure", "message": "synthetic-secret"}, status=s)
            for s in [401, 403, 404, 409, 412, 429, 500]
        ] + [response(bucket(), status=202), response(bucket(createdBy="other")),
             response(bucket(id=True)), response(bucket(), headers={"etag": "*"})]:
            self.send.reset_mock()
            self.send.side_effect = [reply]
            with self.assertRaises(BucketUncertain) as err:
                self.store.create(REQUEST)
            self.assertNotIn("synthetic-secret", str(err.exception))
            self.assertEqual(self.send.call_count, 1)

    def test_delete_loss_and_all_unexpected_responses_are_uncertain_without_retry(self):
        for reply in [OSError("secret"), response(body=b"", status=202)] + [
            response({"code": "Failure"}, status=s) for s in [401, 403, 404, 409, 412, 429, 500]
        ]:
            self.send.reset_mock()
            self.send.side_effect = [response(bucket()), reply]
            with self.assertRaises(BucketUncertain):
                self.store.delete_empty(ACK, self.record, self.record.receipt, "b" * 32)
            self.assertEqual(self.send.call_count, 2)

    def test_changed_or_unavailable_bucket_blocks_delete(self):
        failures = [response(bucket(**change)) for change in [
            {"id": "other"}, {"metadata": {}}, {"metadata": {**metadata(REQUEST), "extra": "x"}},
            {"versioning": "Enabled"}, {"publicAccessType": "ObjectRead"},
            {"createdBy": "other"}, {"autoTiering": "Auto"}, {"kmsKeyId": "key"},
            {"isReadOnly": 0}, {"objectEventsEnabled": True},
        ]] + [response([]), response(bucket(), headers={"etag": "*"}),
              response({"code": "NotAuthorizedOrNotFound"}, status=404)]
        for reply in failures:
            self.send.reset_mock()
            self.send.side_effect = [reply]
            with self.assertRaises(BucketError):
                self.store.delete_empty(ACK, self.record, self.record.receipt, "b" * 32)
            self.assertEqual(self.send.call_count, 1)

    def test_invalid_inputs_are_rejected_before_dispatch(self):
        for change in [{"scope": replace(REQUEST.scope, repository_id=True)}, {"namespace": "../ns"},
                       {"name": ""}, {"epoch": True}, {"principal": "other"}, {"correlation": "\n"}]:
            with self.assertRaises(BucketError):
                self.store.create(replace(REQUEST, **change))
        for config in [dict(CONFIG, key_file="secret"), dict(CONFIG, tenancy="other"),
                       dict(CONFIG, region="other"), dict(CONFIG, user=False)]:
            with self.assertRaises(BucketError):
                OciRecoveryBucket(config, REQUEST.scope)
        self.send.assert_not_called()

    def test_bad_namespace_transport_and_late_success_are_rejected(self):
        for reply in [response(True), response("../ns"), response(body=b"x" * 262145),
                      response(body=b'{"x":1,"x":1}'), response("ns", status=202)]:
            self.send.side_effect = [reply]
            with self.assertRaises(BucketError):
                self.store.namespace()
        self.send.side_effect = [response(bucket())]
        with patch("troubleshooter_deployment.request_budget.monotonic", side_effect=[0, 21]):
            with self.assertRaises(BucketUncertain):
                self.store.create(REQUEST)

    def test_unknown_field_names_are_bounded_and_close_errors_sanitized(self):
        self.send.side_effect = [response(bucket(**{"\nsecret": 1}))]
        with self.assertRaises(BucketError):
            self.store.read(ACK)
        with patch.object(self.store._client.base_client.session, "close", side_effect=OSError("secret")):
            with self.assertRaises(BucketError) as error:
                self.store.close()
            self.assertNotIn("secret", str(error.exception))
