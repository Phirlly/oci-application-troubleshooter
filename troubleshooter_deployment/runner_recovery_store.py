"""One normal setup invocation, with durable progress and conservative recovery.

Requires trusted serialized workflow/retained history and exclusive namespace
control, as runner_bootstrap does. No CLI/workflow exposure until its trust and
outer process watchdog are implemented. Recovery-store ready is not Demo ready.
A crash between intent persistence and bucket POST remains pending.
"""
from contextlib import ExitStack
from dataclasses import dataclass, replace
from time import time

from .bucket_bootstrap import acknowledge, acknowledgement, enroll, require_dispatch_window, window
from .github_attempt import GitHubAttempts
from .journal import Journal, Snapshot, WriteConflict, WriteUncertain
from .oci_audit import OciBucketAudit
from .oci_bucket_bootstrap import OciBucketBootstrapStore
from .oci_journal import OciJournalStore
from .oci_owned_stack import OwnedStackObservation
from .oci_recovery_bucket import OciRecoveryBucket
from .oci_stack_bootstrap import InitialStackAcknowledgement
from .policy import bootstrap
from .recovery_bucket import BucketAcknowledgement
from .runner_bootstrap import BootstrapPending, bootstrap_initial_stack


@dataclass(frozen=True, repr=False)
class RecoveryStoreReady:
    anchor: InitialStackAcknowledgement | OwnedStackObservation
    bucket: BucketAcknowledgement
    snapshot: Snapshot


def _completed(attempts, scope, original, current):
    if original != current and attempts.observe_attempt(scope.repository_id, original).status != "completed":
        raise ValueError("original_attempt_not_completed")


def bootstrap_recovery_store(*, scope, current, owner, repository, github_token, oci_config):
    reason = "initial_stack_unavailable_or_mismatched"
    try:
        anchor = bootstrap_initial_stack(scope=scope, current=current, owner=owner, repository=repository,
                                         github_token=github_token, oci_config=oci_config)
        if isinstance(anchor, BootstrapPending):
            return anchor
        reason = "recovery_store_attempt_unconfirmed"
        attempts = GitHubAttempts(owner, repository, github_token)
        if (attempts.observe_attempt(scope.repository_id, current).status != "in_progress"
                or (current.source_sha, current.workflow_sha) !=
                (anchor.request.attempt.source_sha, anchor.request.attempt.workflow_sha)):
            return BootstrapPending(reason)
        with ExitStack() as cleanup:
            reason = "bucket_bootstrap_unavailable_or_mismatched"
            carrier = OciBucketBootstrapStore(oci_config, anchor)
            cleanup.callback(carrier.close)
            saved = carrier.read()
            bucket = OciRecoveryBucket(oci_config, scope)
            cleanup.callback(bucket.close)
            if saved.record is None:
                reason = "original_stack_attempt_unconfirmed"
                _completed(attempts, scope, anchor.request.attempt, current)
                reason = "bucket_intent_unconfirmed"
                state = enroll(anchor.request, anchor.stack_id, bucket.namespace(), current, time())
                saved = carrier.persist(saved, state)
                reason = "bucket_creation_uncertain"
                if attempts.observe_attempt(scope.repository_id, current).status != "in_progress":
                    return BootstrapPending(reason)
                require_dispatch_window(saved.record, time())
                # Only this new enrollment in this invocation permits first dispatch.
                ack = bucket.create(saved.record.request)
                bucket.read(ack)
                reason = "bucket_identity_unconfirmed"
                saved = carrier.persist(saved, acknowledge(saved.record, ack))
            elif saved.record.phase == "create_uncertain":
                reason = "original_bucket_attempt_unconfirmed"
                if saved.record.attempt == current:
                    return BootstrapPending(reason)
                _completed(attempts, scope, saved.record.attempt, current)
                reason = "bucket_creation_unreconciled"
                audit = OciBucketAudit(oci_config, scope)
                cleanup.callback(audit.close)
                evidence = audit.reconcile_creation(saved.record.request, *window(saved.record))
                if evidence is None:
                    return BootstrapPending(reason)
                ack = bucket.reconcile_creation(evidence)
                reason = "bucket_identity_unconfirmed"
                saved = carrier.persist(saved, acknowledge(saved.record, ack))
            state = saved.record
            ack = acknowledgement(state)
            reason = "recovery_bucket_unavailable_or_mismatched"
            bucket.read(ack)
            store = OciJournalStore(oci_config, ack.identity, ack.request.namespace, ack.request.name)
            cleanup.callback(store.close)
            if state.phase == "journal_initializing":
                reason = "original_bucket_attempt_unconfirmed"
                _completed(attempts, scope, state.attempt, current)
                reason = "journal_initialization_unconfirmed"
                if carrier.read() != saved:
                    return BootstrapPending(reason)
                initial = Journal(store, ack.identity, state.attempt)
                try:
                    initial.initialize(ack.identity)
                except (WriteConflict, WriteUncertain):
                    pass
                if initial.load().record != bootstrap(ack.identity, ack.identity, state.attempt):
                    return BootstrapPending(reason)
                reason = "journal_ready_marker_unconfirmed"
                carrier.persist(saved, replace(state, phase="ready"))
            reason = "journal_read_or_takeover_unconfirmed"
            journal = Journal(store, ack.identity, current)
            snapshot = journal.load()
            if snapshot.record.owner != current:
                updated = journal.takeover(snapshot, attempts.observe(snapshot.record))
                snapshot = journal.load()
                if snapshot != updated:
                    return BootstrapPending(reason)
            result = RecoveryStoreReady(anchor, ack, snapshot)
        return result
    except Exception:
        return BootstrapPending(reason)
