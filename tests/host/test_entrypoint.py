from contextlib import redirect_stderr
from io import StringIO
import unittest
from unittest.mock import patch

from troubleshooter_host import __main__ as entry
from troubleshooter_host.inputs import HostInputs
from tests.host.test_inputs import metadata


class EntrypointTests(unittest.TestCase):
    def test_invalid_or_unprivileged_invocation_is_sanitized(self):
        for arguments, uid in [([], 0), (["bad"], 0), (["storage"], 10001)]:
            output = StringIO()
            with patch.object(entry.os, "geteuid", return_value=uid), redirect_stderr(output):
                self.assertEqual(entry.main(arguments), 1)
            self.assertEqual(output.getvalue(), "Host startup failed: configuration or service unavailable.\n")

    def test_stop_does_not_depend_on_working_metadata_or_storage(self):
        with patch.object(entry.os, "geteuid", return_value=0), patch.object(entry, "remove_container") as stop, patch.object(entry, "load_metadata") as metadata_load:
            self.assertEqual(entry.main(["stop"]), 0)
            stop.assert_called_once()
            metadata_load.assert_not_called()

    def test_actions_load_validated_metadata_and_bound_storage_wait(self):
        inputs = HostInputs.from_imds(metadata())
        with patch.object(entry.os, "geteuid", return_value=0), patch.object(entry, "load_metadata", return_value=inputs), patch.object(
                entry, "directory"), patch.object(entry, "wait_for_volume") as wait, patch.object(entry, "prepare_filesystem") as storage, patch.object(
                entry.os.path, "ismount", return_value=False):
            self.assertEqual(entry.main(["storage"]), 0)
            wait.assert_called_once_with(inputs)
            storage.assert_called_once()
        for action, target in [("prepare", "prepare_runtime"), ("reconcile", "reconcile")]:
            with patch.object(entry.os, "geteuid", return_value=0), patch.object(entry, "load_metadata", return_value=inputs), patch.object(entry, target) as call:
                self.assertEqual(entry.main([action]), 0)
                call.assert_called_once_with(inputs)

    def test_run_requires_same_prepared_revision_and_hides_raw_failures(self):
        inputs = HostInputs.from_imds(metadata())
        with patch.object(entry.os, "geteuid", return_value=0), patch.object(entry, "load_metadata", return_value=inputs), patch.object(
                entry, "read_json", return_value={"revision": inputs.revision}), patch.object(entry, "finish_mount"), patch.object(
                entry, "ensure_network"), patch.object(entry.os, "execvpe") as run:
            self.assertEqual(entry.main(["run"]), 0)
            self.assertEqual(run.call_args.args[1][-1], inputs.artifact_image_id)
        output = StringIO()
        with patch.object(entry.os, "geteuid", return_value=0), patch.object(entry, "load_metadata", side_effect=ValueError("never-print-this-secret")), redirect_stderr(output):
            self.assertEqual(entry.main(["run"]), 1)
        self.assertNotIn("never-print", output.getvalue())

    def test_device_wait_is_finite(self):
        inputs = HostInputs.from_imds(metadata())
        with patch.object(entry.Path, "exists", side_effect=[False, True]), patch.object(entry.time, "sleep") as sleep:
            entry.wait_for_volume(inputs)
            sleep.assert_called_once_with(3)
        with patch.object(entry.Path, "exists", return_value=False), patch.object(entry.time, "sleep") as sleep:
            with self.assertRaises(ValueError):
                entry.wait_for_volume(inputs)
            self.assertEqual(sleep.call_count, 40)
