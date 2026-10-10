from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from tests.live.proof_state import ProofStore, ProofStateError, new_state, validate_state
from tests.live.proof_flow import advance, observe
from tests.live.proof_stack import ProofStack, StackError, fixture_zip
from tests.live.recovery_proof import supervise
from tests.contract.oci_responses import CONFIG, OCI_IDENTITY, SyntheticSigner, response
from tests.contract.test_oci_recovery_bucket import REQUEST
from troubleshooter_deployment.recovery_bucket import BucketAcknowledgement, BucketObservation
from troubleshooter_deployment.records import Identity
from troubleshooter_deployment.codec import decode
from troubleshooter_deployment.oci_audit import AuditScan


class MemoryProof:
    def __init__(self, state):
        self.state, self.saved = state, []

    def write(self, state):
        validate_state(state)
        self.state = deepcopy(state)
        self.saved.append(deepcopy(state))


class Services:
    def __init__(self, store):
        self.store, self.calls, self.receipt = store, [], None
        self.fail = None

    def called(self, name):
        self.calls.append(name)
        if self.fail == name:
            raise RuntimeError("synthetic-secret")

    def create_stack(self, state):
        assert self.store.state["stack_status"] == "uncertain"
        self.called("create_stack")
        return "synthetic-stack"

    def check_stack(self, state):
        self.called("check_stack")

    def create_bucket(self, request):
        assert self.store.state["bucket_status"] == "uncertain"
        self.called("create_bucket")
        return BucketAcknowledgement(request, Identity(request.scope, "synthetic-bucket", request.epoch))

    def check_bucket(self, ack):
        self.called("check_bucket")
        return BucketObservation('"v1"', ())

    def persist(self, state, record):
        assert decode(self.store.state["record"].encode()) == record
        assert self.store.state["receipt_status"] == "uncertain"
        self.called("persist")
        self.receipt = record.receipt
        return self.receipt

    def read_receipt(self, state, ack):
        self.called("read_receipt")
        return self.receipt

    def delete_bucket(self, ack, record, receipt, correlation):
        assert self.store.state["bucket_status"] == "delete_uncertain"
        self.called("delete_bucket")
        from troubleshooter_deployment.records import DeleteAcknowledgement
        return DeleteAcknowledgement(ack.identity, record.receipt.removal_id)

    def delete_stack(self, state):
        assert self.store.state["stack_status"] == "delete_uncertain"
        self.called("delete_stack")

    def audit(self, ack, operation, correlation, start, end):
        self.called("audit")
        return AuditScan(start, end, ())


