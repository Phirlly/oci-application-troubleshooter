from dataclasses import replace
import unittest
from unittest.mock import patch

from troubleshooter_deployment.cleanup import completion
from troubleshooter_deployment.oci_audit import AuditError, OciBucketAudit
from troubleshooter_deployment.records import DeleteAcknowledgement
from tests.contract.oci_responses import CONFIG, SyntheticSigner, response
from tests.contract.test_audit_delete_binding import INTENT, RECEIPT, deletion_event
from tests.contract.test_oci_audit import START, END, page


class AuditDeleteRecoveryTests(unittest.TestCase):
    def setUp(self):
        patch("oci.loggingsearch.log_search_client.Signer", return_value=SyntheticSigner()).start()
        self.addCleanup(patch.stopall)
        self.audit = OciBucketAudit(CONFIG, INTENT.ack.identity.scope)
        self.addCleanup(self.audit.close)
        self.send = patch("oci.base_client.OCIHTTPAdapter.send").start()

    def recover(self, intent=INTENT, receipt=RECEIPT):
        return self.audit.reconcile_deletion(intent, receipt, START, END)

    def test_positive_exact_response_confirms_only_bound_request_without_claiming_coverage(self):
        self.send.side_effect = [page([deletion_event()]), page([deletion_event()])]
        scan = self.audit.observe_deletion(INTENT, RECEIPT, START, END)
        self.assertFalse(scan.complete)
        self.assertFalse(hasattr(scan.matches[0], "identity"))
        result = self.recover()
        self.assertEqual(result, DeleteAcknowledgement(INTENT.ack.identity, RECEIPT.removal_id))
        self.assertEqual(completion(RECEIPT, RECEIPT, result), "complete")
        self.assertEqual(completion(RECEIPT, None, result), "cleanup_incomplete")
        self.assertTrue(all(call.args[0].method == "POST" for call in self.send.call_args_list))

    def test_empty_duplicate_or_mismatched_event_cannot_confirm(self):
        variants = [[], [deletion_event(), deletion_event()]]
        for section, key, value in [("response", "status", "404"), ("identity", "principalId", "other"),
                                     ("request", "path", "/replacement"), ("request", "action", "POST")]:
            item = deletion_event()
            item["data"][section][key] = value
            variants.append([item])
        for headers in ({}, {"opc-client-request-id": [INTENT.correlation], "If-Match": ['"new"']},
                        {"opc-client-request-id": ["f" * 32], "If-Match": [INTENT.if_match]}):
            item = deletion_event()
            item["data"]["request"]["headers"] = headers
            variants.append([item])
        for items in variants:
            self.send.side_effect = [page(items)]
            self.assertIsNone(self.recover())

    def test_partial_or_denied_scan_cannot_return_earlier_positive(self):
        for failure in (OSError("secret"), response({"code": "Denied"}, status=403)):
            self.send.reset_mock()
            self.send.side_effect = [page([deletion_event()], headers={"opc-next-page": "next"}), failure]
            with self.assertRaises(AuditError) as caught:
                self.recover()
            self.assertNotIn("secret", str(caught.exception))
            self.assertEqual(self.send.call_count, 2)

    def test_invalid_intent_receipt_or_caller_candidate_cannot_make_requests(self):
        for intent, receipt in [(None, RECEIPT), (INTENT, None),
                                (replace(INTENT, receipt_sha256="0" * 64), RECEIPT),
                                (deletion_event(), RECEIPT)]:
            with self.assertRaises(AuditError):
                self.recover(intent, receipt)
        self.send.assert_not_called()
