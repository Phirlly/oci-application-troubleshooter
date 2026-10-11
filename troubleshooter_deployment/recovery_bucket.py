"""Frozen bucket requests and trusted direct or reconciled creation evidence."""
from base64 import b64encode
from dataclasses import dataclass
from hashlib import sha256
import json
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
    """Validated direct CREATE or exact positive Audit plus independent GETs.

    Reconciliation requires exclusive control and no name deletion/reuse.
    Constructing this value does not establish ownership.
    """
    request: BucketRequest
    identity: Identity


@dataclass(frozen=True)
class BucketObservation:
    revision: str
    unknown_fields: tuple[str, ...]


@dataclass(frozen=True, repr=False)
class BucketCreationEvidence:
    """Trusted adapter evidence of exact request success, not a bucket identity."""
    request: BucketRequest
    content_sha256: str


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


def creation_body_v1(request):
    """Stable wire bytes, matching the original pinned SDK model serialization.

    Keep order and whitespace while any v1 intent may need reconciliation.
    Namespace/principal/correlation are bound separately by the Audit matcher.
    """
    validate_request(request)
    return json.dumps({
        "name": request.name, "compartmentId": request.scope.compartment_id, "metadata": metadata(request),
        "publicAccessType": "NoPublicAccess", "storageTier": "Standard", "objectEventsEnabled": False,
        "versioning": "Disabled", "autoTiering": "Disabled", "bucketScope": "NAMESPACE",
    }, ensure_ascii=True, allow_nan=False).encode("ascii")


def creation_digest_v1(request):
    return b64encode(sha256(creation_body_v1(request)).digest()).decode("ascii")


def validate_creation_evidence(evidence):
    if (type(evidence) is not BucketCreationEvidence or type(evidence.content_sha256) is not str
            or evidence.content_sha256 != creation_digest_v1(evidence.request)):
        raise ValueError("invalid_bucket_creation_evidence")
