"""Conservative domain transitions; no transition authorizes an SDK request."""
from dataclasses import replace

from .codec import encode
from .records import Attempt, Generation, Identity, Operation, OperationResult, Record, RunObservation


class PolicyError(RuntimeError):
    def __init__(self, code="reconciliation_required"):
        self.code = code
        super().__init__(code)


def checked(record: Record) -> Record:
    encode(record)
    return record


def generation_by_id(record, generation_id):
    checked(record)
    for generation in record.generations:
        if generation.generation_id == generation_id:
            return generation
    raise PolicyError("unknown_generation")


def replace_generation(record, generation):
    return checked(replace(record, generations=tuple(
        generation if existing.generation_id == generation.generation_id else existing
        for existing in record.generations
    )))


def pending(record, generation_id):
    return any(op.generation_id == generation_id and op.state == "uncertain" for op in record.operations)


def require_open(record):
    checked(record)
    if record.receipt is not None:
        raise PolicyError("purge_in_progress")


def bootstrap(expected: Identity, creation_ack: Identity | None, owner: Attempt) -> Record:
    """Only an exact, validated create acknowledgement can initialize ownership.

    A discovery/name/tag match cannot substitute. Lost create responses require
    the later verified bootstrap adapter; this policy refuses to guess.
    """
    if creation_ack is None or creation_ack != expected:
        raise PolicyError("ownership_unconfirmed")
    return checked(Record(expected, owner))


def begin_generation(record: Record, generation_id: str, release_sha: str) -> Record:
    require_open(record)
    for generation in record.generations:
        if generation.generation_id == generation_id:
            if generation.phase == "provisioning" and generation.release_sha == release_sha:
                return record
            raise PolicyError("generation_conflict")
    if any(g.phase != "workload_removed" for g in record.generations):
        raise PolicyError("workload_already_active")
    return checked(replace(record, generations=record.generations + (Generation(generation_id, release_sha),)))


def claim(record: Record, owner: Attempt, observed: RunObservation | None) -> Record:
    checked(record)
    if record.owner == owner:
        return record
    if (type(observed) is not RunObservation
            or any(type(value) is not int for value in (observed.repository_id, observed.run_id, observed.number))
            or (observed.repository_id, observed.run_id, observed.number, observed.head_sha, observed.status)
            != (record.identity.scope.repository_id, record.owner.run_id, record.owner.number,
                record.owner.source_sha, "completed")):
        raise PolicyError("prior_attempt_not_confirmed_terminal")
    # Uncertain operations retain their original owner and cannot be dispatched.
    return checked(replace(record, owner=owner))


def reserve(record: Record, generation_id: str, operation_id: str, kind: str):
    """Plan one domain reservation. Persist before returning it to a caller.

    An acknowledged reservation is NOT a frozen API request or SDK permission.
    Only Journal.reserve returns an acknowledged reservation. Every persisted
    operation starts uncertain, including a crash before any cloud dispatch.
    """
    require_open(record)
    generation = generation_by_id(record, generation_id)
    for operation in record.operations:
        if operation.operation_id == operation_id:
            if (operation.generation_id, operation.kind, operation.release_sha) != (generation_id, kind, generation.release_sha):
                raise PolicyError("operation_conflict")
            return record, "reconcile" if operation.state == "uncertain" else "resolved"
    if kind not in {"provision", "cleanup"}:
        raise PolicyError("invalid_operation")
    if pending(record, generation_id):
        raise PolicyError("operation_unresolved")
    if (kind == "provision") != (generation.phase == "provisioning"):
        raise PolicyError("operation_phase_conflict")
    operation = Operation(operation_id, generation_id, kind, record.owner, generation.release_sha)
    return checked(replace(record, operations=record.operations + (operation,))), "reserved"


def resolve_operation(record: Record, result: OperationResult) -> Record:
    require_open(record)
    operation = next((op for op in record.operations if op.operation_id == result.operation_id), None)
    if operation is None or operation.attempt != result.attempt:
        raise PolicyError("operation_observation_mismatch")
    if result.outcome not in {"succeeded", "failed", "running", "unknown"}:
        raise PolicyError("invalid_operation_observation")
    if result.outcome in {"running", "unknown"} or result.inventory_reconciled is not True:
        return record
    generation = generation_by_id(record, operation.generation_id)
    if type(result.resources) is not tuple or (operation.kind == "cleanup" and result.resources):
        raise PolicyError("invalid_resource_inventory")
    resources = list(generation.resources)
    for resource in result.resources:
        if resource.state != "present":
            raise PolicyError("invalid_resource_inventory")
        existing = next((r for r in resources if r.resource_id == resource.resource_id), None)
        if existing is not None:
            if existing != resource:
                raise PolicyError("resource_inventory_conflict")
        elif operation.state == "resolved":
            raise PolicyError("resolved_inventory_changed")
        else:
            resources.append(resource)
    updated = replace(record, operations=tuple(
        replace(op, state="resolved") if op == operation else op for op in record.operations
    ))
    return replace_generation(updated, replace(generation, resources=tuple(resources)))


def begin_removal(record: Record, generation_id: str, removal_id: str) -> Record:
    require_open(record)
    generation = generation_by_id(record, generation_id)
    if generation.removal_id is not None:
        if generation.removal_id != removal_id:
            raise PolicyError("removal_scope_conflict")
        return record
    return replace_generation(record, replace(generation, phase="removing", removal_id=removal_id))


def require_forward_transition(before: Record, after: Record):
    """A valid serialized record must also preserve already-established facts."""
    checked(before)
    checked(after)
    if before.identity != after.identity or (before.receipt is not None and before.receipt != after.receipt):
        raise PolicyError("journal_history_conflict")
    old_generations = {g.generation_id: g for g in before.generations}
    new_generations = {g.generation_id: g for g in after.generations}
    if not old_generations.keys() <= new_generations.keys():
        raise PolicyError("journal_history_conflict")
    phases = {"provisioning": 0, "removing": 1, "workload_removed": 2}
    for identifier, current in new_generations.items():
        previous = old_generations.get(identifier)
        if previous is None:
            if current.phase != "provisioning" or current.resources:
                raise PolicyError("invalid_new_generation")
            continue
        if (previous.release_sha != current.release_sha
                or not 0 <= phases[current.phase] - phases[previous.phase] <= 1
                or (previous.removal_id is not None and previous.removal_id != current.removal_id)
                or (previous.external_cleanup_complete and not current.external_cleanup_complete)):
            raise PolicyError("journal_history_conflict")
        resources = {r.resource_id: r for r in current.resources}
        for resource in previous.resources:
            updated = resources.get(resource.resource_id)
            if updated is None or updated.kind != resource.kind:
                raise PolicyError("journal_history_conflict")
            if ((resource.state != "present" and updated.state == "present")
                    or (resource.state in {"terminated", "destroyed", "deleted"} and updated.state != resource.state)):
                raise PolicyError("journal_history_conflict")
    operations = {op.operation_id: op for op in after.operations}
    for previous in before.operations:
        current = operations.get(previous.operation_id)
        if (current is None or replace(current, state=previous.state) != previous
                or (previous.state == "resolved" and current.state != "resolved")):
            raise PolicyError("journal_history_conflict")
    if before.receipt is None and after.receipt is not None and after.receipt.attempt != after.owner:
        raise PolicyError("receipt_owner_mismatch")
