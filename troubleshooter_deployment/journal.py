"""Conditional journal boundary; implementations must not hide write retries."""
from dataclasses import dataclass
from typing import Protocol

from .codec import decode, encode
from .policy import PolicyError, bootstrap, claim, reserve, require_forward_transition
from .records import Attempt, Identity, Record, RunObservation


class WriteConflict(RuntimeError):
    def __init__(self):
        super().__init__("Journal revision changed; reread before continuing")


class WriteUncertain(RuntimeError):
    def __init__(self):
        super().__init__("Journal write outcome unknown; reconcile before continuing")


class ConditionalStore(Protocol):
    """Future adapter must distinguish confirmed absence from denied/failed reads.

    read returns (opaque revision, bounded bytes), or confirmed object absence.
    write(None, body) is create-if-absent; other revisions require atomic CAS.
    A lost reply raises WriteUncertain even if the write may have succeeded.
    The adapter must establish bucket identity and enforce transport bounds.
    """

    def read(self) -> tuple[str, bytes] | None: ...

    def write(self, expected: str | None, body: bytes) -> str: ...


@dataclass(frozen=True)
class Snapshot:
    record: Record
    revision: str


def _revision_valid(value):
    return type(value) is str and 0 < len(value) <= 1024 and all(32 <= ord(c) < 127 for c in value)


class Journal:
    def __init__(self, store: ConditionalStore, identity: Identity, owner: Attempt):
        self.store = store
        self.identity = identity
        self.owner = owner

    def initialize(self, creation_ack: Identity | None) -> Snapshot:
        record = bootstrap(self.identity, creation_ack, self.owner)
        revision = self.store.write(None, encode(record))
        if not _revision_valid(revision):
            raise WriteUncertain()
        return Snapshot(record, revision)

    def load(self) -> Snapshot:
        stored = self.store.read()
        if stored is None:
            raise PolicyError("journal_missing_reconcile_bucket")
        revision, body = stored
        if not _revision_valid(revision):
            raise PolicyError("invalid_journal_revision")
        record = decode(body)
        if record.identity != self.identity:
            raise PolicyError("journal_ownership_conflict")
        return Snapshot(record, revision)

    def _owned(self, snapshot):
        if snapshot.record.identity != self.identity or snapshot.record.owner != self.owner:
            raise PolicyError("journal_owner_mismatch")

    def _save(self, snapshot, updated):
        if not _revision_valid(snapshot.revision):
            raise PolicyError("invalid_journal_revision")
        if updated.identity != self.identity or updated.owner != self.owner:
            raise PolicyError("journal_owner_mismatch")
        require_forward_transition(snapshot.record, updated)
        revision = self.store.write(snapshot.revision, encode(updated))
        if not _revision_valid(revision):
            raise WriteUncertain()
        return Snapshot(updated, revision)

    def commit(self, snapshot: Snapshot, updated: Record) -> Snapshot:
        """Persist a policy-produced transition, not arbitrary caller data."""
        self._owned(snapshot)
        return self._save(snapshot, updated)

    def takeover(self, snapshot: Snapshot, observed: RunObservation | None) -> Snapshot:
        if snapshot.record.identity != self.identity:
            raise PolicyError("journal_ownership_conflict")
        return self._save(snapshot, claim(snapshot.record, self.owner, observed))

    def reserve(self, snapshot: Snapshot, generation_id: str, operation_id: str, kind: str):
        self._owned(snapshot)
        updated, decision = reserve(snapshot.record, generation_id, operation_id, kind)
        if decision != "reserved":
            return snapshot, decision
        # No catches/retries: a lost acknowledgement must return no reservation.
        return self._save(snapshot, updated), decision
