"""Resource Manager packages include only reviewed source, reproducibly."""
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile

from troubleshooter_deployment.workload_package import build_workload_package, SOURCE_FILES


class WorkloadPackageTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        (self.root / "terraform").mkdir()
        for name in SOURCE_FILES:
            self.source(name).write_bytes(("synthetic " + name).encode())

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
            self.assertEqual(archive.namelist(), sorted(SOURCE_FILES))
            for item in archive.infolist():
                self.assertEqual(item.date_time, (2026, 1, 1, 0, 0, 0))
                self.assertEqual(item.external_attr >> 16, 0o100644)
                self.assertEqual(archive.read(item), self.source(item.filename).read_bytes())

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
            self.assertEqual(len(archive.namelist()), 11)
            self.assertIn(".terraform.lock.hcl", archive.namelist())
            self.assertIn("LICENSE", archive.namelist())
            self.assertNotIn("deployment.tfvars.json.example", archive.namelist())
