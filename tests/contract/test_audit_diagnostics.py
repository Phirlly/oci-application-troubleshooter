"""Synthetic rejection reasons through the transport, Audit and worker boundary."""
from copy import deepcopy
from contextlib import redirect_stdout
from io import StringIO
import os
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from oci.exceptions import ConnectTimeout, RequestException, ServiceError
from tests.contract.oci_responses import CONFIG, SyntheticSigner, response
from tests.contract.test_oci_audit import START, END, page, event
from tests.contract.test_oci_recovery_bucket import ACK, REQUEST
from tests.live import proof_worker
from tests.live.proof_flow import observe
from tests.live.proof_state import ProofStore, new_state
from tests.live.recovery_proof import supervise
from troubleshooter_deployment.oci_audit import AuditError, OciBucketAudit
from troubleshooter_deployment.oci_transport import BoundedOciSession, TransportError
from troubleshooter_deployment.request_budget import BudgetError
from troubleshooter_deployment.records import Scope


class TransportReasonTests(unittest.TestCase):
    def setUp(self):
        self.session = BoundedOciSession("https://audit.us-ashburn-1.oraclecloud.com")
        self.addCleanup(self.session.close)

    def reject(self, reply):
        with patch("oci.base_client.OCIHTTPAdapter.send", return_value=reply) as send:
            with self.assertRaises(TransportError) as caught:
                self.session.get("https://audit.us-ashburn-1.oraclecloud.com/20190901/auditEvents")
        send.assert_called_once()
        self.assertEqual(str(caught.exception), "oci_transport_rejected")
        self.assertTrue(reply.raw.closed)
        return caught.exception.reason

    def test_reasons_are_fixed_and_response_content_never_becomes_diagnostics(self):
        cases = [
            (response(body=b"secret", status=302), "redirect"),
            (response({}, headers={"Content-Encoding": "synthetic-secret"}), "content_encoding"),
            (response({}, headers={"Content-Length": "synthetic-secret"}), "content_length_invalid"),
            (response({}, headers={"Content-Length": "900000"}), "content_length_limit"),
            (response(body=b"x" * 262145, headers={"Content-Length": None}), "body_limit"),
            (response({}, headers={"Content-Length": "3"}), "content_length_mismatch"),
            (response(body=b'{"secret":1,"secret":2}'), "json_duplicate_key"),
            (response(body=b"NaN"), "json_nonfinite"),
            (response(body=b"synthetic-secret"), "json_invalid"),
        ]
        for reply, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(self.reject(reply), reason)

    def test_read_failure_and_read_bound_are_preserved(self):
        reply = response({})
        reply.raw.read = Mock(side_effect=OSError("synthetic-secret"))
        self.assertEqual(self.reject(reply), "body_read")
        reply.raw.read.assert_called_once_with(262145, decode_content=False)

    def test_primary_failure_survives_close_failure_and_close_only_has_its_own_reason(self):
        for body, reason in [(b"{}", "response_close"), (b"synthetic-secret", "json_invalid")]:
            reply = response(body=body)
            original = reply.close
            def broken_close():
                original()
                raise OSError("synthetic-secret")
            reply.close = broken_close
            self.assertEqual(self.reject(reply), reason)

    def test_unexpected_header_failure_and_unknown_constructor_reasons_are_sanitized(self):
        reply = response({})
        reply.headers.get = Mock(side_effect=RuntimeError("synthetic-secret"))
        self.assertEqual(self.reject(reply), "response_headers")
        for value in ("synthetic-secret", None, [], {}):
            error = TransportError(value)
            self.assertEqual(error.reason, "unspecified")
            self.assertEqual(str(error), "oci_transport_rejected")
            self.assertIsNone(AuditError(value).transport_reason)


