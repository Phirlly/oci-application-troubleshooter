from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from troubleshooter_deployment.oci_audit import OciBucketAudit, AuditError
from tests.contract.oci_responses import CONFIG, SyntheticSigner, response
from tests.contract.test_oci_recovery_bucket import ACK, REQUEST

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
END = START + timedelta(minutes=30)


def event(operation="CreateBucket"):
    method, path, status = ("POST", "/n/synthetic-ns/b", "200") if operation == "CreateBucket" else (
        "DELETE", "/n/synthetic-ns/b/synthetic-bucket", "204")
    return {
        "eventId": "12345678-1234-1234-1234-123456789abc",
        "eventTime": "2025-12-31T23:59:59.123Z",
        "eventType": "synthetic.objectstorage." + operation,
        "cloudEventsVersion": "0.1", "eventTypeVersion": "2.0", "source": "synthetic",
        "contentType": "application/json",
        "data": {"eventName": operation, "compartmentId": REQUEST.scope.compartment_id,
                 "resourceName": REQUEST.name, "resourceId": ACK.identity.bucket_id,
                 "identity": {"principalId": REQUEST.principal, "tenantId": REQUEST.scope.tenancy_id},
                 "request": {"action": method, "path": path, "id": "separate-oracle-id",
                             "headers": {"opc-client-request-id": [REQUEST.correlation]}},
                 "response": {"status": status}},
    }


class BucketAuditTests(unittest.TestCase):
    def setUp(self):
        patch("oci.audit.audit_client.Signer", return_value=SyntheticSigner()).start()
        self.addCleanup(patch.stopall)
        self.audit = OciBucketAudit(CONFIG, REQUEST.scope)
        self.addCleanup(self.audit.close)
        self.send = patch("oci.base_client.OCIHTTPAdapter.send").start()

    def scan(self, operation="CreateBucket", **kw):
        return self.audit.observe(ACK, operation, REQUEST.correlation, kw.get("start", START), kw.get("end", END))

    def test_empty_page_continuation_and_normalized_candidate(self):
        self.send.side_effect = [response([], headers={"opc-next-page": "next"}),
                                 response([event()])]
        scan = self.scan()
        self.assertTrue(scan.complete)
        self.assertEqual(len(scan.matches), 1)
        self.assertEqual(scan.matches[0].event_time, "2025-12-31T23:59:59.123Z")
        self.assertEqual(scan.processed_start, START)
        self.assertNotIn("headers", repr(scan))
        requests = [c.args[0] for c in self.send.call_args_list]
        self.assertTrue(all(c.method == "GET" for c in requests))
        self.assertEqual(urlsplit(requests[0].url).path, "/20190901/auditEvents")
        params = parse_qs(urlsplit(requests[0].url).query)
        self.assertEqual(set(params), {"compartmentId", "startTime", "endTime"})
        self.assertEqual(params["compartmentId"], [REQUEST.scope.compartment_id])
        self.assertEqual(parse_qs(urlsplit(requests[1].url).query)["page"], ["next"])

    def test_delete_candidates_do_not_become_cleanup_acknowledgements(self):
        self.send.side_effect = [response([event("DeleteBucket"), event("DeleteBucket")])]
        scan = self.scan("DeleteBucket")
        self.assertEqual(len(scan.matches), 2)  # No deduplication into unique proof.
        self.assertFalse(hasattr(scan.matches[0], "identity"))

    def test_matching_is_strict_and_oracle_id_is_not_client_correlation(self):
        changes = [
            (["data", "eventName"], "GetBucket"), (["data", "resourceId"], "other"),
            (["data", "resourceName"], "other"), (["data", "compartmentId"], "other"),
            (["data", "identity", "principalId"], "other"),
            (["data", "identity", "tenantId"], "other"),
            (["data", "request", "path"], "/n/synthetic-ns/b/other"),
            (["data", "request", "action"], "GET"),
            (["data", "request", "headers"], {"opc-request-id": [REQUEST.correlation]}),
            (["data", "request", "headers"], {"opc-client-request-id": REQUEST.correlation}),
            (["data", "request", "headers"], {"opc-client-request-id": [REQUEST.correlation, REQUEST.correlation]}),
            (["data", "request", "headers"], {"opc-client-request-id": [REQUEST.correlation],
                                            "OPC-CLIENT-REQUEST-ID": [REQUEST.correlation]}),
            (["data", "response", "status"], 200), (["data", "response", "status"], "202"),
            (["eventId"], "bad"), (["eventTime"], "not-a-date"),
            (["eventTime"], "2026-01-01T00:00:00"), (["eventType"], None),
            (["cloudEventsVersion"], "unknown"), (["data"], None),
        ]
        for path, value in changes:
            item = deepcopy(event())
            target = item
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            self.send.side_effect = [response([item])]
            self.assertEqual(self.scan().matches, (), path)

    def test_incomplete_malformed_overflow_and_repeated_pages_fail_closed(self):
        for replies in [
            [response({})], [response([True])], [response([], status=202)],
            [response([], headers={"opc-next-page": ""})],
            [response([], headers={"opc-next-page": "x" * 1025})],
            [response([], headers={"opc-next-page": "same"}) for _ in range(2)],
            [response([], headers={"opc-next-page": str(i)}) for i in range(5)],
            [response([{}] * 1025)], [response(body=b"x" * 262145)],
            [response([], headers={"opc-next-page": "next"}), OSError("secret")],
            [response({"code": "Denied"}, status=403)],
        ]:
            self.send.reset_mock()
            self.send.side_effect = replies
            with self.assertRaises(AuditError) as error:
                self.scan()
            self.assertNotIn("secret", str(error.exception))
            self.assertLessEqual(self.send.call_count, 5)

    def test_invalid_query_is_rejected_without_network(self):
        for start, end in [(START, START), (START, START + timedelta(hours=2)),
                           (START.replace(tzinfo=None), END), (True, END),
                           (START + timedelta(seconds=1), END)]:
            with self.assertRaises(AuditError):
                self.scan(start=start, end=end)
        with self.assertRaises(AuditError):
            self.scan("Other")
        self.send.assert_not_called()

    def test_config_close_and_late_scan_failures_are_sanitized(self):
        with self.assertRaises(AuditError):
            OciBucketAudit(dict(CONFIG, tenancy="other"), REQUEST.scope)
        self.send.side_effect = [response([event()])]
        with patch("troubleshooter_deployment.request_budget.monotonic", side_effect=[0, 21]):
            with self.assertRaises(AuditError):
                self.scan()
        with patch.object(self.audit._client.base_client.session, "close", side_effect=OSError("secret")):
            with self.assertRaises(AuditError):
                self.audit.close()