class ProofFlowTests(unittest.TestCase):
    def setUp(self):
        self.state = new_state(REQUEST.scope, REQUEST.principal, REQUEST.namespace)
        self.store = MemoryProof(self.state)
        self.services = Services(self.store)

    def test_success_orders_receipt_before_delete_and_retains_stack_until_cleanup(self):
        advance(self.store, self.state, self.services, cleanup=False)
        self.assertEqual(self.state["bucket_status"], "deleted")
        self.assertEqual(self.state["receipt_status"], "survived")
        self.assertEqual(self.state["stack_status"], "acknowledged")
        calls = self.services.calls
        self.assertLess(calls.index("create_stack"), calls.index("create_bucket"))
        self.assertLess(calls.index("read_receipt"), calls.index("delete_bucket"))
        advance(self.store, self.state, self.services, cleanup=True)
        self.assertEqual(self.state["stack_status"], "deleted")
        self.assertEqual(self.services.calls.count("create_stack"), 1)
        self.assertEqual(self.services.calls.count("create_bucket"), 1)
        self.assertEqual(self.services.calls.count("delete_bucket"), 1)

    def test_each_ambiguous_mutation_is_frozen_and_not_repeated(self):
        for failure, key, expected in [
            ("create_stack", "stack_status", "uncertain"),
            ("create_bucket", "bucket_status", "uncertain"),
            ("delete_bucket", "bucket_status", "delete_uncertain"),
        ]:
            state = new_state(REQUEST.scope, REQUEST.principal, REQUEST.namespace)
            store, services = MemoryProof(state), None
            services = Services(store)
            services.fail = failure
            with self.assertRaises(RuntimeError):
                advance(store, state, services, cleanup=False)
            self.assertEqual(store.state[key], expected)
            previous = list(services.calls)
            services.fail = None
            advance(store, state, services, cleanup=False)
            advance(store, state, services, cleanup=True)
            self.assertEqual(services.calls, previous)

    def test_receipt_loss_reconciles_with_readback_and_never_repeats_put(self):
        self.services.fail = "persist"
        with self.assertRaises(RuntimeError):
            advance(self.store, self.state, self.services, cleanup=False)
        self.assertEqual(self.state["receipt_status"], "uncertain")
        self.services.fail = None
        advance(self.store, self.state, self.services, cleanup=False)
        self.assertNotIn("delete_bucket", self.services.calls)
        self.services.receipt = decode(self.state["record"].encode()).receipt
        advance(self.store, self.state, self.services, cleanup=False)
        self.assertEqual(self.state["receipt_status"], "survived")
        self.assertEqual(self.services.calls.count("persist"), 1)

    def test_failed_durable_write_blocks_dispatch(self):
        with patch.object(self.store, "write", side_effect=OSError("disk-full")):
            with self.assertRaises(OSError):
                advance(self.store, self.state, self.services, cleanup=False)
        self.assertEqual(self.services.calls, [])

    def test_cleanup_before_bucket_dispatch_never_creates_and_job_check_failure_is_uncertain(self):
        self.state["stack_status"], self.state["stack_id"] = "acknowledged", "synthetic-stack"
        self.services.fail = "delete_stack"
        with self.assertRaises(RuntimeError):
            advance(self.store, self.state, self.services, cleanup=True)
        self.assertEqual(self.state["stack_status"], "delete_uncertain")
        self.services.fail = None
        advance(self.store, self.state, self.services, cleanup=True)
        self.assertEqual(self.services.calls.count("delete_stack"), 1)
        self.assertNotIn("create_bucket", self.services.calls)

    def test_observe_is_read_only_and_empty_scan_does_not_complete_uncertain_cleanup(self):
        advance(self.store, self.state, self.services, cleanup=False)
        self.state["bucket_status"] = "delete_uncertain"
        self.state["receipt_status"] = "confirmed"
        previous = len(self.services.calls)
        observe(self.store, self.state, self.services)
        self.assertEqual(self.services.calls[previous:], ["audit", "audit"])
        self.assertEqual(self.state["bucket_status"], "delete_uncertain")
        self.assertEqual(self.state["audit_counts"], [0, 0])

    def test_audit_window_advances_for_delayed_resume_and_can_select_an_earlier_hour(self):
        from datetime import datetime, timedelta, timezone
        advance(self.store, self.state, self.services, cleanup=False)
        old_start = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(days=1)
        self.state["started"] = old_start.isoformat()
        observe(self.store, self.state, self.services)
        self.assertGreater(datetime.fromisoformat(self.state["audit_window"][0]), old_start)
        observe(self.store, self.state, self.services, processed_start=old_start)
        self.assertEqual(self.state["audit_window"], [old_start.isoformat(), (old_start + timedelta(hours=1)).isoformat()])


class ProofStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name).resolve() / "state"
        self.state = new_state(REQUEST.scope, REQUEST.principal, REQUEST.namespace)

    def test_exclusive_lock_owner_modes_atomic_roundtrip_and_reopen(self):
        with ProofStore(self.path) as store:
            self.assertIsNone(store.read())
            store.write(self.state)
            self.assertEqual(store.read(), self.state)
            self.assertEqual((self.path / "state.json").stat().st_mode & 0o777, 0o600)
            with self.assertRaises(ProofStateError):
                with ProofStore(self.path):
                    self.fail("concurrent lock acquired")
        with ProofStore(self.path) as store:
            self.assertEqual(store.read(), self.state)

    def test_symlinks_broad_permissions_and_corrupt_or_oversize_state_rejected(self):
        self.path.symlink_to(Path(self.temp.name), target_is_directory=True)
        with self.assertRaises(ProofStateError):
            with ProofStore(self.path):
                pass
        self.path.unlink()
        with ProofStore(self.path) as store:
            for body in [b"{}", b'{"schema":1,"schema":1}', b"x" * 65537]:
                target = self.path / "state.json"
                target.write_bytes(body)
                target.chmod(0o600)
                with self.assertRaises(ProofStateError):
                    store.read()
            target.unlink()
            target.symlink_to(Path(self.temp.name) / "secret")
            with self.assertRaises(ProofStateError):
                store.read()
        (self.path / "state.json").unlink()
        self.path.chmod(0o755)
        with self.assertRaises(ProofStateError):
            with ProofStore(self.path):
                pass

    def test_invalid_state_rejects_unknown_fields_and_impossible_ack(self):
        for change in [{"secret": "not-allowed"}, {"stack_status": "acknowledged"},
                       {"bucket_status": "deleted"}, {"schema": True}, {"record": "bad"}]:
            with self.assertRaises(ProofStateError):
                validate_state(dict(self.state, **change))

    def test_fsync_failure_leaves_no_permission_to_dispatch(self):
        with ProofStore(self.path) as store:
            with patch("tests.live.proof_state.os.fsync", side_effect=OSError("disk")):
                with self.assertRaises(ProofStateError):
                    store.write(self.state)

    def test_inherited_worker_retains_lock_after_parent_descriptor_closes(self):
        ready = self.path.parent / "ready"
        with ProofStore(self.path) as store:
            child = subprocess.Popen(
                [sys.executable, "-m", "tests.live.proof_watchdog_fixture", "--lock-fd", str(store.lock_fd),
                 "run", "--state", str(self.path), "--ready", str(ready)],
                pass_fds=(store.lock_fd,), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.addCleanup(lambda: child.poll() is None and child.kill())
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists(), "inherited worker did not start")
        try:
            with self.assertRaises(ProofStateError):
                with ProofStore(self.path):
                    self.fail("parent exit unlocked the live worker")
        finally:
            child.kill()
            child.wait(timeout=5)
        with ProofStore(self.path):
            pass

    def test_fsync_and_directory_sync_precede_write_acknowledgement(self):
        with ProofStore(self.path) as store:
            observed = []
            real_fsync, real_replace = os.fsync, os.replace

            def synced(fd):
                observed.append("directory" if fd == store.fd else "file")
                real_fsync(fd)

            def replaced(*args, **kwargs):
                observed.append("replace")
                real_replace(*args, **kwargs)

            with patch("tests.live.proof_state.os.fsync", side_effect=synced), patch(
                    "tests.live.proof_state.os.replace", side_effect=replaced):
                store.write(self.state)
            self.assertEqual(observed, ["file", "replace", "directory"])

    def test_state_directory_parent_is_synced_and_failure_prevents_entry(self):
        with patch("tests.live.proof_state.os.fsync", side_effect=OSError("disk")):
            with self.assertRaises(ProofStateError):
                with ProofStore(self.path):
                    self.fail("unsynced directory admitted")
        synced = []
        real_fsync = os.fsync
        with patch("tests.live.proof_state.os.fsync",
                   side_effect=lambda fd: (synced.append(os.fstat(fd).st_ino), real_fsync(fd))):
            with ProofStore(self.path):
                pass
        self.assertIn(self.path.parent.stat().st_ino, synced)