class AuditReasonTests(unittest.TestCase):
    def test_scan_failures_are_fixed_and_preserve_bounds(self):
        with patch("oci.loggingsearch.log_search_client.Signer", return_value=SyntheticSigner()):
            audit = OciBucketAudit(CONFIG, REQUEST.scope)
        self.addCleanup(audit.close)
        cases = [
            ([RequestException("synthetic-secret")], "request"),
            ([ConnectTimeout("synthetic-secret")], "request"),
            ([RuntimeError("synthetic-secret")], "request"),
            ([BudgetError()], "budget"),
            ([response({})], "response"),
            ([page(status=202)], "response"),
            ([response({"results": [True]})], "response"),
            ([page([event()] * 11)], "event_limit"),
            ([page([event()] * 10, headers={"opc-next-page": "next"}), page([event()] * 11)], "event_limit"),
            ([page(headers={"opc-next-page": "next"}), RequestException("synthetic-secret")], "request"),
            ([page(headers={"opc-next-page": ""})], "pagination"),
            ([page(headers={"opc-next-page": "same"}) for _ in range(2)], "pagination"),
            ([page(headers={"opc-next-page": str(i)}) for i in range(5)], "page_limit"),
        ]
        for status in (400, 401, 403, 404, 408, 409, 429, 500, 502, 503, 504):
            cases.append(([response({"code": "synthetic-secret"}, status=status)], "service_" + str(status)))
        for status in (418, "403", None, True):
            cases.append(([ServiceError(status, "synthetic-secret", {}, "synthetic-secret")], "service"))
        for replies, reason in cases:
            with self.subTest(reason=reason), patch("oci.base_client.OCIHTTPAdapter.send", side_effect=replies) as send:
                with self.assertRaises(AuditError) as caught:
                    audit.observe(ACK, "CreateBucket", REQUEST.correlation, START, END)
            self.assertEqual(caught.exception.scan_reason, reason)
            self.assertIsNone(caught.exception.transport_reason)
            self.assertEqual(str(caught.exception), "audit_scan_unavailable_or_incomplete")
            self.assertEqual(send.call_count, len(replies))
        with patch("oci.base_client.OCIHTTPAdapter.send") as send:
            with self.assertRaises(AuditError) as caught:
                audit.observe(ACK, "CreateBucket", REQUEST.correlation, START, START)
            self.assertEqual(caught.exception.scan_reason, "query")
            send.assert_not_called()
        for reason in ("synthetic-secret", None, [], {}):
            self.assertIsNone(AuditError(scan_reason=reason).scan_reason)

    def test_actual_sdk_preserves_reason_and_never_accepts_a_partial_scan(self):
        with patch("oci.loggingsearch.log_search_client.Signer", return_value=SyntheticSigner()):
            audit = OciBucketAudit(CONFIG, REQUEST.scope)
        self.addCleanup(audit.close)
        for prefix in ([], [page(headers={"opc-next-page": "next"})]):
            with patch("oci.base_client.OCIHTTPAdapter.send",
                       side_effect=prefix + [response(body=b"synthetic-secret")]) as send:
                with self.assertRaises(AuditError) as caught:
                    audit.observe(ACK, "CreateBucket", REQUEST.correlation, START, END)
            self.assertEqual(caught.exception.transport_reason, "json_invalid")
            self.assertEqual(str(caught.exception), "audit_scan_unavailable_or_incomplete")
            self.assertEqual(send.call_count, len(prefix) + 1)
        for failure in (BudgetError(), OSError("synthetic-secret")):
            with patch("oci.base_client.OCIHTTPAdapter.send", side_effect=failure):
                with self.assertRaises(AuditError) as caught:
                    audit.observe(ACK, "CreateBucket", REQUEST.correlation, START, END)
            self.assertIsNone(caught.exception.transport_reason)

    def test_failed_observation_preserves_previous_counts_window_and_ownership(self):
        state = new_state(REQUEST.scope, REQUEST.principal, REQUEST.namespace)
        state.update(stack_status="acknowledged", stack_id="synthetic-stack",
                     bucket_status="acknowledged", bucket_id="synthetic-bucket",
                     audit_counts=[1, 1], audit_window=[START.isoformat(), END.isoformat()])
        previous = deepcopy(state)
        services, store = Mock(), Mock()
        services.audit.side_effect = [Mock(matches=(object(),)), AuditError("body_limit")]
        with self.assertRaises(AuditError):
            observe(store, state, services, search_start=START)
        self.assertEqual(state, previous)
        store.write.assert_not_called()


