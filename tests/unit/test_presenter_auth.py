"""Synthetic passwords only; real hashing and persistent session behavior."""
from dataclasses import replace
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from troubleshooter_app.credentials import Verifier, make_verifier, password_valid
from troubleshooter_app.sessions import SessionStore
from troubleshooter_app.authentication import Authentication, LoginBusy

PASSWORD = "synthetic-presenter-password"


class AuthenticationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.verifier = make_verifier(PASSWORD)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.now = 10000.0
        self.clock = lambda: self.now
        self.store = SessionStore(self.path, self.verifier, clock=self.clock)
        self.auth = Authentication(self.verifier, self.store, clock=self.clock)

    def test_real_login_and_wrong_password(self):
        self.assertIsNone(self.auth.login("incorrect-synthetic-password"))
        session = self.auth.login(PASSWORD)
        self.assertIsNotNone(session)
        self.assertEqual(len(session.token), 43)
        self.assertEqual(len(session.csrf), 43)
        self.assertEqual(self.store.lookup(session.token).csrf, session.csrf)
        database = (self.path / "presenter.sqlite3").read_bytes()
        for private in (session.token, PASSWORD, self.verifier.encoded):
            self.assertNotIn(private.encode(), database)

    def test_logout_revokes_and_random_cookie_cannot_authenticate(self):
        session = self.store.create()
        self.store.revoke(session.token)
        self.assertIsNone(self.store.lookup(session.token))
        for token in (None, "", "x" * 43, "x" * 10000, "../presenter.sqlite3"):
            self.assertIsNone(self.store.lookup(token))

    def test_idle_and_absolute_expiry(self):
        session = self.store.create()
        self.now += 1800
        self.assertIsNone(self.store.lookup(session.token))
        session = self.store.create()
        for _ in range(15):
            self.now += 1799
            self.assertIsNotNone(self.store.lookup(session.token))
        self.now = session.created + 28800
        self.assertIsNone(self.store.lookup(session.token))

    def test_clock_rollback_invalidates_existing_session(self):
        session = self.store.create()
        self.now -= 1
        self.assertIsNone(self.store.lookup(session.token))

    def test_concurrent_request_timestamp_is_read_after_acquiring_transaction(self):
        session = self.store.create()
        original_transaction = self.store.transaction

        @contextmanager
        def interleaved_transaction():
            # A second request completes before this request acquires its lock.
            self.now += 1
            with original_transaction() as connection:
                connection.execute('UPDATE sessions SET last_seen=?', (self.now,))
            with original_transaction() as connection:
                yield connection

        with patch.object(self.store, 'transaction', interleaved_transaction):
            self.assertIsNotNone(self.store.lookup(session.token))

    def test_restart_preserves_sessions_rotation_revokes_without_erasing_other_tables(self):
        session = self.store.create()
        with sqlite3.connect(self.path / "presenter.sqlite3") as connection:
            connection.execute("CREATE TABLE sample_case (value TEXT)")
            connection.execute("INSERT INTO sample_case VALUES ('retained')")
        restarted = SessionStore(self.path, self.verifier, clock=self.clock)
        self.assertIsNotNone(restarted.lookup(session.token))
        rotated = SessionStore(self.path, replace(self.verifier, revision="b" * 32), clock=self.clock)
        self.assertIsNone(rotated.lookup(session.token))
        with sqlite3.connect(self.path / "presenter.sqlite3") as connection:
            self.assertEqual(connection.execute("SELECT value FROM sample_case").fetchone(), ("retained",))

    def test_same_revision_with_changed_verifier_rejects_startup(self):
        other = make_verifier("different-synthetic-password")
        with self.assertRaises(ValueError):
            SessionStore(self.path, replace(other, revision=self.verifier.revision))

    def test_missing_database_after_initialization_does_not_reset_auth(self):
        (self.path / "presenter.sqlite3").unlink()
        with self.assertRaises(Exception):
            self.store.lookup("a" * 43)
        with self.assertRaises(ValueError):
            SessionStore(self.path, self.verifier)
        self.assertFalse((self.path / "presenter.sqlite3").exists())

    def test_session_capacity_evicts_oldest(self):
        sessions = []
        for _ in range(9):
            sessions.append(self.store.create())
            self.now += 1
        self.assertIsNone(self.store.lookup(sessions[0].token))
        self.assertTrue(all(self.store.lookup(s.token) for s in sessions[1:]))

    def test_login_rate_and_concurrency_are_bounded(self):
        with patch.object(Verifier, "matches", return_value=False) as verify:
            for _ in range(5):
                self.assertIsNone(self.auth.login(PASSWORD))
            with self.assertRaises(LoginBusy):
                self.auth.login(PASSWORD)
            self.assertEqual(verify.call_count, 5)
            self.now += 60
            self.assertIsNone(self.auth.login(PASSWORD))
        self.auth.hash_slot.acquire()
        try:
            with self.assertRaises(LoginBusy):
                self.auth.login(PASSWORD)
        finally:
            self.auth.hash_slot.release()

    def test_hash_failure_releases_concurrency_slot(self):
        with patch.object(Verifier, "matches", side_effect=RuntimeError("synthetic-private")):
            with self.assertRaises(RuntimeError):
                self.auth.login(PASSWORD)
        self.assertIsNotNone(self.auth.login(PASSWORD))

    def test_password_and_verifier_inputs_are_bounded(self):
        for value in (None, 1, "short", "a" * 257, "\ud800"):
            self.assertFalse(password_valid(value))
            with self.assertRaises(ValueError):
                make_verifier(value)
            self.assertFalse(self.verifier.matches(value))
        self.assertTrue(password_valid("a" * 12))
        self.assertTrue(password_valid("a" * 256))
        payload = self.verifier.to_bytes()
        self.assertEqual(Verifier.from_bytes(payload), self.verifier)
        for candidate in (b"{}", b"[]", b"not-json", payload * 20,
                          payload.replace(b"m=65536", b"m=999999999"),
                          payload.replace(b'"schema":1', b'"schema":2')):
            with self.assertRaises(ValueError):
                Verifier.from_bytes(candidate)
        self.assertNotIn(self.verifier.encoded, repr(self.verifier))

    def test_noncanonical_base64_is_rejected_before_password_verification(self):
        invalid = '$argon2id$v=19$m=65536,t=3,p=1$' + 'B' * 22 + '$' + 'B' * 43
        with self.assertRaisesRegex(ValueError, 'invalid_verifier'):
            Verifier('a' * 32, invalid)

    def test_unsupported_database_schema_and_missing_settings_fail_closed(self):
        with sqlite3.connect(self.store.path) as connection:
            connection.execute('PRAGMA user_version=2')
        with self.assertRaisesRegex(ValueError, 'unsupported_authentication_schema'):
            SessionStore(self.path, self.verifier)
        with sqlite3.connect(self.store.path) as connection:
            connection.execute('PRAGMA user_version=1')
            connection.execute('DELETE FROM auth_settings')
        with self.assertRaisesRegex(ValueError, 'verifier_revision_conflict'):
            SessionStore(self.path, self.verifier)
        with self.assertRaisesRegex(ValueError, 'authentication_storage_unavailable'):
            self.store.check()


if __name__ == "__main__":
    unittest.main()
