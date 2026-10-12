from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from troubleshooter_host.inputs import HostError, HostInputs, encoded
from troubleshooter_host.runtime import container_arguments, prepare_runtime, remove_container, reconcile
from tests.host.test_inputs import metadata


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.inputs = HostInputs.from_imds(metadata())

    def test_payload_arguments_are_rootful_unprivileged_and_exact(self):
        argv = container_arguments(self.inputs)
        for flag in ["--pull=never", "--read-only", "--read-only-tmpfs=false", "--cap-drop=ALL",
                     "--security-opt=no-new-privileges", "--user=10001:10001"]:
            self.assertIn(flag, argv)
        self.assertNotIn("--userns=keep-id", " ".join(argv))
        self.assertIn("/var/lib/ocitroubleshooter/data/application:/data:rw,Z", argv)
        self.assertIn("/run/ocitroubleshooter/runtime:/run/demo:ro,Z", argv)
        self.assertEqual(argv[-1], self.inputs.artifact_image_id)
        self.assertNotIn("--privileged", argv)

    def test_prepare_validates_before_activation_and_reuses_verified_image(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            runtime = root / "runtime"
            runtime.mkdir()
            calls = []
            def run(argv, **kwargs):
                calls.append((argv, kwargs))
                return b""
            with patch("troubleshooter_host.runtime.ROOT", root), patch("troubleshooter_host.runtime.RUNTIME", runtime), patch(
                    "troubleshooter_host.runtime.CONTROL", root), patch("troubleshooter_host.runtime.directory"), patch(
                    "troubleshooter_host.runtime.finish_mount") as mount, patch("troubleshooter_host.runtime.ensure_network") as network, patch(
                    "troubleshooter_host.runtime.acquire_image") as acquire, patch("troubleshooter_host.runtime.verify_image") as verify, patch(
                    "troubleshooter_host.runtime.acquire_verifier", return_value=b"synthetic-verifier"), patch(
                    "troubleshooter_host.runtime.command", side_effect=run), patch("troubleshooter_host.host_io.os.fchown"):
                prepare_runtime(self.inputs)
                acquire.assert_called_once()
                mount.assert_called_once()
                network.assert_called_once()
                self.assertEqual((runtime / "presenter.json").read_bytes(), b"synthetic-verifier")
                self.assertEqual((runtime / "presenter.json").stat().st_mode & 0o777, 0o440)
                self.assertEqual((root / "active.json").read_bytes(), encoded({"revision": self.inputs.revision}))
                self.assertEqual(calls[0][1]["stdin"], b"synthetic-verifier")
                self.assertIn("--network=none", calls[0][0])
                prepare_runtime(self.inputs)
                acquire.assert_called_once()
                verify.assert_called_once()
                with self.assertRaises(HostError):
                    prepare_runtime(replace(self.inputs, release_sha="e" * 40))

    def test_invalid_secret_cannot_publish_active_revision(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            with patch("troubleshooter_host.runtime.ROOT", root), patch("troubleshooter_host.runtime.CONTROL", root), patch(
                    "troubleshooter_host.runtime.directory"), patch("troubleshooter_host.runtime.finish_mount"), patch(
                    "troubleshooter_host.runtime.ensure_network"), patch("troubleshooter_host.runtime.acquire_image"), patch(
                    "troubleshooter_host.runtime.acquire_verifier", return_value=b"bad"), patch(
                    "troubleshooter_host.runtime.command", side_effect=HostError("host_command_failed")):
                with self.assertRaises(HostError):
                    prepare_runtime(self.inputs)
                self.assertFalse((root / "active.json").exists())

    def test_stop_removes_only_recorded_container_identity(self):
        with tempfile.TemporaryDirectory() as name:
            cidfile = Path(name) / "app.cid"
            with patch("troubleshooter_host.runtime.CIDFILE", cidfile), patch("troubleshooter_host.runtime.command") as run:
                remove_container()
                run.assert_not_called()
                cidfile.write_text("foreign-name")
                with self.assertRaises(HostError):
                    remove_container()
                run.assert_not_called()
                cidfile.write_text("c" * 64)
                remove_container()
                self.assertEqual(run.call_args.args[0], ["podman", "rm", "--force", "--ignore", "c" * 64])
                self.assertFalse(cidfile.exists())
                cidfile.write_text("d" * 64)
                run.side_effect = lambda *args, **kwargs: cidfile.unlink()
                remove_container()

    def test_reconcile_only_restarts_running_service_after_validated_change(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "active.json").write_bytes(encoded({"revision": self.inputs.revision}))
            with patch("troubleshooter_host.runtime.CONTROL", root), patch("troubleshooter_host.runtime.command", return_value=b"active\n") as run:
                reconcile(self.inputs)
                run.assert_not_called()
                reconcile(replace(self.inputs, presenter_secret_version="2"))
                self.assertEqual(run.call_args.args[0], ["systemctl", "--no-block", "try-restart", "ocitroubleshooter.service"])
                run.reset_mock()
                run.return_value = b"failed\n"
                reconcile(replace(self.inputs, presenter_secret_version="2"))
                self.assertEqual(run.call_count, 1)

    def test_corrected_secret_recovers_failed_service_once_per_new_revision(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            previous = {"identity": self.inputs.identity, "revision": self.inputs.revision}
            (root / "attempt.json").write_bytes(encoded(previous))
            with patch("troubleshooter_host.runtime.CONTROL", root), patch("troubleshooter_host.runtime.ROOT", root), patch(
                    "troubleshooter_host.runtime.command", return_value=b"failed\n") as run:
                reconcile(self.inputs)
                run.assert_not_called()
                changed = replace(self.inputs, presenter_secret_version="2")
                reconcile(changed)
                self.assertEqual([call.args[0] for call in run.call_args_list], [
                    ["systemctl", "show", "--property=ActiveState", "--value", "ocitroubleshooter.service"],
                    ["systemctl", "reset-failed", "ocitroubleshooter.service"],
                    ["systemctl", "--no-block", "start", "ocitroubleshooter.service"]])
                run.reset_mock()
                reconcile(changed)
                run.assert_not_called()
                reconcile(replace(changed, release_sha="e" * 40))
                run.assert_not_called()
