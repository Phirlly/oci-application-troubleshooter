"""Public-safe frozen request commitments; never OCI ownership or dispatch authority."""
from dataclasses import asdict, dataclass, fields
from hashlib import sha256
import json
import re

from .codec import MAX_BYTES, validate_attempt, validate_scope
from .records import Attempt


class CommitmentError(ValueError):
    def __init__(self):
        super().__init__("invalid_bootstrap_commitment")


@dataclass(frozen=True)
class StackBootstrapCommitment:
    repository_id: int
    environment: str
    attempt: Attempt
    scope_sha256: str
    request_sha256: str
    schema: int = 1


def parse_commitment(payload):
    try:
        if (type(payload) is not dict or set(payload) != {f.name for f in fields(StackBootstrapCommitment)}
                or type(payload["schema"]) is not int or payload["schema"] != 1
                or type(payload["repository_id"]) is not int or not 0 < payload["repository_id"] < 2**63
                or type(payload["environment"]) is not str
                or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", payload["environment"]) is None):
            raise CommitmentError()
        for name in ("scope_sha256", "request_sha256"):
            if type(payload[name]) is not str or re.fullmatch(r"[0-9a-f]{64}", payload[name]) is None:
                raise CommitmentError()
        attempt = Attempt(**payload["attempt"])
        validate_attempt(attempt)
        return StackBootstrapCommitment(**{**payload, "attempt": attempt})
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        raise CommitmentError() from None


def commitment_payload(commitment):
    try:
        if type(commitment) is not StackBootstrapCommitment:
            raise CommitmentError()
        payload = asdict(commitment)
        parse_commitment(payload)
        return payload
    except (ValueError, TypeError, RecursionError):
        raise CommitmentError() from None


def scope_digest(scope):
    try:
        validate_scope(scope)
        return sha256(json.dumps(asdict(scope), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    except (ValueError, TypeError):
        raise CommitmentError() from None


def freeze_stack_request(scope, attempt, request_body):
    """Caller supplies exact serialized nonsecret bytes, not keys or verifiers.

    The digest binds reconstruction; it cannot reconstruct the original request.
    A future caller must retain its release, serializer and original inputs.
    """
    try:
        target = scope_digest(scope)
        validate_attempt(attempt)
        if type(request_body) is not bytes or not 0 < len(request_body) <= MAX_BYTES:
            raise CommitmentError()
        return StackBootstrapCommitment(scope.repository_id, scope.environment, attempt,
                                        target, sha256(request_body).hexdigest())
    except (ValueError, TypeError):
        raise CommitmentError() from None
