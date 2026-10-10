"""Generation-scoped cleanup ordering and conservative finalization decisions."""
from dataclasses import replace

from .codec import RESOURCE_STATES
from .policy import PolicyError, checked, generation_by_id, pending, replace_generation, require_open
from .records import DeleteAcknowledgement, Receipt, Record


def next_action(record: Record, generation_id: str) -> str:
    generation = generation_by_id(record, generation_id)
    if generation.phase == "provisioning":
        return "begin_removal"
    if pending(record, generation_id):
        return "reconcile"
    if any(r.kind == "host" and r.state != "terminated" for r in generation.resources):
        return "fence_workload"
    if generation.phase != "workload_removed":
        return "destroy_workload"
    if not generation.external_cleanup_complete:
        return "clean_external"
    provider = [r for r in generation.resources if r.kind in {"secret", "key", "vault"}]
    if any(r.kind == "secret" and r.state == "present" for r in provider):
        return "schedule_secrets"
    if any(r.state == "present" for r in provider):
        return "schedule_keys_and_vault"
    if any(r.state == "scheduled" for r in provider):
        return "provider_deletion_pending"
    return "ready_for_receipt"


def observe_resource(record: Record, generation_id: str, resource_id: str,
                     state: str, deletion_due: str | None = None) -> Record:
    """Accept positive, exact-ID observations only; 404 is not a deletion event."""
    require_open(record)
    generation = generation_by_id(record, generation_id)
    if generation.phase == "provisioning":
        raise PolicyError("removal_not_started")
    resource = next((r for r in generation.resources if r.resource_id == resource_id), None)
    if resource is None or state not in RESOURCE_STATES[resource.kind] or state == "present":
        raise PolicyError("invalid_cleanup_observation")
    if resource.state in {"terminated", "destroyed", "deleted"} and state != resource.state:
        raise PolicyError("cleanup_cannot_regress")
    if resource.kind == "stack" and any(r.kind == "host" and r.state != "terminated" for r in generation.resources):
        raise PolicyError("workload_not_fenced")
    if resource.kind in {"secret", "key", "vault"}:
        if generation.phase != "workload_removed" or not generation.external_cleanup_complete:
            raise PolicyError("credentials_still_needed")
        if resource.kind in {"key", "vault"} and state == "scheduled":
            if any(r.kind == "secret" and r.state == "present" for r in generation.resources):
                raise PolicyError("schedule_secrets_first")
    observed = replace(resource, state=state, deletion_due=deletion_due)
    return replace_generation(record, replace(generation, resources=tuple(
        observed if r.resource_id == resource_id else r for r in generation.resources
    )))


def mark_workload_removed(record: Record, generation_id: str) -> Record:
    require_open(record)
    generation = generation_by_id(record, generation_id)
    if generation.phase == "provisioning" or pending(record, generation_id):
        raise PolicyError("workload_removal_unconfirmed")
    if any(r.kind in {"host", "stack"} and r.state not in {"terminated", "destroyed"} for r in generation.resources):
        raise PolicyError("workload_removal_unconfirmed")
    # Empty host/stack inventory is safe only after all creation intents have
    # been reconciled. The future adapter must prove complete owned inventory.
    return replace_generation(record, replace(generation, phase="workload_removed"))


def confirm_external_cleanup(record: Record, generation_id: str) -> Record:
    require_open(record)
    generation = generation_by_id(record, generation_id)
    if generation.phase != "workload_removed" or pending(record, generation_id):
        raise PolicyError("workload_removal_unconfirmed")
    return replace_generation(record, replace(generation, external_cleanup_complete=True))


def prepare_purge(record: Record, removal_id: str) -> Record:
    checked(record)
    if record.receipt is not None:
        if record.receipt.removal_id != removal_id:
            raise PolicyError("purge_scope_conflict")
        return record
    if any(next_action(record, g.generation_id) != "ready_for_receipt" for g in record.generations):
        raise PolicyError("cleanup_incomplete")
    receipt = Receipt(record.identity, record.owner, removal_id,
                      tuple(g.generation_id for g in record.generations))
    # Persist this transition conditionally BEFORE transferring the receipt to
    # an independent durable store. It closes this epoch to new deployments.
    return checked(replace(record, receipt=receipt))


def may_purge(record: Record, durable_readback: Receipt | None) -> bool:
    """Caller supplies independently retrieved, validated receipt content.

    A local proposed receipt, an unacknowledged write or this bool alone is not
    deletion authority. The real adapter must also recheck bucket ID/epoch and
    conditional revision immediately before deletion.
    """
    checked(record)
    return record.receipt is not None and durable_readback == record.receipt


def completion(receipt: Receipt, durable_readback: Receipt | None,
               acknowledgement: DeleteAcknowledgement | None) -> str:
    """Require a positive direct or strictly reconciled deletion response.

    The caller must already hold the independently durable receipt. A lost
    reply, subsequent404 or ordinary Audit observation alone is insufficient.
    """
    if (durable_readback == receipt and acknowledgement is not None and acknowledgement.identity == receipt.identity
            and acknowledgement.removal_id == receipt.removal_id):
        return "complete"
    return "cleanup_incomplete"
