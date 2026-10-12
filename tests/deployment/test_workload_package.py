"""Resource Manager packages include only reviewed source, reproducibly."""
from hashlib import sha256
import base64
import gzip
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile

from troubleshooter_deployment.workload_package import build_workload_package, SOURCE_FILES
from troubleshooter_deployment.host_startup import host_source_paths


class WorkloadPackageTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        (self.root / "terraform").mkdir()
        for name in SOURCE_FILES:
            self.source(name).write_bytes(("synthetic " + name).encode())
        for name in host_source_paths():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(("synthetic " + name).encode())

    def source(self, name):
        return self.root / ("LICENSE" if name == "LICENSE" else "terraform/" + name)

    def test_allowlist_determinism_and_archive_permissions(self):
        for name in ("secret.tfvars.json", "terraform.tfstate", ".env", "unexpected.tf"):
            (self.root / "terraform" / name).write_text("must never ship")
        first = build_workload_package(self.root)
        for name in SOURCE_FILES:
            self.source(name).chmod(0o600)
        second = build_workload_package(self.root)
        self.assertEqual(first, second)
        self.assertEqual(first.sha256, sha256(first.content).hexdigest())
        with ZipFile(BytesIO(first.content)) as archive:
            self.assertEqual(archive.namelist(), sorted((*SOURCE_FILES, "host_startup.tf.json")))
            for item in archive.infolist():
                self.assertEqual(item.date_time, (2026, 1, 1, 0, 0, 0))
                self.assertEqual(item.external_attr >> 16, 0o100644)
                if item.filename != "host_startup.tf.json":
                    self.assertEqual(archive.read(item), self.source(item.filename).read_bytes())
            startup = json.loads(archive.read("host_startup.tf.json"))["locals"]["host_user_data"]
            config = json.loads(gzip.decompress(base64.b64decode(startup)).split(b"\n", 1)[1])
            self.assertEqual(len(config["write_files"]), len(host_source_paths()))

    def test_missing_or_nonregular_source_fails_closed(self):
        source = self.source("variables.tf")
        source.unlink()
        with self.assertRaisesRegex(ValueError, "workload source"):
            build_workload_package(self.root)
        source.mkdir()
        with self.assertRaisesRegex(ValueError, "workload source"):
            build_workload_package(self.root)

    def test_symlinked_file_or_source_directory_is_rejected(self):
        source = self.source("variables.tf")
        source.unlink()
        source.symlink_to(self.root / "LICENSE")
        with self.assertRaisesRegex(ValueError, "workload source"):
            build_workload_package(self.root)
        source.unlink()
        source.write_text("variable")
        (self.root / "terraform").rename(self.root / "redirected")
        (self.root / "terraform").symlink_to(self.root / "redirected", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "workload source"):
            build_workload_package(self.root)
        with self.assertRaisesRegex(ValueError, "workload source"):
            build_workload_package(self.root / "terraform")

    def test_oversized_file_is_rejected(self):
        self.source("variables.tf").write_bytes(b"x" * (128 * 1024 + 1))
        with self.assertRaisesRegex(ValueError, "workload source"):
            build_workload_package(self.root)

    def test_actual_repository_package_has_no_runtime_inputs(self):
        root = Path(__file__).resolve().parents[2]
        package = build_workload_package(root)
        with ZipFile(BytesIO(package.content)) as archive:
            self.assertEqual(len(archive.namelist()), 12)
            self.assertIn(".terraform.lock.hcl", archive.namelist())
            self.assertIn("LICENSE", archive.namelist())
            self.assertNotIn("deployment.tfvars.json.example", archive.namelist())

    def test_symlinked_host_source_or_parent_is_rejected(self):
        source = self.root / "troubleshooter_host/inputs.py"
        source.unlink()
        source.symlink_to(self.root / "LICENSE")
        with self.assertRaisesRegex(ValueError, "workload source"):
            build_workload_package(self.root)
        source.unlink()
        source.write_bytes(b"synthetic")
        (self.root / "troubleshooter_host").rename(self.root / "other")
        (self.root / "troubleshooter_host").symlink_to(self.root / "other", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "workload source"):
            build_workload_package(self.root)
