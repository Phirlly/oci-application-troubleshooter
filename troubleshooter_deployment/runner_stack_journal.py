"""Resume a runner journal without a separate bucket creation transaction.

Requires trusted serialized workflow, retained ownership history and an outer
watchdog before public exposure. Journal ready does not mean Demo ready.
"""
from contextlib import ExitStack
from dataclasses import dataclass

from .github_attempt import GitHubAttempts
from .journal import Journal, Snapshot
from .oci_owned_stack import OwnedStackObservation
from .oci_stack_bootstrap import InitialStackAcknowledgement
from .oci_stack_journal import OciStackJournalStore
from .runner_bootstrap import BootstrapPending, bootstrap_initial_stack


@dataclass(frozen=True, repr=False)
class StackJournalReady:
    anchor: InitialStackAcknowledgement | OwnedStackObservation
    snapshot: Snapshot


def bootstrap_stack_journal(*, scope, current, owner, repository, github_token, oci_config):
    reason = "initial_stack_unavailable_or_mismatched"
    try:
        anchor = bootstrap_initial_stack(scope=scope, current=current, owner=owner, repository=repository,
                                         github_token=github_token, oci_config=oci_config)
        if isinstance(anchor, BootstrapPending):
            return anchor
        reason = "stack_journal_attempt_unconfirmed"
        attempts = GitHubAttempts(owner, repository, github_token)
        if attempts.observe_attempt(scope.repository_id, current).status != "in_progress":
            return BootstrapPending(reason)
        with ExitStack() as cleanup:
            reason = "stack_journal_unavailable_or_mismatched"
            store = OciStackJournalStore(oci_config, anchor)
            cleanup.callback(store.close)
            if store.read() is None:
                reason = "original_stack_attempt_unconfirmed"
                original = anchor.request.attempt
                if ((current.source_sha, current.workflow_sha) !=
                        (original.source_sha, original.workflow_sha)):
                    return BootstrapPending("stack_journal_release_mismatch")
                if original != current and attempts.observe_attempt(scope.repository_id, original).status != "completed":
                    return BootstrapPending(reason)
                if attempts.observe_attempt(scope.repository_id, current).status != "in_progress":
                    return BootstrapPending(reason)
                reason = "stack_journal_initialization_unconfirmed"
                Journal(store, store.identity, original).initialize(store.identity)
            reason = "stack_journal_read_or_takeover_unconfirmed"
            journal = Journal(store, store.identity, current)
            snapshot = journal.load()
            record = snapshot.record
            # Creation provenance stays frozen. Only an empty open journal can
            # hand control to different code; later retries compare to that
            # persisted owner, so their own deployment history remains resumable.
            if ((current.source_sha, current.workflow_sha) !=
                    (record.owner.source_sha, record.owner.workflow_sha)
                    and (record.generations or record.operations or record.receipt is not None)):
                return BootstrapPending("stack_journal_release_mismatch")
            if snapshot.record.owner != current:
                observation = attempts.observe(snapshot.record)
                if attempts.observe_attempt(scope.repository_id, current).status != "in_progress":
                    return BootstrapPending(reason)
                updated = journal.takeover(snapshot, observation)
                snapshot = journal.load()
                if snapshot != updated:
                    return BootstrapPending(reason)
            result = StackJournalReady(anchor, snapshot)
        return result
    except Exception:
        return BootstrapPending(reason)
