from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from troubleshooter_deployment.oci_audit import OciBucketAudit, AuditError, AuditScan
from tests.contract.oci_responses import CONFIG, SyntheticSigner, response
from tests.contract.test_oci_recovery_bucket import ACK, REQUEST

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
END = START + timedelta(minutes=30)


def event(operation="CreateBucket"):
    method, path, status = ("POST", "/n/synthetic-ns/b", "200") if operation == "CreateBucket" else (
        "DELETE", "/n/synthetic-ns/b/synthetic-bucket", "204")
    return {
        "id": "synthetic:event/1", "time": "2025-12-31T23:59:59.123Z",
        "type": "com.oraclecloud.objectstorage." + operation.lower(),
        "specversion": "1.0", "dataschema": "2.0", "source": path,
        "data": {"eventName": operation, "compartmentId": REQUEST.scope.compartment_id,
                 "additionalDetails": {"bucketName": REQUEST.name, "namespace": REQUEST.namespace},
                 "resourceId": path,
                 "identity": {"principalId": REQUEST.principal, "tenantId": REQUEST.scope.tenancy_id},
                 "request": {"action": method, "path": path, "id": "separate-oracle-id",
                             "headers": {"opc-client-request-id": [REQUEST.correlation]}},
                 "response": {"status": status}},
    }


def page(events=(), **kwargs):
    return response({"results": [{"data": {"datetime": 1767225599123, "logContent": item}}
                                 for item in events]}, **kwargs)