class ProofStackTests(unittest.TestCase):
    def setUp(self):
        patch("oci.resource_manager.resource_manager_client.Signer", return_value=SyntheticSigner()).start()
        self.addCleanup(patch.stopall)
        self.stack = ProofStack(CONFIG, REQUEST.scope)
        self.addCleanup(self.stack.close)
        self.send = patch("oci.base_client.OCIHTTPAdapter.send").start()
        self.state = new_state(REQUEST.scope, REQUEST.principal, REQUEST.namespace)
        self.state["stack_id"] = "synthetic-stack"

    def data(self):
        return {"id": "synthetic-stack", "compartmentId": REQUEST.scope.compartment_id,
                "displayName": self.state["stack_name"], "lifecycleState": "ACTIVE",
                "freeformTags": {"recovery_proof": self.state["epoch"]},
                "configSource": {"configSourceType": "ZIP_UPLOAD", "workingDirectory": ""},
                "variables": {}}

    def test_create_exact_resource_free_zip_and_no_job(self):
        self.send.side_effect = [response(self.data())]
        self.assertEqual(self.stack.create(self.state), "synthetic-stack")
        request = self.send.call_args.args[0]
        self.assertEqual(request.method, "POST")
        body = json.loads(request.body)
        self.assertEqual(body["configSource"]["configSourceType"], "ZIP_UPLOAD")
        self.assertEqual(request.headers["opc-retry-token"], self.state["stack_token"])
        import base64
        self.assertEqual(base64.b64decode(body["configSource"]["zipFileBase64Encoded"]), fixture_zip())
        self.assertNotIn("runApplyJob", body)

    def test_delete_requires_exact_stack_and_complete_no_job_result(self):
        self.send.side_effect = [response(self.data()), response([]), response(body=b"", status=204)]
        self.stack.delete(self.state)
        calls = [c.args[0] for c in self.send.call_args_list]
        self.assertEqual([c.method for c in calls], ["GET", "GET", "DELETE"])
        self.assertIn("stackId=synthetic-stack", calls[1].url)
        self.assertNotIn("lifecycleState", calls[1].url)
        self.assertEqual(calls[2].headers["if-match"], '"revision-1"')
        for reply in [response([{"id": "job"}]), response([], headers={"opc-next-page": "next"}),
                      response([], headers={"opc-next-page": ""}), response({})]:
            self.send.reset_mock()
            self.send.side_effect = [response(self.data()), reply]
            with self.assertRaises(StackError):
                self.stack.delete(self.state)
            self.assertEqual(self.send.call_count, 2)

    def test_metadata_mismatch_and_lost_delete_never_acknowledged(self):
        for changed in [{"id": "other"}, {"freeformTags": {}}, {"lifecycleState": "DELETING"},
                        {"configSource": {"configSourceType": "GIT_CONFIG_SOURCE"}}]:
            self.send.side_effect = [response(dict(self.data(), **changed))]
            with self.assertRaises(StackError):
                self.stack.check(self.state)
        self.send.side_effect = [response(self.data()), response([]), OSError("secret")]
        with self.assertRaises(StackError) as error:
            self.stack.delete(self.state)
        self.assertNotIn("secret", str(error.exception))

    def test_optional_root_directory_is_accepted_but_nested_or_wrong_type_is_not(self):
        for source in ({"configSourceType": "ZIP_UPLOAD"},
                       {"configSourceType": "ZIP_UPLOAD", "workingDirectory": ""},
                       {"configSourceType": "ZIP_UPLOAD", "workingDirectory": None}):
            data = self.data()
            data["configSource"] = source
            self.send.side_effect = [response(data), response(data)]
            self.assertEqual(self.stack.create(self.state), "synthetic-stack")
            self.stack.check(self.state)
        for root in ("nested", False, 0, [], {}):
            source = {"configSourceType": "ZIP_UPLOAD", "workingDirectory": root}
            self.send.side_effect = [response(dict(self.data(), configSource=source))]
            with self.assertRaises(StackError):
                self.stack.check(self.state)


class ProofSupervisorTests(unittest.TestCase):
    def test_cli_help_and_missing_optin_never_need_credentials(self):
        for args, expected in [(["--help"], 0), (["run"], 2)]:
            result = subprocess.run([sys.executable, "-m", "tests.live.recovery_proof", *args],
                                    capture_output=True, timeout=5)
            self.assertEqual(result.returncode, expected)
            self.assertNotIn(b"Traceback", result.stderr)

    def test_watchdog_kills_and_reaps_worker_before_releasing_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp).resolve() / "state"
            with ProofStore(path) as store:
                result = supervise(["run"], store, timeout=0.1, worker="tests.live.proof_watchdog_fixture")
                self.assertEqual(result, "worker_stopped_outcome_unknown")
                with self.assertRaises(ProofStateError):
                    with ProofStore(path):
                        pass
            with ProofStore(path):
                pass

    def test_interrupt_kills_reaps_and_restores_signal_handler(self):
        import signal
        process = unittest.mock.Mock()
        process.wait.side_effect = [KeyboardInterrupt(), 0]
        process.poll.return_value = None
        process.pid = 12345
        previous = signal.getsignal(signal.SIGTERM)
        with patch("tests.live.recovery_proof.subprocess.Popen", return_value=process), patch(
                "tests.live.recovery_proof.os.killpg") as kill:
            self.assertEqual(supervise(["run"], unittest.mock.Mock(lock_fd=7)), "worker_stopped_outcome_unknown")
        kill.assert_called_once_with(12345, signal.SIGKILL)
        self.assertEqual(process.wait.call_count, 2)
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)

    def test_repeated_cancellation_is_deferred_until_worker_is_reaped(self):
        import signal
        events = []
        process = unittest.mock.Mock(pid=12345)
        process.poll.return_value = None

        def wait(*args, **kwargs):
            if not events:
                events.append("interrupted")
                raise KeyboardInterrupt()
            events.append("reaped")
            return 0

        def kill(*args):
            mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())
            self.assertTrue({signal.SIGINT, signal.SIGTERM} <= mask)
            os.kill(os.getpid(), signal.SIGTERM)
            os.kill(os.getpid(), signal.SIGINT)
            events.append("killed")

        process.wait.side_effect = wait
        previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            for sig in previous:
                signal.signal(sig, lambda *unused: events.append("late_cancel"))
            with patch("tests.live.recovery_proof.subprocess.Popen", return_value=process), patch(
                    "tests.live.recovery_proof.os.killpg", side_effect=kill):
                self.assertEqual(supervise(["run"], unittest.mock.Mock(lock_fd=7)), "worker_stopped_outcome_unknown")
            self.assertEqual(events[:3], ["interrupted", "killed", "reaped"])
            self.assertEqual(events[3:], ["late_cancel", "late_cancel"])
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)