class AuditWorkerDiagnosticTests(unittest.TestCase):
    def test_help_and_missing_optin_work_without_importing_oci(self):
        for args, expected in [(["--help"], 0), (["observe"], 2)]:
            code = ("import runpy, sys; sys.modules['oci'] = None; "
                    f"sys.argv = {['proof', *args]!r}; "
                    "runpy.run_module('tests.live.recovery_proof', run_name='__main__')")
            result = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=5)
            self.assertEqual(result.returncode, expected, result.stderr.decode())

    def test_failed_observe_does_not_print_previous_successful_counts(self):
        from tests.live import recovery_proof
        state = {"stack_status": "deleted", "bucket_status": "deleted",
                 "receipt_status": "survived", "audit_counts": [1, 1]}
        for status, expected in [("audit_transport_body_limit", 1),
                                 ("worker_finished_check_state", 0)]:
            output = StringIO()
            with patch.object(sys, "argv", ["proof", "observe", "--allow-live", "--profile", "synthetic"]), patch(
                    "tests.live.recovery_proof.ensure_directory"), patch(
                    "tests.live.recovery_proof.ProofStore") as store, patch(
                    "tests.live.recovery_proof.supervise", return_value=status), redirect_stdout(output):
                store.return_value.__enter__.return_value.read.return_value = state
                self.assertEqual(recovery_proof.main(), expected)
            self.assertEqual("audit_candidates=1,1" in output.getvalue(), expected == 0)
            self.assertEqual("audit_coverage=unknown" in output.getvalue(), expected == 0)
            self.assertIn(status, output.getvalue())

    def test_actual_worker_returns_reason_code_without_writing_state_or_calling_mutations(self):
        from tests.live.recovery_proof import audit_diagnostic_exits
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp).resolve() / "proof"
            scope = Scope(1, "recovery-proof", CONFIG["tenancy"],
                          REQUEST.scope.compartment_id, CONFIG["region"])
            state = new_state(scope, REQUEST.principal, REQUEST.namespace)
            state.update(stack_status="acknowledged", stack_id="synthetic-stack",
                         bucket_status="acknowledged", bucket_id="synthetic-bucket")
            with ProofStore(path) as store:
                store.write(state)
                original = (path / "state.json").read_bytes()
                argv = ["worker", "observe", "--allow-live", "--profile", "synthetic",
                        "--search-start", START.isoformat(), "--search-minutes", "1",
                        "--lock-fd", str(os.dup(store.lock_fd))]
                with patch.object(sys, "argv", argv), patch.object(proof_worker, "STATE_PATH", path), patch(
                        "tests.live.proof_worker.credentials", return_value=CONFIG), patch(
                        "oci.object_storage.object_storage_client.Signer", return_value=SyntheticSigner()), patch(
                        "oci.resource_manager.resource_manager_client.Signer", return_value=SyntheticSigner()), patch(
                        "oci.loggingsearch.log_search_client.Signer", return_value=SyntheticSigner()), patch(
                        "oci.base_client.OCIHTTPAdapter.send", return_value=response(body=b"synthetic-secret")) as send:
                    code = proof_worker.main()
                self.assertEqual(code, audit_diagnostic_exits()["json_invalid"])
                self.assertEqual((path / "state.json").read_bytes(), original)
                send.assert_called_once()
                request = send.call_args.args[0]
                self.assertEqual(request.method, "POST")
                self.assertEqual(request.url.split("?")[0],
                                 "https://logging.us-ashburn-1.oci.oraclecloud.com/20190909/search")
                from datetime import datetime, timedelta
                query = json.loads(request.body)
                self.assertEqual(datetime.fromisoformat(query["timeEnd"])
                                 - datetime.fromisoformat(query["timeStart"]), timedelta(minutes=1))

    def test_worker_and_supervisor_carry_scan_failures_without_provider_content(self):
        from tests.live.recovery_proof import audit_diagnostic_exits, audit_scan_exits
        codes = audit_scan_exits()
        self.assertFalse(set(codes.values()) & set(audit_diagnostic_exits().values()))
        for reason, code in codes.items():
            argv = ["worker", "observe", "--allow-live", "--profile", "synthetic", "--lock-fd", "123"]
            with patch.object(sys, "argv", argv), patch("tests.live.proof_worker.ProofStore") as store, patch(
                    "tests.live.proof_worker.os.close"):
                store.return_value.__enter__.side_effect = AuditError(scan_reason=reason)
                self.assertEqual(proof_worker.main(), code)
            process = Mock()
            process.wait.return_value = process.poll.return_value = code
            with patch("tests.live.recovery_proof.subprocess.Popen", return_value=process) as launch:
                self.assertEqual(supervise(["observe"], Mock(lock_fd=7)), "audit_scan_" + reason)
                self.assertEqual(supervise(["cleanup"], Mock(lock_fd=7)), "worker_failed_check_state")
            self.assertEqual(launch.call_args.kwargs["stdout"], subprocess.DEVNULL)
            self.assertEqual(launch.call_args.kwargs["stderr"], subprocess.DEVNULL)

    def test_actual_worker_saves_only_correlated_counts_and_preserves_ownership(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp).resolve() / "proof"
            scope = Scope(1, "recovery-proof", CONFIG["tenancy"], REQUEST.scope.compartment_id, CONFIG["region"])
            state = new_state(scope, REQUEST.principal, REQUEST.namespace)
            state.update(stack_status="acknowledged", stack_id="synthetic-stack",
                         bucket_status="acknowledged", bucket_id="synthetic-bucket")
            items = []
            for operation, key in (("CreateBucket", "create_correlation"), ("DeleteBucket", "delete_correlation")):
                item = event(operation)
                item["data"]["additionalDetails"]["bucketName"] = state["name"]
                item["data"]["request"]["headers"] = {"opc-client-request-id": [state[key]]}
                if operation == "DeleteBucket":
                    item["data"]["resourceId"] = item["data"]["request"]["path"] = "/n/synthetic-ns/b/" + state["name"]
                items.append(item)
            with ProofStore(path) as store:
                store.write(state)
                argv = ["worker", "observe", "--allow-live", "--profile", "synthetic",
                        "--search-start", START.isoformat(), "--search-minutes", "30",
                        "--lock-fd", str(os.dup(store.lock_fd))]
                replies = [page(items), page(items)]
                with patch.object(sys, "argv", argv), patch.object(proof_worker, "STATE_PATH", path), patch(
                        "tests.live.proof_worker.credentials", return_value=CONFIG), patch(
                        "oci.object_storage.object_storage_client.Signer", return_value=SyntheticSigner()), patch(
                        "oci.resource_manager.resource_manager_client.Signer", return_value=SyntheticSigner()), patch(
                        "oci.loggingsearch.log_search_client.Signer", return_value=SyntheticSigner()), patch(
                        "oci.base_client.OCIHTTPAdapter.send", side_effect=replies) as send:
                    self.assertEqual(proof_worker.main(), 0)
                self.assertEqual(send.call_count, 2)
                for call in send.call_args_list:
                    self.assertEqual(call.args[0].method, "POST")
                    self.assertEqual(call.args[0].url.split("?")[0],
                                     "https://logging.us-ashburn-1.oci.oraclecloud.com/20190909/search")
                self.assertTrue(all(reply.raw.closed for reply in replies))
                expected = dict(state, audit_counts=[1, 1], audit_window=[START.isoformat(), END.isoformat()])
                self.assertEqual(store.read(), expected)

    def test_supervisor_emits_only_fixed_statuses_and_discards_both_output_streams(self):
        from tests.live.recovery_proof import audit_diagnostic_exits
        codes = audit_diagnostic_exits()
        cases = [(code, ["observe"], "audit_transport_" + reason)
                 for reason, code in codes.items()]
        cases += [(1, ["observe"], "worker_failed_check_state"),
                  (-9, ["observe"], "worker_failed_check_state"),
                  (255, ["observe"], "worker_failed_check_state"),
                  (next(iter(codes.values())), ["run"], "worker_failed_check_state")]
        for code, args, expected in cases:
            process = Mock()
            process.wait.return_value = process.poll.return_value = code
            with patch("tests.live.recovery_proof.subprocess.Popen", return_value=process) as launch:
                self.assertEqual(supervise(args, Mock(lock_fd=7)), expected)
            self.assertEqual(launch.call_args.kwargs["stdout"], subprocess.DEVNULL)
            self.assertEqual(launch.call_args.kwargs["stderr"], subprocess.DEVNULL)


if __name__ == "__main__":
    unittest.main()
