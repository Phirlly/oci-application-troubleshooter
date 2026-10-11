"""Closed v1 recovery-bucket intent retained on an already acknowledged stack.

Mutable tags are a carrier, not ownership evidence. Only the trusted serialized
controller may enroll, advance or use these records; no bucket name reuse.
"""
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json

from .codec import validate_attempt
from .records import Attempt, Identity
from .recovery_bucket import (
    BucketAcknowledgement, BucketRequest, creation_digest_v1, require_text,
    validate_acknowledgement, validate_request,
)
from .stack_bootstrap import validate_initial_stack

PREFIX = "troubleshooter_bucket_v1_"


class BootstrapError(ValueError):
    def __init__(self):
        super().__init__("invalid_or_conflicting_bucket_bootstrap")


@dataclass(frozen=True, repr=False)
class BucketBootstrap:
    request: BucketRequest
    attempt: Attempt
    started: str
    phase: str = "create_uncertain"
    bucket_id: str | None = None


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _request(initial, stack_id, namespace):
    validate_initial_stack(initial)
    require_text(stack_id, r"[A-Za-z0-9_.:-]{1,256}")
    require_text(namespace, r"[A-Za-z0-9_-]{1,256}")
    seed = sha256(_json({"schema": 1, "stack": stack_id, "namespace": namespace,
                        "initial_sha256": sha256(initial.body).hexdigest()}).encode()).hexdigest()
    return BucketRequest(initial.scope, namespace, "troubleshooter-recovery-" + seed[:32], seed[:32],
                         initial.principal, sha256(("create-bucket-v1:" + seed).encode()).hexdigest()[:32])


def window(record):
    try:
        require_text(record.started, r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:00Z")
        start = datetime.fromisoformat(record.started)
        return start, start + timedelta(hours=1)
    except Exception:
        raise BootstrapError() from None


def require_dispatch_window(record, now):
    start, end = window(record)
    if not start.timestamp() <= now < end.timestamp():
        raise BootstrapError()


def _pack(initial, stack_id, record):
    if type(record) is not BucketBootstrap:
        raise BootstrapError()
    validate_request(record.request)
    validate_attempt(record.attempt)
    window(record)
    if (record.request != _request(initial, stack_id, record.request.namespace)
            or (record.attempt.source_sha, record.attempt.workflow_sha) !=
            (initial.attempt.source_sha, initial.attempt.workflow_sha)
            or type(record.phase) is not str
            or record.phase not in {"create_uncertain", "journal_initializing", "ready"}):
        raise BootstrapError()
    tags = {PREFIX + "intent": _json({"schema": 1, "state": record.phase,
        "started": record.started, "request_sha256": creation_digest_v1(record.request)}),
        PREFIX + "attempt": _json(asdict(record.attempt)), PREFIX + "namespace": record.request.namespace}
    if record.phase == "create_uncertain":
        if record.bucket_id is not None:
            raise BootstrapError()
    else:
        tags[PREFIX + "id"] = require_text(record.bucket_id, r"[A-Za-z0-9_.:-]{1,256}")
    return tags


def _limits(tags, defined):
    if type(tags) is not dict or len(tags) > 10 or type(defined) is not dict:
        raise BootstrapError()
    for mapping in (tags, defined, *defined.values()):
        if type(mapping) is not dict:
            raise BootstrapError()
        for key in mapping:
            require_text(key, r"[!-~]{1,100}")
            if "." in key:
                raise BootstrapError()
    if sum(len(values) for values in defined.values()) > 64:
        raise BootstrapError()
    for value in tags.values():
        if type(value) is not str or len(value) > 256:
            raise BootstrapError()
    # Defined values can be strings or lists of strings. They are never rewritten.
    for fields in defined.values():
        for value in fields.values():
            values = value if type(value) is list else [value]
            if any(type(item) is not str or len(item) > 256 for item in values):
                raise BootstrapError()
    # Include pinned-SDK spacing/escaping; do not assume service normalization.
    encoded = json.dumps({"freeformTags": tags, "definedTags": defined}, ensure_ascii=True, allow_nan=False)
    if len(encoded.encode("utf-8")) > 5000:
        raise BootstrapError()


def _capacity(initial, stack_id, tags, defined, record):
    _limits(tags, defined)
    if record.phase == "create_uncertain":
        future = replace(record, phase="journal_initializing", bucket_id="x" * 256)
        _limits({**tags, **_pack(initial, stack_id, future)}, defined)


def enroll(initial, stack_id, namespace, attempt, now):
    try:
        started = datetime.fromtimestamp(now, timezone.utc).replace(second=0, microsecond=0).isoformat()
        record = BucketBootstrap(_request(initial, stack_id, namespace), attempt, started.replace("+00:00", "Z"))
        _pack(initial, stack_id, record)
        return record
    except Exception:
        raise BootstrapError() from None


def read_bootstrap(initial, stack_id, tags, defined):
    try:
        _limits(tags, defined)
        saved = {k: v for k, v in tags.items() if k.startswith("troubleshooter_bucket_")}
        if not saved:
            return None
        intent = json.loads(saved[PREFIX + "intent"])
        record = BucketBootstrap(_request(initial, stack_id, saved[PREFIX + "namespace"]),
            Attempt(**json.loads(saved[PREFIX + "attempt"])), intent["started"],
            intent["state"], saved.get(PREFIX + "id"))
        if saved != _pack(initial, stack_id, record):
            raise BootstrapError()
        _capacity(initial, stack_id, tags, defined, record)
        return record
    except Exception:
        raise BootstrapError() from None


def merge_bootstrap(initial, stack_id, tags, defined, record):
    try:
        existing = read_bootstrap(initial, stack_id, tags, defined)
        packed = _pack(initial, stack_id, record)
        if existing is None:
            valid = record.phase == "create_uncertain"
        elif existing.phase == "create_uncertain":
            valid = record == replace(existing, phase="journal_initializing", bucket_id=record.bucket_id)
        else:
            valid = existing.phase == "journal_initializing" and record == replace(existing, phase="ready")
        if not valid:
            raise BootstrapError()
        merged = {**tags, **packed}
        _capacity(initial, stack_id, merged, defined, record)
        return merged
    except Exception:
        raise BootstrapError() from None


def acknowledge(record, ack):
    try:
        validate_acknowledgement(ack)
        if record.phase != "create_uncertain" or record.request != ack.request:
            raise BootstrapError()
        return replace(record, phase="journal_initializing", bucket_id=ack.identity.bucket_id)
    except Exception:
        raise BootstrapError() from None


def acknowledgement(record):
    """Only a validated, independently read initializing/ready carrier may call."""
    ack = BucketAcknowledgement(record.request,
        Identity(record.request.scope, record.bucket_id, record.request.epoch))
    validate_acknowledgement(ack)
    return ack