class ProofWorkerContractTests(unittest.TestCase):
    def test_full_worker_flow_uses_real_adapters_and_no_apply_then_resumes_cleanup(self):
        from tests.live import proof_worker
        from troubleshooter_deployment.recovery_bucket import metadata
        from tests.live.proof_state import bucket_request
        from tests.contract.test_oci_recovery_bucket import bucket
        from urllib.parse import urlsplit

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp).resolve() / "state"
            variables, methods = {}, []

            def send(request, **kwargs):
                nonlocal variables
                methods.append((request.method, urlsplit(request.url).path))
                if urlsplit(request.url).path == "/n":
                    return response("synthetic-ns")
                state = json.loads((path / "state.json").read_bytes())
                stack = {
                    "id": "synthetic-stack", "compartmentId": state["scope"]["compartment_id"],
                    "displayName": state["stack_name"], "lifecycleState": "ACTIVE",
                    "freeformTags": {"recovery_proof": state["epoch"]},
                    "configSource": {"configSourceType": "ZIP_UPLOAD", "workingDirectory": ""},
                    "variables": variables,
                }
                if request.method == "DELETE":
                    return response(body=b"", status=204)
                if urlsplit(request.url).path.endswith("/jobs"):
                    return response([])
                if "/stacks" in request.url:
                    if request.method == "PUT":
                        variables = json.loads(request.body)["variables"]
                        stack["variables"] = variables
                    return response(stack)
                intent = bucket_request(state)
                return response(bucket(name=intent.name, metadata=metadata(intent),
                                       compartmentId=intent.scope.compartment_id))

            with patch("oci.object_storage.object_storage_client.Signer", return_value=SyntheticSigner()), patch(
                    "oci.resource_manager.resource_manager_client.Signer", return_value=SyntheticSigner()), patch(
                    "oci.audit.audit_client.Signer", return_value=SyntheticSigner()), patch(
                    "oci.base_client.OCIHTTPAdapter.send", side_effect=send), patch(
                    "tests.live.proof_worker.credentials", return_value=CONFIG) as creds, patch.object(
                    proof_worker, "STATE_PATH", path):
                with ProofStore(path) as store:
                    for action in ("run", "run", "cleanup", "cleanup"):
                        argv = ["worker", action, "--allow-live", "--profile", "synthetic",
                                "--compartment", REQUEST.scope.compartment_id, "--lock-fd", str(os.dup(store.lock_fd))]
                        with patch.object(sys, "argv", argv):
                            self.assertEqual(proof_worker.main(), 0)
                    self.assertEqual(store.read()["stack_status"], "deleted")
                    self.assertEqual(store.read()["receipt_status"], "survived")
                self.assertEqual(creds.call_count, 4)
            self.assertEqual(sum(method == "POST" for method, path in methods), 2)
            self.assertEqual(sum(method == "DELETE" for method, path in methods), 2)
            self.assertEqual(sum(method == "PUT" for method, path in methods), 1)
            self.assertNotIn(("POST", "/20180917/jobs"), methods)
