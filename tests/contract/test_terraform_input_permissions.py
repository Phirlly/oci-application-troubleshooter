"""Offline regression for input readability by the unprivileged container."""
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tests.contract import check_terraform_inputs as checker


class InputsInspected(Exception):
    """Stop at the first plan after inspecting the generated inputs."""


def probe(engine):
    assertions = unittest.TestCase()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "terraform"
        source.mkdir()
        for name in ("variables.tf", "deployment.tfvars.json.example"):
            shutil.copyfile(checker.ROOT / "terraform" / name, source / name)
            (source / name).chmod(0o600)
        workspaces = set()

        def inspect(command, **kwargs):
            if engine == "podman":
                mount = command[command.index("--volume") + 1]
                workspace = Path(mount.removesuffix(":/config:ro"))
            else:
                workspace = Path(command[1].removeprefix("-chdir="))
            workspaces.add(workspace)
            assertions.assertEqual(stat.S_IMODE(workspace.stat().st_mode), 0o755)
            names = ["variables.tf", "empty.tfrc"]
            if "plan" in command:
                names.append("deployment.tfvars.json")
            for name in names:
                assertions.assertEqual(
                    stat.S_IMODE((workspace / name).stat().st_mode), 0o644, name,
                )
            assertions.assertEqual((workspace / "empty.tfrc").read_text(), "")
            if "plan" in command:
                assertions.assertEqual(
                    json.loads((workspace / "deployment.tfvars.json").read_text()),
                    checker.VALUES,
                )
                raise InputsInspected()
            return subprocess.CompletedProcess(
                command, 0, stdout='{"terraform_version": "1.5.7"}', stderr="",
            )

        with patch.object(checker, "ROOT", root), patch.object(checker.subprocess, "run", inspect):
            with assertions.assertRaises(InputsInspected):
                checker.check(engine)
        assertions.assertEqual(len(workspaces), 1)
        assertions.assertFalse(next(iter(workspaces)).exists())
        for path in source.iterdir():
            assertions.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


@unittest.skipUnless(os.name == "posix", "POSIX file modes and child umask are required")
class TerraformInputPermissionsTests(unittest.TestCase):
    def run_with_umask(self, mask):
        for engine in ("local", "podman"):
            with self.subTest(engine=engine, umask=oct(mask)):
                result = subprocess.run(
                    [sys.executable, "-B", "-m", "tests.contract.test_terraform_input_permissions",
                     "--probe", engine],
                    cwd=checker.ROOT, umask=mask, stdin=subprocess.DEVNULL,
                    capture_output=True, text=True, timeout=15,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_inputs_are_readable_under_standard_umask(self):
        self.run_with_umask(0o022)

    def test_inputs_are_readable_under_restrictive_umask(self):
        self.run_with_umask(0o077)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--probe":
        probe(sys.argv[2])
    else:
        unittest.main()
