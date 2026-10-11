"""Persistent opaque presenter sessions on an already verified local mount."""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time

IDLE_SECONDS = 1800
ABSOLUTE_SECONDS = 28800
MAX_SESSIONS = 8


@dataclass(frozen=True, repr=False)
class Session:
    token: str
    csrf: str
    created: float


class SessionStore:
    def __init__(self, directory: Path, verifier, *, clock=time.time):
        self.path = directory / "presenter.sqlite3"
        self.clock = clock
        marker = directory / ".presenter-initialized"
        if (not directory.is_dir() or directory.is_symlink() or self.path.is_symlink()
                or marker.is_symlink() or (marker.exists() and not self.path.is_file())):
            raise ValueError("authentication_storage_unavailable")
        if not self.path.exists():
            os.close(os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600))
        digest = hashlib.sha256(verifier.encoded.encode()).hexdigest()
        with self.transaction() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if version == 0 and not tables and not marker.exists():
                connection.execute("CREATE TABLE auth_settings (id INTEGER PRIMARY KEY CHECK(id=1), revision TEXT NOT NULL, verifier_digest TEXT NOT NULL)")
                connection.execute("CREATE TABLE sessions (token_hash TEXT PRIMARY KEY, csrf TEXT NOT NULL, created REAL NOT NULL, last_seen REAL NOT NULL)")
                connection.execute("PRAGMA user_version=1")
                connection.execute("INSERT INTO auth_settings VALUES (1, ?, ?)", (verifier.revision, digest))
            elif version != 1:
                raise ValueError("unsupported_authentication_schema")
            else:
                saved = connection.execute("SELECT revision, verifier_digest FROM auth_settings WHERE id=1").fetchone()
                if saved is None or (saved[0] == verifier.revision and saved[1] != digest):
                    raise ValueError("verifier_revision_conflict")
                if saved[0] != verifier.revision:
                    connection.execute("DELETE FROM sessions")
                    connection.execute("UPDATE auth_settings SET revision=?, verifier_digest=? WHERE id=1", (verifier.revision, digest))
        if not marker.exists():
            with os.fdopen(os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb") as output:
                output.write(b"1\n")
                output.flush()
                os.fsync(output.fileno())

    @contextmanager
    def transaction(self):
        connection = sqlite3.connect(self.path.resolve().as_uri() + "?mode=rw", uri=True,
                                     timeout=1, isolation_level=None)
        try:
            if connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                raise ValueError("authentication_storage_unavailable")
            connection.execute("PRAGMA synchronous=EXTRA")
            if connection.execute("PRAGMA synchronous").fetchone()[0] != 3:
                raise ValueError("authentication_storage_unavailable")
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        finally:
            connection.close()

    def create(self):
        with self.transaction() as connection:
            now = self.clock()
            session = Session(secrets.token_urlsafe(32), secrets.token_urlsafe(32), now)
            connection.execute("DELETE FROM sessions WHERE last_seen<=? OR created<=? OR last_seen>?",
                               (now - IDLE_SECONDS, now - ABSOLUTE_SECONDS, now))
            connection.execute("DELETE FROM sessions WHERE token_hash IN (SELECT token_hash FROM sessions ORDER BY created DESC, rowid DESC LIMIT -1 OFFSET ?)", (MAX_SESSIONS - 1,))
            connection.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)",
                               (self.digest(session.token), session.csrf, now, now))
        return session

    @staticmethod
    def digest(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def lookup(self, token):
        if type(token) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            return None
        digest = self.digest(token)
        with self.transaction() as connection:
            now = self.clock()
            row = connection.execute("SELECT csrf, created, last_seen FROM sessions WHERE token_hash=?", (digest,)).fetchone()
            if row is None:
                return None
            csrf, created, seen = row
            if not seen <= now < min(seen + IDLE_SECONDS, created + ABSOLUTE_SECONDS):
                connection.execute("DELETE FROM sessions WHERE token_hash=?", (digest,))
                return None
            connection.execute("UPDATE sessions SET last_seen=? WHERE token_hash=?", (now, digest))
            return Session(token, csrf, created)

    def revoke(self, token):
        with self.transaction() as connection:
            connection.execute("DELETE FROM sessions WHERE token_hash=?", (self.digest(token),))

    def check(self):
        with self.transaction() as connection:
            if connection.execute("SELECT COUNT(*) FROM auth_settings").fetchone()[0] != 1:
                raise ValueError("authentication_storage_unavailable")
