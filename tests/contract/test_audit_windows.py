"""Developer observations use explicit bounded search-time windows."""
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from io import StringIO
import sys
import unittest
from unittest.mock import Mock, patch

from tests.contract.test_oci_recovery_bucket import REQUEST
from tests.live import proof_worker, recovery_proof
from tests.live.proof_flow import observe
from tests.live.proof_state import new_state, validate_state

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


class AuditWindowTests(unittest.TestCase):
    def setUp(self):
        self.state = new_state(REQUEST.scope, REQUEST.principal, REQUEST.namespace)
        self.state.update(started=START.isoformat(), stack_status="acknowledged", stack_id="synthetic-stack",
                          bucket_status="acknowledged", bucket_id="synthetic-bucket")
        self.store, self.services = Mock(), Mock()
        self.services.audit.return_value = Mock(matches=())

    def test_explicit_minimum_maximum_and_default_intervals_reach_both_queries(self):
        for options, minutes in [({}, 60), ({"search_minutes": 1}, 1), ({"search_minutes": 60}, 60)]:
            self.services.reset_mock()
            observe(self.store, self.state, self.services, search_start=START, **options)
            end = START + timedelta(minutes=minutes)
            self.assertEqual(self.services.audit.call_count, 2)
            for call in self.services.audit.call_args_list:
                self.assertEqual(call.args[-2:], (START, end))
            self.assertEqual(self.state["audit_window"], [START.isoformat(), end.isoformat()])
            validate_state(self.state)

    def test_rolling_window_uses_duration_and_does_not_precede_proof_start(self):
        now = START + timedelta(hours=2)
        with patch("tests.live.proof_flow.datetime") as clock:
            clock.now.return_value = now
            clock.fromisoformat.side_effect = datetime.fromisoformat
            for minutes in (1, 60):
                observe(self.store, self.state, self.services, search_minutes=minutes)
                end = now + timedelta(minutes=1)
                self.assertEqual(self.state["audit_window"],
                                 [(end - timedelta(minutes=minutes)).isoformat(), end.isoformat()])
            self.state["started"] = now.isoformat()
            observe(self.store, self.state, self.services, search_minutes=60)
            self.assertEqual(self.state["audit_window"][0], now.isoformat())

    def test_invalid_durations_fail_without_calls_or_state_updates_even_before_bucket_exists(self):
        for bucket in ("synthetic-bucket", None):
            self.state["bucket_id"] = bucket
            self.state["bucket_status"] = "acknowledged" if bucket else "new"
            for duration in (0, 61, -1, True, False, 1.0, "1", None):
                with self.assertRaises(ValueError):
                    observe(self.store, self.state, self.services, search_minutes=duration)
        self.services.audit.assert_not_called()
        self.store.write.assert_not_called()

    def test_cli_rejects_invalid_or_non_observe_duration_before_private_access(self):
        cases = [(action, minutes) for action in ("run", "cleanup") for minutes in ("1", "60")]
        cases += [("observe", minutes) for minutes in ("0", "61", "1.5", "true")]
        for action, minutes in cases:
            argv = ["proof", action, "--allow-live", "--profile", "synthetic", "--search-minutes", minutes]
            with patch.object(sys, "argv", argv), patch.object(recovery_proof, "ProofStore") as store, redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit) as error:
                    recovery_proof.main()
                self.assertEqual(error.exception.code, 2)
                store.assert_not_called()
            with patch.object(sys, "argv", argv + ["--lock-fd", "123"]), patch.object(
                    proof_worker, "ProofStore") as store, patch.object(proof_worker, "credentials") as creds, redirect_stderr(StringIO()):
                try:
                    result = proof_worker.main()
                except SystemExit as error:
                    result = error.code
                self.assertEqual(result, 2)
                store.assert_not_called()
                creds.assert_not_called()

    def test_cli_rejects_legacy_time_labels_before_private_access(self):
        for flag, value in (("--processed-start", START.isoformat()), ("--processed-minutes", "1")):
            argv = ["proof", "observe", "--allow-live", "--profile", "synthetic", flag, value]
            with patch.object(sys, "argv", argv), patch.object(recovery_proof, "ProofStore") as store, redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit) as error:
                    recovery_proof.main()
                self.assertEqual(error.exception.code, 2)
                store.assert_not_called()


if __name__ == "__main__":
    unittest.main()
