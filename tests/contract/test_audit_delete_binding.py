from dataclasses import replace
from hashlib import sha256
import unittest
from unittest.mock import patch

from troubleshooter_deployment.bucket_deletion import BucketDeleteIntent
from troubleshooter_deployment.cleanup import prepare_purge
from troubleshooter_deployment.codec import encode_receipt
from troubleshooter_deployment.oci_audit import AuditError, OciBucketAudit
from troubleshooter_deployment.policy import bootstrap
from tests.contract.oci_responses import CONFIG, SyntheticSigner, response
from tests.contract.test_oci_audit import START, END, event, page
from tests.contract.test_oci_recovery_bucket import ACK
from tests.deployment.fixtures import ATTEMPT, REMOVAL

RECEIPT = prepare_purge(bootstrap(ACK.identity, ACK.identity, ATTEMPT), REMOVAL).receipt
INTENT = BucketDeleteIntent(ACK, REMOVAL, sha256(encode_receipt(RECEIPT)).hexdigest(),
                           "b" * 32, '"Mixed-Case-Revision"')


def deletion_event(intent=INTENT):
    item = event("DeleteBucket")
    item["data"]["additionalDetails"]["bucketName"] = intent.ack.request.name
    path = "/n/" + intent.ack.request.namespace + "/b/" + intent.ack.request.name
    item["data"]["resourceId"] = item["data"]["request"]["path"] = path
    item["data"]["request"]["headers"] = {
        "opc-client-request-id": [intent.correlation], "If-Match": [intent.if_match],
    }
    return item


class AuditDeleteBindingTests(unittest.TestCase):
    def setUp(self):
        patch("oci.loggingsearch.log_search_client.Signer", return_value=SyntheticSigner()).start()
        self.addCleanup(patch.stopall)
        self.audit = OciBucketAudit(CONFIG, ACK.identity.scope)
        self.addCleanup(self.audit.close)
        self.send = patch("oci.base_client.OCIHTTPAdapter.send").start()

    def scan(self, intent=INTENT, receipt=RECEIPT, start=START, end=END):
        return self.audit.observe_deletion(intent, receipt, start, end)

    def test_exact_value_and_case_insensitive_header_remain_only_observations(self):
        for key in ("if-match", "If-Match", "IF-MATCH"):
            item = deletion_event()
            item["data"]["request"]["headers"] = {
                "OPC-Client-Request-ID": [INTENT.correlation], key: [INTENT.if_match],
            }
            self.send.side_effect = [page([item])]
            scan = self.scan()
            self.assertEqual(len(scan.matches), 1)
            self.assertFalse(scan.complete)
            self.assertFalse(hasattr(scan.matches[0], "identity"))
            self.assertNotIn(INTENT.if_match, repr(scan))
        for call in self.send.call_args_list:
            self.assertEqual(call.args[0].method, "POST")
            self.assertEqual(call.kwargs["timeout"], (5, 15))
            self.assertTrue(call.kwargs["verify"])

    def test_missing_malformed_duplicate_or_changed_headers_do_not_match(self):
        variants = [{}, {"If-Match": INTENT.if_match}, {"If-Match": []},
                    {"If-Match": [INTENT.if_match, INTENT.if_match]},
                    {"If-Match": [INTENT.if_match], "if-match": [INTENT.if_match]},
                    {"If-Match": [INTENT.if_match.lower()]},
                    {"If-Match": [INTENT.if_match.strip('"')]},
                    {"If-Match": [None]}, {"If-Match": ["*"]}]
        for headers in variants:
            item = deletion_event()
            item["data"]["request"]["headers"] = {"opc-client-request-id": [INTENT.correlation], **headers}
            self.send.side_effect = [page([item])]
            self.assertEqual(self.scan().matches, (), headers)
        for section, key, value in [("request", "action", "POST"), ("request", "path", "/other"),
                                     ("response", "status", "200"), ("identity", "principalId", "other")]:
            item = deletion_event()
            item["data"][section][key] = value
            self.send.side_effect = [page([item])]
            self.assertEqual(self.scan().matches, ())
        item = deletion_event()
        item["data"]["request"]["headers"]["opc-client-request-id"] = [ACK.request.correlation]
        self.send.side_effect = [page([item])]
        self.assertEqual(self.scan().matches, ())

    def test_ordinary_observe_does_not_require_if_match(self):
        item = deletion_event()
        del item["data"]["request"]["headers"]["If-Match"]
        self.send.side_effect = [page([item])]
        self.assertEqual(len(self.audit.observe(ACK, "DeleteBucket", INTENT.correlation, START, END).matches), 1)

    def test_invalid_intent_receipt_scope_or_window_fails_before_network(self):
        for intent, receipt in [(None, RECEIPT), (INTENT, None),
                                (replace(INTENT, receipt_sha256="0" * 64), RECEIPT),
                                (replace(INTENT, if_match="*"), RECEIPT),
                                (INTENT, replace(RECEIPT, removal_id="f" * 32))]:
            with self.assertRaises(AuditError) as caught:
                self.scan(intent, receipt)
            self.assertEqual(caught.exception.scan_reason, "query")
        with patch.object(self.audit, "_scope", replace(ACK.identity.scope, region="other")):
            with self.assertRaises(AuditError):
                self.scan()
        with self.assertRaises(AuditError):
            self.scan(end=START)
        self.send.assert_not_called()

    def test_duplicate_pages_are_preserved_and_partial_or_over_budget_scans_fail(self):
        self.send.side_effect = [page([deletion_event()], headers={"opc-next-page": "next"}),
                                page([deletion_event()])]
        scan = self.scan()
        self.assertEqual(len(scan.matches), 2)
        self.assertFalse(scan.complete)
        for replies in [
            [page([deletion_event()], headers={"opc-next-page": "next"}), OSError("secret")],
            [page(headers={"opc-next-page": str(i)}) for i in range(5)],
            [page(headers={"opc-next-page": "same"}) for _ in range(2)],
            [response(body=b"x" * 262145)], [page([deletion_event()] * 11)],
        ]:
            self.send.reset_mock()
            self.send.side_effect = replies
            with self.assertRaises(AuditError):
                self.scan()
            self.assertLessEqual(self.send.call_count, 5)
        self.send.side_effect = [page([deletion_event()])]
        with patch("troubleshooter_deployment.request_budget.monotonic", side_effect=[0, 21]):
            with self.assertRaises(AuditError):
                self.scan()
