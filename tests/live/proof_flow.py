"""Explicit developer experiment ordering; uncertainty is never retried blindly."""
from datetime import datetime, timedelta, timezone

from troubleshooter_deployment.cleanup import completion, prepare_purge
from troubleshooter_deployment.codec import decode, encode
from troubleshooter_deployment.policy import bootstrap
from troubleshooter_deployment.records import Attempt, Identity
from troubleshooter_deployment.recovery_bucket import BucketAcknowledgement
from .proof_state import bucket_request, validate_state

# Synthetic local fixture provenance. This is NOT a GitHub attempt observation.
PROOF_ATTEMPT = Attempt(1, 1, "0" * 40, "0" * 40)


def acknowledged_bucket(state):
    request = bucket_request(state)
    return BucketAcknowledgement(request, Identity(request.scope, state["bucket_id"], request.epoch))


def _save(store, state, **changes):
    state.update(changes)
    store.write(state)  # A failed durable write must propagate BEFORE any dispatch.


def advance(store, state, services, *, cleanup):
    validate_state(state)
    if (state["stack_status"] in {"uncertain", "delete_uncertain", "deleted"}
            or state["bucket_status"] in {"uncertain", "delete_uncertain"}):
        return
    if state["stack_status"] == "new":
        if cleanup:
            return
        _save(store, state, stack_status="uncertain")
        identifier = services.create_stack(state)
        _save(store, state, stack_status="acknowledged", stack_id=identifier)
    services.check_stack(state)
    if state["bucket_status"] == "new" and not cleanup:
        _save(store, state, bucket_status="uncertain")
        ack = services.create_bucket(bucket_request(state))
        _save(store, state, bucket_status="acknowledged", bucket_id=ack.identity.bucket_id)
    if state["bucket_status"] == "acknowledged":
        ack = acknowledged_bucket(state)
        observed = services.check_bucket(ack)
        if state["record"] is None:
            record = prepare_purge(bootstrap(ack.identity, ack.identity, PROOF_ATTEMPT), state["removal"])
            _save(store, state, record=encode(record).decode("ascii"),
                  unknown_bucket_fields=list(observed.unknown_fields))
        record = decode(state["record"].encode())
        if state["receipt_status"] == "new":
            _save(store, state, receipt_status="uncertain")
            services.persist(state, record)
        # Always read independently, including after a lost PUT.
        receipt = services.read_receipt(state, ack)
        if receipt != record.receipt:
            return
        _save(store, state, receipt_status="confirmed")
        _save(store, state, bucket_status="delete_uncertain")
        result = services.delete_bucket(ack, record, receipt, state["delete_correlation"])
        if completion(record.receipt, receipt, result) != "complete":
            return
        _save(store, state, bucket_status="deleted")
    if state["bucket_status"] == "deleted":
        ack, record = acknowledged_bucket(state), decode(state["record"].encode())
        if services.read_receipt(state, ack) != record.receipt:
            return
        _save(store, state, receipt_status="survived")
    if cleanup:
        # No workload jobs were submitted; adapter rechecks exact ID/metadata,
        # fresh ETag and a complete empty job listing before DELETE.
        _save(store, state, stack_status="delete_uncertain")
        services.delete_stack(state)
        _save(store, state, stack_status="deleted")


def observe(store, state, services, *, processed_start=None, processed_minutes=60):
    if type(processed_minutes) is not int or not 1 <= processed_minutes <= 60:
        raise ValueError("invalid_audit_window_duration")
    validate_state(state)
    if state["bucket_id"] is None:
        return
    if processed_start is None:
        end = datetime.now(timezone.utc).replace(second=0, microsecond=0) + timedelta(minutes=1)
        start = max(datetime.fromisoformat(state["started"]), end - timedelta(minutes=processed_minutes))
    else:
        start, end = processed_start, processed_start + timedelta(minutes=processed_minutes)
    ack = acknowledged_bucket(state)
    create = services.audit(ack, "CreateBucket", state["create_correlation"], start, end)
    delete = services.audit(ack, "DeleteBucket", state["delete_correlation"], start, end)
    _save(store, state, audit_counts=[len(create.matches), len(delete.matches)],
          audit_window=[start.isoformat(), end.isoformat()])
