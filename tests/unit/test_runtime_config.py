import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from troubleshooter_app.configuration import RuntimeConfig


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "config.json"
        self.value = {"public_origin": "https://demo.example", "release_sha": "a" * 40}

    def load(self, value):
        self.path.write_text(json.dumps(value))
        return RuntimeConfig.load(self.path)

    def test_closed_configuration(self):
        config = self.load(self.value)
        self.assertEqual(config.public_origin, "https://demo.example")
        self.assertEqual(config.release_sha, "a" * 40)
        for value in ({}, [], {**self.value, "password": "private"},
                      {**self.value, "release_sha": "main"}):
            with self.assertRaisesRegex(ValueError, "invalid_configuration"):
                self.load(value)

    def test_requires_canonical_https_origin(self):
        for origin in ("http://demo.example", "https://demo.example/", "https://user@demo.example",
                       "https://demo.example?q=x", "https://demo.example#x", "https://demo.example/path",
                       "https://demo.example:invalid", "https://DEMO.example", "https://demo.example:443",
                       "https://demo.example\\.evil", "https://demo.example\n", "https://", None):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                self.load({**self.value, "public_origin": origin})
        self.assertEqual(self.load({**self.value, "public_origin": "https://localhost:9443"}).public_origin,
                         "https://localhost:9443")

    def test_missing_oversized_duplicate_and_unreadable_configuration(self):
        with self.assertRaises(ValueError):
            RuntimeConfig.load(self.path)
        for raw in (b"x" * 4097, b'{"public_origin":1,"public_origin":2}', b'\xff', b'{'):
            self.path.write_bytes(raw)
            with self.assertRaises(ValueError):
                RuntimeConfig.load(self.path)
        with patch.object(Path, "open", side_effect=PermissionError("private")):
            with self.assertRaisesRegex(ValueError, "^invalid_configuration$"):
                RuntimeConfig.load(self.path)
