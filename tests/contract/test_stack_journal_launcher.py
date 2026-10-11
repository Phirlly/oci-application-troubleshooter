import contextlib
from dataclasses import replace
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.contract.oci_responses import CONFIG, OCI_IDENTITY
from tests.live.proof_state import ProofStore
from tests.live import stack_journal_proof as launcher
from tests.live.stack_journal_state import new_state, validate_state


class StackJournalLauncherTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name).resolve() / "fixture"
        self.state = new_state(replace(OCI_IDENTITY.scope, repository_id=1,
            environment="stack-journal-proof-synthetic"), CONFIG["user"])
        self.addCleanup(patch.stopall)
        patch.object(launcher, "STATE_PATH", self.path).start()

    def invoke(self, args):
        with contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()):
            code = launcher.main(args)
        return code, output.getvalue()

    def test_optin_help_and_bad_inherited_lock_precede_private_access(self):
        with patch.object(launcher, "ProofStore") as store:
            for args in (["run"], ["run", "--allow-live"], ["run", "--profile", "synthetic"], ["--help"]):
                with self.assertRaises(SystemExit):
                    self.invoke(args)
            store.assert_not_called()
        with patch("tests.live.proof_worker.credentials") as credentials:
            code, _ = self.invoke(["run", "--allow-live", "--profile", "synthetic", "--lock-fd", "999999"])
            self.assertEqual(code, 1)
            credentials.assert_not_called()

    def test_parent_checks_phase_and_never_prints_scope(self):
        with ProofStore(self.path, validator=validate_state) as store:
            store.write(self.state)
        with patch.object(launcher, "supervise", return_value="worker_finished_check_state") as supervise:
            code, output = self.invoke(["run", "--allow-live", "--profile", "synthetic"])
        self.assertEqual(code, 1)
        self.assertIn("phase=new", output)
        self.assertNotIn(CONFIG["user"], output)
        self.assertEqual(supervise.call_args.kwargs["worker"], "tests.live.stack_journal_proof")

    def test_missing_state_target_and_mismatched_existing_scope_stop_before_services(self):
        with ProofStore(self.path, validator=validate_state) as store:
            with patch("tests.live.proof_worker.credentials") as credentials:
                args = launcher.parser().parse_args(["run", "--allow-live", "--profile", "synthetic"])
                self.assertEqual(launcher.worker(args, store), 2)
                credentials.assert_not_called()
            store.write(self.state)
            for changed in ({"user": "other"}, {"region": "other"}, {"tenancy": "other"}):
                with patch("tests.live.proof_worker.credentials", return_value={**CONFIG, **changed}), \
                        patch("tests.live.stack_journal_services.StackJournalProof") as proof:
                    self.assertEqual(launcher.worker(args, store), 2)
                    proof.assert_not_called()
            store.write({**self.state, "created_at": "2000-01-01T00:00:00Z"})
            with patch("tests.live.proof_worker.credentials", return_value=CONFIG), \
                    patch("tests.live.stack_journal_services.StackJournalProof") as proof:
                self.assertEqual(launcher.worker(args, store), 2)
                proof.assert_not_called()
