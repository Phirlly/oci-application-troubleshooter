from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
from io import StringIO
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from troubleshooter_deployment.bucket_deletion import read_deletion
from troubleshooter_deployment.recovery_bucket import metadata
from tests.contract.oci_responses import CONFIG, SyntheticSigner, response
from tests.contract.test_audit_delete_binding import deletion_event
from tests.contract.test_delete_observation_proof import saved_deletion
from tests.contract.test_oci_audit import START, END, page
from tests.contract.test_oci_recovery_bucket import REQUEST, bucket
from tests.live import proof_flow, proof_worker, recovery_proof
from tests.live.proof_state import ProofStore, bucket_request


class InterruptedDeleteWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve() / "proof"
        self.path = self.base.with_name("recovery-proof-interrupted-delete")
        self.variables, self.requests, self.matches, self.deny_survival = {}, [], 1, False
        for name in ("object_storage.object_storage_client", "resource_manager.resource_manager_client",
                     "loggingsearch.log_search_client"):
            patch("oci." + name + ".Signer", return_value=SyntheticSigner()).start()
        patch.object(proof_worker, "credentials", return_value=CONFIG).start()
        patch.object(proof_worker, "STATE_PATH", self.base).start()
        patch("oci.base_client.OCIHTTPAdapter.send", side_effect=self.send).start()
        self.clock = patch.object(proof_flow, "datetime", wraps=proof_flow.datetime).start()
        self.clock.now.return_value = START
        self.addCleanup(patch.stopall)

    def send(self, request, **kwargs):
        path = urlsplit(request.url).path
        self.requests.append((request.method, path))
        if path == "/n":
            return response(REQUEST.namespace)
        state = json.loads((self.path / "state.json").read_bytes())
        if path.endswith("/search"):
            body = json.loads(request.body)
            self.assertEqual(body["timeStart"], START.strftime("%Y-%m-%dT%H:%M:%SZ"))
            self.assertEqual(body["timeEnd"], (START + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))
            return page([deletion_event(read_deletion(self.variables))] * self.matches)
        if request.method == "DELETE":
            if "/b/" in path:
                self.assertEqual(state["bucket_status"], "delete_uncertain")
                self.assertIn("delete_window", state)
                intent = read_deletion(self.variables)
                self.assertEqual(request.headers["if-match"], intent.if_match)
                self.assertEqual(request.headers["opc-client-request-id"], intent.correlation)
            return response(body=b"", status=204)
        if path.endswith("/jobs"):
            return response([])
        if "/stacks" in path:
            if self.deny_survival and state["bucket_status"] == "deleted":
                return response({"code": "Denied"}, status=403)
            if request.method == "PUT":
                self.variables = json.loads(request.body)["variables"]
            return response({"id": "synthetic-stack", "compartmentId": state["scope"]["compartment_id"],
                             "displayName": state["stack_name"], "lifecycleState": "ACTIVE",
                             "freeformTags": {"recovery_proof": state["epoch"]},
                             "configSource": {"configSourceType": "ZIP_UPLOAD", "workingDirectory": ""},
                             "variables": self.variables})
        intent = bucket_request(state)
        return response(bucket(name=intent.name, metadata=metadata(intent),
                               compartmentId=intent.scope.compartment_id))

    def worker(self, action):
        with ProofStore(self.path) as store:
            argv = ["worker", action, "--experiment", "interrupted-delete", "--allow-live",
                    "--profile", "synthetic", "--compartment", REQUEST.scope.compartment_id,
                    "--lock-fd", str(os.dup(store.lock_fd))]
            with patch.object(sys, "argv", argv):
                return proof_worker.main()

    def state(self):
        with ProofStore(self.path) as store:
            return store.read()

    def test_reply_loss_reload_and_recovery_never_repeat_delete_and_preserve_other_slot(self):
        old, _, _ = saved_deletion()
        with ProofStore(self.base) as store:
            store.write(old)
        historical = (self.base / "state.json").read_bytes()
        self.assertEqual(self.worker("run-interrupted"), 1)
        self.assertEqual(self.state()["bucket_status"], "delete_uncertain")
        self.assertEqual(self.state()["receipt_status"], "confirmed")
        original = (self.path / "state.json").read_bytes()
        before = len(self.requests)
        self.assertEqual(self.worker("cleanup"), 0)
        self.assertEqual(self.worker("run"), 0)
        self.assertEqual(len(self.requests), before)
        self.clock.now.return_value = END + timedelta(days=1)
        for matches in (0, 2):
            self.matches = matches
            self.assertEqual(self.worker("recover-delete"), 3)
            self.assertEqual((self.path / "state.json").read_bytes(), original)
        self.matches = 1
        write = ProofStore.write
        def fail_completion(store, state):
            if state["bucket_status"] == "deleted":
                raise OSError("disk")
            return write(store, state)
        with patch.object(ProofStore, "write", fail_completion):
            self.assertEqual(self.worker("recover-delete"), 1)
        self.assertEqual((self.path / "state.json").read_bytes(), original)
        self.deny_survival = True
        self.assertEqual(self.worker("recover-delete"), 1)
        self.assertEqual(self.state()["bucket_status"], "deleted")
        self.assertEqual(self.state()["receipt_status"], "confirmed")
        self.deny_survival = False
        before = len(self.requests)
        self.assertEqual(self.worker("recover-delete"), 0)
        self.assertEqual(self.requests[before:], [("GET", "/20180917/stacks/synthetic-stack")])
        self.assertEqual(self.state()["receipt_status"], "survived")
        self.assertEqual(self.worker("cleanup"), 0)
        self.assertEqual(self.state()["stack_status"], "deleted")
        self.assertEqual(sum(method == "DELETE" and "/b/" in path for method, path in self.requests), 1)
        self.assertEqual(sum(method == "DELETE" for method, path in self.requests), 2)
        self.assertEqual((self.base / "state.json").read_bytes(), historical)

    def test_missing_or_conflicting_independent_intent_prevents_audit_and_writes(self):
        self.assertEqual(self.worker("run-interrupted"), 1)
        self.clock.now.return_value = END + timedelta(days=1)
        from troubleshooter_deployment.bucket_deletion import KEY
        variables = self.variables.copy()
        for value in (None, "invalid"):
            self.variables = variables.copy()
            if value is None:
                del self.variables[KEY]
            else:
                self.variables[KEY] = value
            original = (self.path / "state.json").read_bytes()
            before = len(self.requests)
            self.assertEqual(self.worker("recover-delete"), 1)
            self.assertEqual(self.requests[before:], [("GET", "/20180917/stacks/synthetic-stack")])
            self.assertEqual((self.path / "state.json").read_bytes(), original)


