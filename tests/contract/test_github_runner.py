from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from troubleshooter_deployment import github_runner, github_bootstrap_worker
from troubleshooter_deployment.runner_bootstrap import BootstrapPending
from troubleshooter_deployment.runner_stack_journal import StackJournalReady
from tests.contract.test_actions_context import environment
from tests.deployment.fixtures import ATTEMPT


class BootstrapWorkerTests(unittest.TestCase):
    def invoke(self, values=None, result=None, error=None):
        output = StringIO()
        with patch.dict(os.environ, environment() if values is None else values, clear=True), patch(
                "troubleshooter_deployment.github_bootstrap_worker.checked_out_sha", return_value=ATTEMPT.source_sha), patch(
                "troubleshooter_deployment.runner_stack_journal.bootstrap_stack_journal",
                return_value=result, side_effect=error) as bootstrap, redirect_stdout(output), redirect_stderr(output):
            code = github_bootstrap_worker.main()
        self.assertEqual(output.getvalue(), "")
        return code, bootstrap

    def test_exact_once_success_and_closed_failure_results(self):
        for result, expected in ((StackJournalReady(None, None), 0), (BootstrapPending("private-secret"), 12),
                                 (None, 13), ("private-result", 13)):
            code, bootstrap = self.invoke(result=result)
            self.assertEqual(code, expected)
            bootstrap.assert_called_once()
        self.assertEqual(self.invoke(error=RuntimeError("private-error"))[0], 13)

    def test_bad_context_or_config_never_invokes_services(self):
        for values, expected in (({}, 10), ({**environment(), "OCI_PRIVATE_KEY": ""}, 11)):
            code, bootstrap = self.invoke(values)
            self.assertEqual(code, expected)
            bootstrap.assert_not_called()


