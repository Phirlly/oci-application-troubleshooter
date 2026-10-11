"""Closed, bounded schema2 journal carrier; never a source of ownership."""
from hashlib import sha256
import json

from .codec import decode, encode
from .records import StackIdentity

PREFIX = "troubleshooter_journal_v2_"
MAX_JOURNAL_BYTES = 131072
CHUNK_BYTES = 6000
CHUNKS = 22
KEYS = {PREFIX + str(i) for i in range(CHUNKS)} | {PREFIX + "manifest"}


class StackJournalVariablesError(ValueError):
    def __init__(self):
        super().__init__("invalid_or_oversized_stack_journal")


def _validate(variables):
    if (type(variables) is not dict or len(variables) > 250
            or any(type(k) is not str or not k or type(v) is not str
                   or len(k.encode("utf-8")) + len(v.encode("utf-8")) > 8192
                   for k, v in variables.items())):
        raise StackJournalVariablesError()


def _pack(body):
    if type(body) is not bytes or len(body) > MAX_JOURNAL_BYTES:
        raise StackJournalVariablesError()
    record = decode(body)
    if type(record.identity) is not StackIdentity or encode(record) != body:
        raise StackJournalVariablesError()
    text = body.decode("ascii")
    manifest = json.dumps({"schema": 2, "bytes": len(body), "sha256": sha256(body).hexdigest()},
                          sort_keys=True, separators=(",", ":"))
    return {**{PREFIX + str(i): text[i * CHUNK_BYTES:(i + 1) * CHUNK_BYTES] for i in range(CHUNKS)},
            PREFIX + "manifest": manifest}


def read_journal(variables):
    try:
        _validate(variables)
        present = {k: v for k, v in variables.items() if k.startswith("troubleshooter_journal_")}
        if not present:
            return None
        if set(present) != KEYS:
            raise StackJournalVariablesError()
        body = "".join(present[PREFIX + str(i)] for i in range(CHUNKS)).encode("ascii")
        if _pack(body) != present:
            raise StackJournalVariablesError()
        return body
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise StackJournalVariablesError() from None


def merge_journal(variables, body):
    try:
        read_journal(variables)  # Partial/corrupt previous state is never repaired.
        merged = {**variables, **_pack(body)}
        _validate(merged)
        return merged
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise StackJournalVariablesError() from None
