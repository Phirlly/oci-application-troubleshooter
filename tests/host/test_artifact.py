import base64
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from troubleshooter_host.artifact import acquire_image, acquire_verifier, verify_image
from troubleshooter_host.inputs import HostError, HostInputs, encoded
from tests.host.test_inputs import metadata


def image_info(inputs):
    return [{"Id": inputs.artifact_image_id.removeprefix("sha256:"), "Architecture": "amd64", "Os": "linux",
             "Config": {"User": "10001:10001", "Entrypoint": ["python", "-m", "troubleshooter_app"]}}]


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.inputs = replace(HostInputs.from_imds(metadata()), artifact_sha256=sha256(b"archive").hexdigest())

    def test_download_is_exact_bounded_verified_before_load_and_removed(self):
        calls = []
        def run(argv, **options):
            calls.append((argv, options))
            if argv[0] == "oci":
                Path(argv[argv.index("--file") + 1]).write_bytes(b"archive")
            if argv[:3] == ["podman", "image", "inspect"]:
                return encoded(image_info(self.inputs))
            return b""
        with tempfile.TemporaryDirectory() as name, patch("troubleshooter_host.artifact.command", side_effect=run):
            acquire_image(self.inputs, Path(name))
            self.assertEqual(list(Path(name).iterdir()), [])
        argv, bounds = calls[0]
        for option, value in [("--namespace-name", self.inputs.artifact_namespace),
                              ("--name", self.inputs.artifact_object), ("--auth", "instance_principal"),
                              ("--region", self.inputs.region), ("--config-file", "/dev/null")]:
            self.assertEqual(argv[argv.index(option) + 1], value)
        self.assertIn("--no-retry", argv)
        self.assertIn("--no-multipart", argv)
        self.assertEqual(bounds["file_limit"], 2 * 1024**3)
        self.assertEqual(calls[1][0][:3], ["podman", "load", "--input"])
        self.assertEqual(calls[2][0], ["podman", "image", "inspect", self.inputs.artifact_image_id])

    def test_corrupt_or_empty_artifact_never_reaches_podman(self):
        for content in [b"", b"corrupt"]:
            calls = []
            def run(argv, **options):
                calls.append(argv)
                Path(argv[argv.index("--file") + 1]).write_bytes(content)
            with tempfile.TemporaryDirectory() as name, patch("troubleshooter_host.artifact.command", side_effect=run):
                with self.assertRaisesRegex(HostError, "artifact_invalid"):
                    acquire_image(self.inputs, Path(name))
                self.assertEqual(len(calls), 1)
                self.assertEqual(list(Path(name).iterdir()), [])

    def test_image_id_platform_user_and_entrypoint_are_bound(self):
        for field, value in [("Id", "sha256:" + "e" * 64), ("Architecture", "arm64"), ("Os", "windows"),
                              ("Config", {"User": "0", "Entrypoint": []})]:
            response = image_info(self.inputs)
            response[0][field] = value
            with patch("troubleshooter_host.artifact.command", return_value=encoded(response)):
                with self.assertRaisesRegex(HostError, "artifact_invalid"):
                    verify_image(self.inputs)

    def test_verifier_fetch_pins_version_and_never_includes_plaintext(self):
        value = encoded({"data": {"secret-id": self.inputs.presenter_secret_ocid, "version-number": 1,
                                  "secret-bundle-content": {"content-type": "BASE64",
                                                            "content": base64.b64encode(b"verifier").decode()}}})
        with patch("troubleshooter_host.artifact.command", return_value=value) as run:
            self.assertEqual(acquire_verifier(self.inputs), b"verifier")
            argv = run.call_args.args[0]
            self.assertEqual(argv[-4:], ["--secret-id", self.inputs.presenter_secret_ocid, "--version-number", "1"])
            self.assertEqual(run.call_args.kwargs["maximum"], 16384)