class GitHubSupervisorTests(unittest.TestCase):
    def test_fixed_worker_suppresses_streams_and_uses_closed_statuses(self):
        for exit_code, expected in ((0, 0), (10, 10), (11, 11), (12, 12), (13, 13), (71, 13), (-9, 13)):
            process = Mock(pid=12345)
            process.wait.return_value = exit_code
            process.poll.return_value = exit_code
            with patch.object(github_runner.subprocess, "Popen", return_value=process) as start:
                self.assertEqual(github_runner.supervise(), expected)
            args = start.call_args
            self.assertEqual(args.args[0], [sys.executable, "-B", "-m", "troubleshooter_deployment.github_bootstrap_worker"])
            self.assertTrue(args.kwargs["start_new_session"])
            for stream in ("stdin", "stdout", "stderr"):
                self.assertEqual(args.kwargs[stream], subprocess.DEVNULL)
            process.wait.assert_called_once_with(timeout=300)

    def test_timeout_cancellation_and_repeated_signals_kill_and_reap(self):
        for failure in (subprocess.TimeoutExpired("worker", 300), KeyboardInterrupt()):
            process = Mock(pid=12345)
            process.wait.side_effect = [failure, 0]
            process.poll.return_value = None
            previous = signal.getsignal(signal.SIGTERM)
            def kill(pid, sig):
                self.assertEqual((pid, sig), (12345, signal.SIGKILL))
                self.assertTrue({signal.SIGINT, signal.SIGTERM} <= signal.pthread_sigmask(signal.SIG_BLOCK, set()))
            with patch.object(github_runner.subprocess, "Popen", return_value=process), patch.object(
                    github_runner.os, "killpg", side_effect=kill) as killed:
                self.assertEqual(github_runner.supervise(), 14)
            killed.assert_called_once()
            self.assertEqual(process.wait.call_count, 2)
            self.assertEqual(signal.getsignal(signal.SIGTERM), previous)

    def test_startup_cancellation_is_deferred_until_child_is_registered(self):
        process = Mock(pid=12345)
        process.poll.return_value = None
        process.wait.return_value = 0
        original_int = signal.getsignal(signal.SIGINT)
        def start(*args, **kwargs):
            self.assertTrue({signal.SIGINT, signal.SIGTERM} <= signal.pthread_sigmask(signal.SIG_BLOCK, set()))
            os.kill(os.getpid(), signal.SIGTERM)
            return process
        with patch.object(github_runner.subprocess, "Popen", side_effect=start), patch.object(
                github_runner.os, "killpg") as killed:
            self.assertEqual(github_runner.supervise(), 14)
        killed.assert_called_once_with(12345, signal.SIGKILL)
        self.assertEqual(signal.getsignal(signal.SIGINT), original_int)

    def test_repeated_cancellation_during_teardown_is_delivered_after_reaping(self):
        process = Mock(pid=12345)
        events = []
        process.poll.return_value = None
        def wait(*args, **kwargs):
            if not events:
                events.append("stopped")
                raise KeyboardInterrupt()
            events.append("reaped")
            return 0
        def kill(*args):
            os.kill(os.getpid(), signal.SIGTERM)
            os.kill(os.getpid(), signal.SIGINT)
            events.append("killed")
        process.wait.side_effect = wait
        with patch.object(github_runner.subprocess, "Popen", return_value=process), patch.object(
                github_runner.os, "killpg", side_effect=kill):
            self.assertEqual(github_runner.supervise(), 14)
        self.assertEqual(events, ["stopped", "killed", "reaped"])

    def test_startup_and_cleanup_failures_are_sanitized_and_never_success(self):
        with patch.object(github_runner.subprocess, "Popen", side_effect=OSError("private-secret")):
            self.assertEqual(github_runner.supervise(), 13)
        for failure in (ProcessLookupError(), OSError("private-secret")):
            process = Mock(pid=12345)
            process.wait.side_effect = [KeyboardInterrupt(), 0]
            process.poll.return_value = None
            with patch.object(github_runner.subprocess, "Popen", return_value=process), patch.object(
                    github_runner.os, "killpg", side_effect=failure):
                self.assertEqual(github_runner.supervise(), 14)
            self.assertEqual(process.wait.call_count, 2)
        process.wait.side_effect = [KeyboardInterrupt(), subprocess.TimeoutExpired("worker", 5)]
        with patch.object(github_runner.subprocess, "Popen", return_value=process), patch.object(github_runner.os, "killpg"):
            self.assertEqual(github_runner.supervise(), 14)
        with patch.object(github_runner, "SUPPORTED", False):
            self.assertEqual(github_runner.supervise(), 13)

    def test_actual_process_is_stopped_and_reaped_with_output_discarded(self):
        actual_popen = subprocess.Popen
        processes = []
        def start(*args, **kwargs):
            process = actual_popen([sys.executable, "-B", "-m", "tests.contract.runner_watchdog_fixture"], **kwargs)
            processes.append(process)
            return process
        with patch.object(github_runner.subprocess, "Popen", side_effect=start):
            self.assertEqual(github_runner.supervise(timeout=0.2), 14)
        self.assertIsNotNone(processes[0].poll())

    def test_main_checks_context_first_and_publishes_only_fixed_status(self):
        with tempfile.TemporaryDirectory() as temp:
            summary = Path(temp) / "summary"
            for code in (0, 10, 11, 12, 13, 14):
                output = StringIO()
                with patch.dict(os.environ, {**environment(), "GITHUB_STEP_SUMMARY": str(summary)}, clear=True), patch.object(
                        github_runner, "checked_out_sha", return_value=ATTEMPT.source_sha), patch.object(
                        github_runner, "supervise", return_value=code), redirect_stdout(output):
                    self.assertEqual(github_runner.main([]), 0 if code == 0 else 1)
                self.assertEqual(output.getvalue().strip(), github_runner.MESSAGES[code])
            self.assertNotIn("synthetic", summary.read_text())
            self.assertNotIn("Demo ready", summary.read_text())
            with patch.dict(os.environ, {}, clear=True), patch.object(github_runner, "supervise") as worker, redirect_stdout(StringIO()):
                self.assertEqual(github_runner.main([]), 1)
            worker.assert_not_called()
        with patch.dict(os.environ, environment(), clear=True), patch.object(
                github_runner, "checked_out_sha", return_value=ATTEMPT.source_sha), patch.object(
                github_runner, "supervise", return_value=0), redirect_stdout(StringIO()):
            self.assertEqual(github_runner.main([]), 1)  # Missing summary cannot claim full success.
        for failure in (RuntimeError("private-secret"), KeyboardInterrupt()):
            output = StringIO()
            with patch.dict(os.environ, environment(), clear=True), patch.object(
                    github_runner, "checked_out_sha", return_value=ATTEMPT.source_sha), patch.object(
                    github_runner, "supervise", side_effect=failure), redirect_stdout(output):
                self.assertEqual(github_runner.main([]), 1)
            self.assertNotIn("private-secret", output.getvalue())

    def test_help_and_unsupported_local_invocation_never_need_credentials(self):
        for args, expected in ((["--help"], 0), ([], 1), (["private-secret"], 1)):
            result = subprocess.run([sys.executable, "-B", "-m", "troubleshooter_deployment.github_runner", *args],
                env={"PATH": os.environ.get("PATH", "")}, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, expected)
            self.assertNotIn(b"Traceback", result.stderr)
            self.assertNotIn(b"private-secret", result.stdout + result.stderr)
