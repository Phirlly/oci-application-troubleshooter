"""Initial stack coordination for a trusted serialized GitHub workflow.

Requires retained control history, an exclusive deployment namespace and a
trusted current attempt/workflow SHA supplied by the eventual workflow caller.
Listing cannot prove universal absence or attest workflow trust. This component
has no workflow/CLI exposure; an outer process watchdog is required before live
use. It neither resumes workload stacks nor resolves expired bootstrap tokens.
"""
from contextlib import ExitStack
from dataclasses import dataclass

from .bootstrap_commitment import freeze_stack_request
from .github_attempt import GitHubAttempts
from .github_bootstrap import GitHubBootstrap
from .oci_stack_bootstrap import OciStackBootstrap
from .stack_bootstrap import build_initial_stack, require_retry_window


@dataclass(frozen=True)
class BootstrapPending:
    reason: str


def bootstrap_initial_stack(*, scope, current, owner, repository, github_token, oci_config):
    """Return independently owned initial stack or a fixed, sanitized Pending.

    Only the invocation that publishes and independently reads a new checkpoint
    can first dispatch. A later attempt must retain the original bytes/token and
    observe the original attempt completed. Current-attempt redispatch is refused.
    No automatic replacement, name/tag adoption or historical winner selection.
    """
    reason = "initial_request_mismatch"
    try:
        # Validate current inputs before any control metadata mutation.
        request = build_initial_stack(scope, current, oci_config["user"])
        reason = "github_evidence_unavailable"
        attempts = GitHubAttempts(owner, repository, github_token)
        checkpoints = GitHubBootstrap(owner, repository, github_token)
        if attempts.observe_attempt(scope.repository_id, current).status != "in_progress":
            return BootstrapPending("current_attempt_not_running")
        history = checkpoints.discover(scope)
        if history:
            original = history[0].commitment
            if any(saved.commitment != original for saved in history):
                return BootstrapPending("competing_bootstrap_commitments")
            reason = "initial_request_mismatch"
            request = build_initial_stack(scope, original.attempt, oci_config["user"])
            if (freeze_stack_request(scope, original.attempt, request.body) != original
                    or (current.source_sha, current.workflow_sha) !=
                    (original.attempt.source_sha, original.attempt.workflow_sha)):
                return BootstrapPending(reason)
            if (current.run_id, current.number) == (original.attempt.run_id, original.attempt.number):
                return BootstrapPending("original_attempt_not_completed")
            reason = "github_evidence_unavailable"
            if attempts.observe_attempt(scope.repository_id, original.attempt).status != "completed":
                return BootstrapPending("original_attempt_not_completed")
            reason = "initial_retry_window_closed"
            for saved in history:
                require_retry_window(saved.created_at)
            created_at = min(saved.created_at for saved in history)
        reason = "initial_stack_unavailable_or_mismatched"
        with ExitStack() as cleanup:
            stack = OciStackBootstrap(oci_config, request)
            cleanup.callback(stack.close)
            if not history:
                stack.check_name_available()
                reason = "github_checkpoint_unconfirmed"
                saved = checkpoints.publish(freeze_stack_request(scope, current, request.body))
                created_at = saved.created_at
                reason = "initial_retry_window_closed"
                require_retry_window(created_at)
            reason = "initial_stack_unavailable_or_mismatched"
            result = stack.create(created_at)
        return result
    except Exception:
        return BootstrapPending(reason)
