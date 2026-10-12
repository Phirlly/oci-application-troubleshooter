"""The root Linux fixture supervisor must not leave namespace peers alive."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from tests.integration.check_linux_host import run, systemd_check


class HostFixtureSupervisionTests(unittest.TestCase):
    def test_interruption_at_process_registration_still_kills_and_reaps(self):
        actual = subprocess.Popen
        created = []
        def spawn(*args, **kwargs):
            process = actual(*args, **kwargs)
            created.append(process)
            os.kill(os.getpid(), signal.SIGTERM)
            return process
        def interrupt(_number, _frame):
            raise RuntimeError("synthetic interruption")
        previous = signal.signal(signal.SIGTERM, interrupt)
        try:
            with patch("tests.integration.check_linux_host.subprocess.Popen", side_effect=spawn):
                with self.assertRaisesRegex(RuntimeError, "synthetic interruption"):
                    run([sys.executable, "-c", "import time; time.sleep(20)"])
            self.assertEqual(len(created), 1)
            self.assertIsNotNone(created[0].poll())
        finally:
            signal.signal(signal.SIGTERM, previous)

    def test_success_and_failure_capture_only_owned_process_output(self):
        result = run([sys.executable, "-c", "import sys; print('synthetic'); print('diagnostic', file=sys.stderr)"])
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "synthetic\n", "diagnostic\n"))
        with self.assertRaises(RuntimeError):
            run([sys.executable, "-c", "raise SystemExit(2)"])
        result = run([sys.executable, "-c", "raise SystemExit(2)"], check=False)
        self.assertEqual(result.returncode, 2)

    def test_systemd_accepts_only_clean_units_and_expected_negative_cycle(self):
        cycle = ("ocitroubleshooter-storage.service: Found ordering cycle on local-fs.target/start\n"
                 "ocitroubleshooter-storage.service: Found dependency on var-lib-ocitroubleshooter-data.mount/start\n")
        clean = subprocess.CompletedProcess([], 0, "", "")
        for status in (0, 1):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as name:
                negative = subprocess.CompletedProcess([], status, "", cycle)
                with patch("tests.integration.check_linux_host.run", side_effect=[clean, negative]) as command:
                    systemd_check(Path(name))
                calls = command.call_args_list
                self.assertEqual(len(calls), 2)
                self.assertEqual(calls[0].kwargs["env"]["LC_ALL"], "C")
                self.assertFalse(calls[1].kwargs["check"])
                self.assertNotIn("_netdev", (Path(name) / "var-lib-ocitroubleshooter-data.mount").read_text())

    def test_systemd_rejects_zero_exit_cycle_for_actual_units(self):
        result = subprocess.CompletedProcess([], 0, "", "synthetic.service: Found ordering cycle on basic.target/start")
        with tempfile.TemporaryDirectory() as name:
            with patch("tests.integration.check_linux_host.run", return_value=result) as command:
                with self.assertRaisesRegex(RuntimeError, "actual units.*ordering cycle"):
                    systemd_check(Path(name))
            self.assertEqual(command.call_count, 1)

    def test_systemd_rejects_unrelated_negative_diagnostics(self):
        clean = subprocess.CompletedProcess([], 0, "", "")
        for status, diagnostic in ((0, ""), (1, "Failed to load unit"),
                                   (0, "unrelated.service: Found ordering cycle on basic.target/start")):
            with self.subTest(diagnostic=diagnostic), tempfile.TemporaryDirectory() as name:
                with patch("tests.integration.check_linux_host.run", side_effect=[clean, subprocess.CompletedProcess([], status, "", diagnostic)]):
                    with self.assertRaisesRegex(RuntimeError, "negative fixture"):
                        systemd_check(Path(name))

    def test_timeout_kills_descendant_even_after_leader_exits(self):
        with tempfile.TemporaryDirectory() as name:
            identity = Path(name) / "child.pid"
            heartbeat = Path(name) / "heartbeat"
            code = ("import os,sys,time\npid=os.fork()\n"
                    "if pid:\n open(sys.argv[1], 'w').write(str(pid))\n os._exit(0)\n"
                    "for i in range(2000):\n open(sys.argv[2], 'w').write(str(i))\n time.sleep(.01)\n")
            try:
                with self.assertRaises(subprocess.TimeoutExpired):
                    run([sys.executable, "-c", code, str(identity), str(heartbeat)], timeout=0.5)
                time.sleep(0.05)
                stopped = heartbeat.read_bytes()
                self.assertTrue(stopped)
                time.sleep(0.15)
                self.assertEqual(heartbeat.read_bytes(), stopped, "Fixture descendant survived timeout")
            finally:
                if identity.exists():
                    try:
                        os.kill(int(identity.read_text()), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