class BucketAuditTests(unittest.TestCase):
    def setUp(self):
        patch("oci.loggingsearch.log_search_client.Signer", return_value=SyntheticSigner()).start()
        self.addCleanup(patch.stopall)
        self.audit = OciBucketAudit(CONFIG, REQUEST.scope)
        self.addCleanup(self.audit.close)
        self.send = patch("oci.base_client.OCIHTTPAdapter.send").start()

    def scan(self, operation="CreateBucket", **kw):
        return self.audit.observe(ACK, operation, REQUEST.correlation, kw.get("start", START), kw.get("end", END))

    def test_empty_page_continuation_and_normalized_candidate(self):
        self.send.side_effect = [page(headers={"opc-next-page": "next"}), page([event()])]
        scan = self.scan()
        self.assertFalse(scan.complete)
        self.assertEqual(len(scan.matches), 1)
        candidate = scan.matches[0]
        self.assertEqual(candidate.event_time, "2025-12-31T23:59:59.123Z")
        self.assertEqual((candidate.event_id, candidate.source, candidate.data_schema),
                         ("synthetic:event/1", "/n/synthetic-ns/b", "2.0"))
        self.assertEqual((scan.search_start, scan.search_end), (START, END))
        self.assertNotIn("headers", repr(scan))
        requests = [c.args[0] for c in self.send.call_args_list]
        self.assertTrue(all(c.method == "POST" for c in requests))
        self.assertEqual(requests[0].url.split("?")[0],
                         "https://logging.us-ashburn-1.oci.oraclecloud.com/20190909/search")
        self.assertEqual(parse_qs(urlsplit(requests[0].url).query), {"limit": ["10"]})
        body = json.loads(requests[0].body)
        self.assertEqual(body, {
            "timeStart": "2026-01-01T00:00:00Z", "timeEnd": "2026-01-01T00:30:00Z", "isReturnFieldInfo": False,
            "searchQuery": f'search "{REQUEST.scope.compartment_id}/_Audit" | where '
                           "data.eventName = 'CreateBucket' or data.eventName = 'DeleteBucket'",
        })
        self.assertEqual(json.loads(requests[1].body), body)
        self.assertEqual(parse_qs(urlsplit(requests[1].url).query), {"limit": ["10"], "page": ["next"]})
        for call in self.send.call_args_list:
            self.assertEqual(call.kwargs["timeout"], (5, 15))
            self.assertTrue(call.kwargs["verify"])
            self.assertTrue(call.kwargs["stream"])
            self.assertEqual(call.kwargs["proxies"], {})

    def test_empty_full_and_duplicate_pages_never_establish_completeness(self):
        for items in ([], [event()] * 10):
            self.send.side_effect = [page(items)]
            scan = self.scan()
            self.assertFalse(scan.complete)
            self.assertEqual(len(scan.matches), len(items))
        self.send.side_effect = [page([event()], headers={"opc-next-page": "next"}), page([event()])]
        self.assertEqual(len(self.scan().matches), 2)
        with self.assertRaises(TypeError):
            AuditScan(START, END, (), complete=True)

    def test_delete_candidates_do_not_become_cleanup_acknowledgements(self):
        self.send.side_effect = [page([event("DeleteBucket"), event("DeleteBucket")])]
        scan = self.scan("DeleteBucket")
        self.assertEqual(len(scan.matches), 2)
        self.assertFalse(hasattr(scan.matches[0], "identity"))
        self.assertFalse(hasattr(scan.matches[0], "bucket_id"))

    def test_resource_name_is_optional_and_content_type_defaults_to_json(self):
        for name in (None, "synthetic-unreliable-display-name"):
            item = event()
            item["data"]["resourceName"] = name
            item["datacontenttype"] = "application/json"
            item["data"]["request"]["headers"] = {"OPC-Client-Request-ID": [REQUEST.correlation]}
            self.send.side_effect = [page([item])]
            self.assertEqual(len(self.scan().matches), 1)

    def test_matching_is_strict_and_oracle_id_is_not_client_correlation(self):
        changes = [
            (["data", "eventName"], "GetBucket"), (["data", "resourceId"], ACK.identity.bucket_id),
            (["data", "additionalDetails", "bucketName"], "other"),
            (["data", "additionalDetails", "namespace"], "other"),
            (["data", "compartmentId"], "other"), (["data", "identity", "principalId"], "other"),
            (["data", "identity", "tenantId"], "other"), (["data", "request", "path"], "/other"),
            (["data", "request", "action"], "GET"),
            (["data", "request", "headers"], {"opc-request-id": [REQUEST.correlation]}),
            (["data", "request", "headers"], {"opc-client-request-id": REQUEST.correlation}),
            (["data", "request", "headers"], {"opc-client-request-id": ["0" * 32]}),
            (["data", "request", "headers"], {"opc-client-request-id": [REQUEST.correlation] * 2}),
            (["data", "request", "headers"], {"opc-client-request-id": [REQUEST.correlation],
                                            "OPC-CLIENT-REQUEST-ID": [REQUEST.correlation]}),
            (["data", "request", "headers"], []),
            (["data", "response", "status"], 200), (["data", "response", "status"], "202"),
            (["id"], ""), (["id"], "x" * 257), (["source"], "bad\nsource"),
            (["time"], "not-a-date"), (["time"], "2026-01-01T00:00:00"),
            (["time"], "2026-02-30T00:00:00Z"), (["type"], None),
            (["type"], "com.oraclecloud.objectstorage.deletebucket"),
            (["specversion"], "0.1"), (["dataschema"], None),
            (["datacontenttype"], None), (["datacontenttype"], "text/plain"), (["data"], None),
        ]
        for path, value in changes:
            item = deepcopy(event())
            target = item
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            self.send.side_effect = [page([item])]
            self.assertEqual(self.scan().matches, (), path)
        for key in ("data", "id", "time", "source", "specversion", "dataschema"):
            item = event()
            del item[key]
            self.send.side_effect = [page([item])]
            self.assertEqual(self.scan().matches, (), key)

    def test_incomplete_malformed_overflow_and_repeated_pages_fail_closed(self):
        for replies in [
            [response({})], [response({"results": None})], [response({"results": [True]})],
            [response({"results": [{"data": {"datetime": True, "logContent": event()}}]})],
            [response({"results": [{"data": {"datetime": 1, "logContent": []}}]})],
            [page(status=202)], [page(headers={"opc-next-page": ""})],
            [page(headers={"opc-next-page": "x" * 1025})],
            [page(headers={"opc-next-page": "same"}) for _ in range(2)],
            [page(headers={"opc-next-page": str(i)}) for i in range(5)],
            [page([event()] * 11)], [response(body=b"x" * 262145)],
            [page([event()], headers={"opc-next-page": "next"}), OSError("secret")],
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
        for ack, correlation in [(ACK, "bad"), (replace(ACK, request=replace(REQUEST, name="bad'query")), REQUEST.correlation),
                                 (replace(ACK, request=replace(REQUEST, scope=replace(REQUEST.scope, region="other"))),
                                  REQUEST.correlation)]:
            with self.assertRaises(AuditError):
                self.audit.observe(ack, "CreateBucket", correlation, START, END)
        self.send.assert_not_called()

    def test_config_close_and_late_scan_failures_are_sanitized(self):
        with self.assertRaises(AuditError):
            OciBucketAudit(dict(CONFIG, tenancy="other"), REQUEST.scope)
        self.send.side_effect = [page([event()])]
        with patch("troubleshooter_deployment.request_budget.monotonic", side_effect=[0, 21]):
            with self.assertRaises(AuditError):
                self.scan()
        with patch.object(self.audit._client.base_client.session, "close", side_effect=OSError("secret")):
            with self.assertRaises(AuditError):
                self.audit.close()
