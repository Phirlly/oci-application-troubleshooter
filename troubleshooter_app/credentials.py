"""Bounded Argon2id verifier format shared by enrollment and the application."""
from dataclasses import dataclass
import base64
import json
import re
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

HASHER = PasswordHasher(memory_cost=65536, time_cost=3, parallelism=1, hash_len=32, salt_len=16)
ENCODING = re.compile(r"\$argon2id\$v=19\$m=65536,t=3,p=1\$[A-Za-z0-9+/]{22}\$[A-Za-z0-9+/]{43}")


def password_valid(value):
    try:
        return type(value) is str and 12 <= len(value.encode("utf-8")) <= 256
    except UnicodeError:
        return False


def unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("invalid_configuration")
        result[key] = value
    return result


@dataclass(frozen=True, repr=False)
class Verifier:
    revision: str
    encoded: str

    def __post_init__(self):
        if (type(self.revision) is not str or not re.fullmatch(r"[a-f0-9]{32}", self.revision)
                or type(self.encoded) is not str or not ENCODING.fullmatch(self.encoded)):
            raise ValueError("invalid_verifier")
        for part, length in zip(self.encoded.split('$')[-2:], (16, 32)):
            decoded = base64.b64decode(part + '=' * (-len(part) % 4), validate=True)
            if len(decoded) != length or base64.b64encode(decoded).decode().rstrip('=') != part:
                raise ValueError('invalid_verifier')

    @classmethod
    def from_bytes(cls, raw):
        try:
            if type(raw) is not bytes or len(raw) > 1024:
                raise ValueError()
            value = json.loads(raw, object_pairs_hook=unique_fields)
            if (type(value) is not dict or set(value) != {"schema", "revision", "verifier"}
                    or type(value["schema"]) is not int or value["schema"] != 1):
                raise ValueError()
            return cls(value["revision"], value["verifier"])
        except (ValueError, TypeError, UnicodeError):
            raise ValueError("invalid_verifier") from None

    def to_bytes(self):
        return json.dumps({"schema": 1, "revision": self.revision, "verifier": self.encoded},
                          separators=(",", ":")).encode()

    def matches(self, password):
        if not password_valid(password):
            return False
        try:
            return HASHER.verify(self.encoded, password)
        except VerifyMismatchError:
            return False


def make_verifier(password):
    if not password_valid(password):
        raise ValueError("invalid_password")
    return Verifier(secrets.token_hex(16), HASHER.hash(password))