class InterruptedDeleteCliTests(unittest.TestCase):
    def test_invalid_fault_slot_and_recovery_search_flags_fail_before_private_access(self):
        for module in (proof_worker, recovery_proof):
            for args in (["run-interrupted", "--experiment", "original"],
                         ["recover-delete", "--search-minutes", "30"]):
                argv = ["proof", *args, "--allow-live", "--profile", "synthetic"]
                if module is proof_worker:
                    argv += ["--lock-fd", "123"]
                with patch.object(sys, "argv", argv), patch.object(module, "ProofStore") as store, redirect_stderr(StringIO()):
                    if module is proof_worker:
                        self.assertEqual(module.main(), 2)
                    else:
                        with self.assertRaises(SystemExit) as caught:
                            module.main()
                        self.assertEqual(caught.exception.code, 2)
                    store.assert_not_called()

    def test_supervisor_and_parent_publish_only_fixed_recovery_status(self):
        for code, status in ((0, "delete_recovery_confirmed"), (3, "delete_recovery_pending"),
                             (1, "worker_failed_check_state")):
            process = Mock()
            process.wait.return_value = process.poll.return_value = code
            with patch.object(recovery_proof.subprocess, "Popen", return_value=process):
                self.assertEqual(recovery_proof.supervise(["recover-delete"], Mock(lock_fd=7)), status)
            state, _, _ = saved_deletion()
            argv = ["proof", "recover-delete", "--experiment", "interrupted-delete", "--allow-live", "--profile", "synthetic"]
            output = StringIO()
            with patch.object(sys, "argv", argv), patch.object(recovery_proof, "ensure_directory"), patch.object(
                    recovery_proof, "ProofStore") as store, patch.object(
                    recovery_proof, "supervise", return_value=status), redirect_stdout(output):
                store.return_value.__enter__.return_value.read.return_value = state
                self.assertEqual(recovery_proof.main(), 0 if code == 0 else 1)
            self.assertIn(status, output.getvalue())
            self.assertNotIn("audit_candidates=", output.getvalue())
