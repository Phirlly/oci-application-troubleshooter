"""Immutable nonsecret domain records and normalized adapter observations.

Observations must come from trusted, independently validated adapters. Constructing
one of these values is not itself proof of cloud ownership or operation success.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Scope:
    repository_id: int
    environment: str
    tenancy_id: str
    compartment_id: str
    region: str


@dataclass(frozen=True)
class Attempt:
    """source_sha is the trigger commit; workflow_sha is persisted provenance."""

    run_id: int
    number: int
    workflow_sha: str
    source_sha: str


@dataclass(frozen=True)
class RunObservation:
    """Fields actually returned by GitHub's specific-attempt REST endpoint."""

    repository_id: int
    run_id: int
    number: int
    head_sha: str
    status: str


@dataclass(frozen=True)
class Identity:
    scope: Scope
    bucket_id: str
    epoch: str


@dataclass(frozen=True)
class Resource:
    resource_id: str
    kind: str
    state: str = "present"
    deletion_due: str | None = None


@dataclass(frozen=True)
class Generation:
    generation_id: str
    release_sha: str
    phase: str = "provisioning"
    removal_id: str | None = None
    external_cleanup_complete: bool = False
    resources: tuple[Resource, ...] = ()


@dataclass(frozen=True)
class Operation:
    operation_id: str
    generation_id: str
    kind: str
    attempt: Attempt
    release_sha: str
    state: str = "uncertain"


@dataclass(frozen=True)
class Receipt:
    identity: Identity
    attempt: Attempt
    removal_id: str
    generation_ids: tuple[str, ...]


@dataclass(frozen=True)
class Record:
    identity: Identity
    owner: Attempt
    generations: tuple[Generation, ...] = ()
    operations: tuple[Operation, ...] = ()
    receipt: Receipt | None = None
    schema: int = 1


@dataclass(frozen=True)
class OperationResult:
    operation_id: str
    attempt: Attempt
    outcome: str
    inventory_reconciled: bool
    resources: tuple[Resource, ...]


@dataclass(frozen=True)
class DeleteAcknowledgement:
    """Received successful deletion response, bound to the exact purge request."""

    identity: Identity
    removal_id: str
