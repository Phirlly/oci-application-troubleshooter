import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.contract.oci_responses import CONFIG, OCI_IDENTITY
from tests.live.proof_state import ProofStore
from tests.live import store_bootstrap_proof as launcher
from tests.live.store_bootstrap_state import new_state, validate_state
from dataclasses import replace


class StoreBootstrapLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name).resolve() / "fixture"
        self.scope = replace(OCI_IDENTITY.scope, repository_id=1, environment="store-proof-synthetic")
        self.state = new_state(self.scope, CONFIG["user"], "synthetic-ns")
        self.addCleanup(patch.stopall)
        patch.object(launcher, "STATE_PATH", self.path).start()

    def invoke(self, *args):
        with contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()):
            result = launcher.main(list(args))
        return result, output.getvalue()

    def test_optin_and_profile_required_before_private_access(self):
        with patch.object(launcher, "ProofStore") as store:
            for args in [("run",), ("run", "--allow-live"), ("run", "--profile", "synthetic")]:
                with self.assertRaises(SystemExit):
                    self.invoke(*args)
            store.assert_not_called()

    def test_help_does_not_import_oci_or_access_private_state(self):
        import builtins
        original = builtins.__import__

        def guarded(name, *args, **kwargs):
            if name == "oci" or name.startswith("oci."):
                raise AssertionError("OCI import before live opt-in")
            return original(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=guarded), patch.object(launcher, "ProofStore") as store:
            with self.assertRaises(SystemExit) as result:
                self.invoke("--help")
            self.assertEqual(result.exception.code, 0)
            store.assert_not_called()

    def test_parent_requires_successful_worker_and_expected_positive_state(self):
        with ProofStore(self.path, validator=validate_state) as store:
            store.write(self.state)
        with patch.object(launcher, "supervise", return_value="worker_finished_check_state") as supervise:
            code, output = self.invoke("run", "--allow-live", "--profile", "synthetic")
        self.assertEqual(code, 1)
        self.assertIn("phase=new", output)
        self.assertNotIn(CONFIG["user"], output)
        self.assertEqual(supervise.call_args.kwargs["worker"], "tests.live.store_bootstrap_proof")

    def test_child_requires_matching_inherited_lock_before_credentials(self):
        with patch("tests.live.proof_worker.credentials") as credentials:
            code, _ = self.invoke("run", "--allow-live", "--profile", "synthetic", "--lock-fd", "999999")
        self.assertEqual(code, 1)
        credentials.assert_not_called()

    def test_existing_scope_and_principal_mismatch_precede_service_construction(self):
        with ProofStore(self.path, validator=validate_state) as store:
            store.write(self.state)
            for changed in ({"user": "other"}, {"region": "other"}, {"tenancy": "other"}):
                with patch("tests.live.proof_worker.credentials", return_value={**CONFIG, **changed}), \
                        patch("tests.live.store_bootstrap_services.BootstrapProof") as proof:
                    args = launcher.parser().parse_args(["run", "--allow-live", "--profile", "synthetic"])
                    self.assertEqual(launcher.worker(args, store), 2)
                    proof.assert_not_called()
            self.assertEqual(store.read(), self.state)

    def test_missing_state_or_compartment_and_expired_allocation_stop_before_services(self):
        with ProofStore(self.path, validator=validate_state) as store:
            with patch("tests.live.proof_worker.credentials") as credentials:
                for action in ("run", "observe", "cleanup"):
                    args = launcher.parser().parse_args([action, "--allow-live", "--profile", "synthetic"])
                    self.assertEqual(launcher.worker(args, store), 2)
                credentials.assert_not_called()
            expired = {**self.state, "created_at": "2000-01-01T00:00:00Z"}
            store.write(expired)
            with patch("tests.live.proof_worker.credentials", return_value=CONFIG), \
                    patch("tests.live.store_bootstrap_services.BootstrapProof") as proof:
                self.assertEqual(launcher.worker(args, store), 2)
                proof.assert_not_called()
