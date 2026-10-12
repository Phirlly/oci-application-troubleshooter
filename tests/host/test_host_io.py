import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

from troubleshooter_host.host_io import atomic_write, read_json, command, load_metadata
from troubleshooter_host.inputs import HostError
from tests.host.test_inputs import metadata


class HostIOTests(unittest.TestCase):
    def test_atomic_replacement_has_exact_permissions_and_no_pending_file(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "record.json"
            atomic_write(path, b'{"state":1}', mode=0o600)
            atomic_write(path, b'{"state":2}', mode=0o440)
            self.assertEqual(read_json(path), {"state": 2})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o440)
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_untrusted_file_shapes_and_duplicate_fields_are_rejected(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            path = root / "record"
            path.write_bytes(b'{"a":1,"a":2}')
            with self.assertRaises(HostError):
                read_json(path)
            alias = root / "alias"
            alias.symlink_to(path)
            for operation in [lambda: read_json(alias), lambda: atomic_write(alias, b"{}")]:
                with self.assertRaises(HostError):
                    operation()
            path.write_bytes(b"x" * 16385)
            with self.assertRaises(HostError):
                read_json(path)
            with self.assertRaises(HostError):
                atomic_write(root, b"{}")

    def test_command_captures_output_and_never_inherits_credentials(self):
        with patch.dict(os.environ, {"OCI_PRIVATE_KEY": "never-child", "SECRET": "never-child"}):
            result = command([sys.executable, "-c", "import os; print(os.environ.get('SECRET')); print('ok')"])
        self.assertEqual(result, b"None\nok\n")

    def test_command_rejects_failure_oversize_and_timeout_without_output_in_error(self):
        for program, options in [
            ("import sys; print('sensitive'); sys.exit(7)", {}),
            ("print('sensitive' * 100)", {"maximum": 32}),
            ("import time; time.sleep(5)", {"timeout": 0.1}),
        ]:
            with self.subTest(program=program), self.assertRaisesRegex(HostError, "^host_command_failed$"):
                command([sys.executable, "-c", program], **options)

    def test_command_supplies_stdin_without_shell(self):
        self.assertEqual(command([sys.executable, "-c", "import sys; print(sys.stdin.read())"],
                                 stdin=b"literal $x `not-shell`"), b"literal $x `not-shell`\n")

    def test_metadata_request_is_fixed_direct_bounded_and_authenticated(self):
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def read(self, size):
                self.size = size
                return json.dumps(metadata()).encode()
        response = Response()
        with patch("troubleshooter_host.host_io.build_opener") as build:
            build.return_value.open.return_value = response
            inputs = load_metadata()
            request = build.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, "http://169.254.169.254/opc/v2/instance/")
            self.assertEqual(request.get_header("Authorization"), "Bearer Oracle")
            self.assertEqual(build.return_value.open.call_args.kwargs["timeout"], 5)
            self.assertEqual(response.size, 65537)
            self.assertEqual(inputs.generation_id, "a" * 32)
            self.assertEqual(build.call_args.args[0].proxies, {})
        with patch("troubleshooter_host.host_io.build_opener", side_effect=OSError("private")):
            with self.assertRaisesRegex(HostError, "^host_metadata_unavailable$"):
                load_metadata()
