from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from tests.contract.oci_responses import CONFIG
from tests.contract.test_delete_observation_proof import saved_deletion
from tests.live import proof_worker, recovery_proof
from tests.live.proof_state import ProofStore


class ProofExperimentTests(unittest.TestCase):
    def test_invalid_selector_is_rejected_before_private_access_or_credentials(self):
        for module in (recovery_proof, proof_worker):
            for value in ("../other", "new", "/tmp/proof", "frozen-delete-2"):
                argv = ["proof", "observe-delete", "--allow-live", "--profile", "synthetic",
                        "--experiment", value]
                if module is proof_worker:
                    argv += ["--lock-fd", "123"]
                with patch.object(sys, "argv", argv), patch.object(module, "ProofStore") as store, patch(
                        "tests.live.proof_worker.credentials") as creds, redirect_stderr(StringIO()):
                    with self.assertRaises(SystemExit) as caught:
                        module.main()
                    self.assertEqual(caught.exception.code, 2)
                    store.assert_not_called()
                    creds.assert_not_called()

    def test_parent_uses_fixed_selection_and_suppresses_old_counts_for_every_observation_result(self):
        state, _, _ = saved_deletion()
        for code, status in ((0, "delete_request_match_observed"), (3, "delete_request_not_observed"),
                             (4, "delete_request_match_ambiguous"), (1, "worker_failed_check_state")):
            output = StringIO()
            argv = ["proof", "--experiment", "frozen-delete", "observe-delete", "--allow-live", "--profile", "synthetic"]
            with patch.object(sys, "argv", argv), patch.object(recovery_proof, "ensure_directory"), patch.object(
                    recovery_proof, "ProofStore") as store, patch.object(
                    recovery_proof, "supervise", return_value=status), redirect_stdout(output):
                store.return_value.__enter__.return_value.read.return_value = state
                self.assertEqual(recovery_proof.main(), 0 if code == 0 else 1)
                store.assert_called_once_with(recovery_proof.STATE_PATH.with_name("recovery-proof-frozen-delete"))
            self.assertNotIn("audit_candidates=", output.getvalue())
            self.assertEqual("audit_coverage=unknown" in output.getvalue(), code != 1)
            self.assertIn(status, output.getvalue())

    def test_supervisor_uses_parsed_action_and_only_fixed_statuses(self):
        from tests.live.recovery_proof import audit_diagnostic_exits, audit_scan_exits
        cases = [(0, "delete_request_match_observed"), (3, "delete_request_not_observed"),
                 (4, "delete_request_match_ambiguous"), (1, "worker_failed_check_state"),
                 (255, "worker_failed_check_state")]
        cases += [(code, "audit_transport_" + reason) for reason, code in audit_diagnostic_exits().items()]
        cases += [(code, "audit_scan_" + reason) for reason, code in audit_scan_exits().items()]
        for code, expected in cases:
            process = Mock()
            process.wait.return_value = process.poll.return_value = code
            with patch.object(recovery_proof.subprocess, "Popen", return_value=process) as launch:
                self.assertEqual(recovery_proof.supervise(
                    ["--experiment", "frozen-delete", "observe-delete"], Mock(lock_fd=7)), expected)
            self.assertEqual(launch.call_args.kwargs["stdout"], subprocess.DEVNULL)
            self.assertEqual(launch.call_args.kwargs["stderr"], subprocess.DEVNULL)

    def test_worker_resumes_only_selected_state_and_rejects_other_slots_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            original_path = Path(temp).resolve() / "recovery-proof"
            selected_path = original_path.with_name("recovery-proof-frozen-delete")
            state, _, _ = saved_deletion()
            with ProofStore(original_path) as original, ProofStore(selected_path) as selected:
                original.write(state)
                selected.write(state)
                original_bytes = (original_path / "state.json").read_bytes()
                selected_bytes = (selected_path / "state.json").read_bytes()
                for store, expected in ((original, 1), (selected, 0), (selected, 0)):
                    argv = ["worker", "--experiment", "frozen-delete", "observe-delete", "--allow-live",
                            "--profile", "synthetic", "--lock-fd", str(os.dup(store.lock_fd))]
                    with patch.object(sys, "argv", argv), patch.object(proof_worker, "STATE_PATH", original_path), patch.object(
                            proof_worker, "credentials", return_value=CONFIG) as creds, patch.object(
                            proof_worker, "ProofServices"), patch.object(
                            proof_worker, "observe_deletion", return_value=1) as observe:
                        self.assertEqual(proof_worker.main(), expected)
                    if store is original:
                        creds.assert_not_called()
                        observe.assert_not_called()
                    else:
                        self.assertEqual(observe.call_args.args[0], state)
                    self.assertEqual((original_path / "state.json").read_bytes(), original_bytes)
                    self.assertEqual((selected_path / "state.json").read_bytes(), selected_bytes)

    def test_new_help_and_optin_checks_do_not_require_oci_imports(self):
        for args, expected in ((["--help"], 0), (["observe-delete"], 2)):
            code = ("import runpy, sys; sys.modules['oci'] = None; "
                    f"sys.argv = {['proof', *args]!r}; "
                    "runpy.run_module('tests.live.recovery_proof', run_name='__main__')")
            result = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=5)
            self.assertEqual(result.returncode, expected, result.stderr.decode())
            if expected == 0:
                self.assertIn(b"observe-delete", result.stdout)
                self.assertIn(b"frozen-delete", result.stdout)
