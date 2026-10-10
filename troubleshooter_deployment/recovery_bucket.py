"""Frozen bucket intent and received creation evidence, never name-based adoption."""
from dataclasses import dataclass
import re

from .codec import validate_identity, validate_scope
from .records import Identity, Scope


@dataclass(frozen=True)
class BucketRequest:
    scope: Scope
    namespace: str
    name: str
    epoch: str
    principal: str
    correlation: str


@dataclass(frozen=True)
class BucketAcknowledgement:
    """Only a validated successful CREATE may establish this observation."""
    request: BucketRequest
    identity: Identity


@dataclass(frozen=True)
class BucketObservation:
    revision: str
    unknown_fields: tuple[str, ...]


def require_text(value, pattern):
    if type(value) is not str or re.fullmatch(pattern, value) is None:
        raise ValueError("invalid_bucket_intent")
    return value


def validate_request(request):
    if type(request) is not BucketRequest:
        raise ValueError("invalid_bucket_intent")
    validate_scope(request.scope)
    for value in (request.namespace, request.name):
        require_text(value, r"[A-Za-z0-9_-]{1,256}")
    for value in (request.epoch, request.correlation):
        require_text(value, r"[0-9a-f]{32}")
    require_text(request.principal, r"[A-Za-z0-9_.:-]{1,256}")


def validate_acknowledgement(ack):
    if type(ack) is not BucketAcknowledgement:
        raise ValueError("invalid_bucket_acknowledgement")
    validate_request(ack.request)
    validate_identity(ack.identity)
    if ack.identity.scope != ack.request.scope or ack.identity.epoch != ack.request.epoch:
        raise ValueError("invalid_bucket_acknowledgement")


def metadata(request):
    validate_request(request)
    return {"deployment_epoch": request.epoch, "deployment_repository": str(request.scope.repository_id),
            "deployment_environment": request.scope.environment,
            "deployment_tenancy": request.scope.tenancy_id}
