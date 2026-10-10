"""Closed, bounded journal format. Reject corruption instead of repairing it."""
from dataclasses import asdict, fields
from datetime import datetime
import json
import re

from .records import Attempt, Generation, Identity, Operation, Receipt, Record, Resource, Scope

MAX_BYTES = 262144
MAX_ITEMS = 256
MAX_RECEIPT_BYTES = 12000
RESOURCE_STATES = {
    "host": {"present", "terminated"},
    "stack": {"present", "destroyed"},
    "secret": {"present", "scheduled", "deleted"},
    "key": {"present", "scheduled", "deleted"},
    "vault": {"present", "scheduled", "deleted"},
}


class RecordError(ValueError):
    def __init__(self):
        super().__init__("Invalid or unsupported deployment record")


def _require(condition):
    if not condition:
        raise RecordError()


def _fields(value, record_type):
    _require(type(value) is dict and set(value) == {f.name for f in fields(record_type)})
    return value


def _items(value):
    _require(type(value) is list and len(value) <= MAX_ITEMS)
    return value


def _text(value, pattern=r"[A-Za-z0-9_.:-]{1,256}"):
    _require(type(value) is str and re.fullmatch(pattern, value) is not None)
    return value


def _positive(value):
    _require(type(value) is int and 0 < value < 2**63)
    return value


def _attempt(value):
    data = _fields(value, Attempt)
    return Attempt(_positive(data["run_id"]), _positive(data["number"]),
                   _text(data["workflow_sha"], r"[0-9a-f]{40}"),
                   _text(data["source_sha"], r"[0-9a-f]{40}"))


def _identity(value):
    data = _fields(value, Identity)
    scope = _fields(data["scope"], Scope)
    return Identity(Scope(_positive(scope["repository_id"]),
                          _text(scope["environment"], r"[a-z0-9][a-z0-9_-]{0,62}"),
                          _text(scope["tenancy_id"]), _text(scope["compartment_id"]),
                          _text(scope["region"], r"[a-z0-9-]{1,64}")),
                    _text(data["bucket_id"]), _text(data["epoch"], r"[0-9a-f]{32}"))


def _resource(value):
    data = _fields(value, Resource)
    resource = Resource(**data)
    _text(resource.resource_id)
    _require(type(resource.kind) is str and resource.kind in RESOURCE_STATES)
    _require(type(resource.state) is str and resource.state in RESOURCE_STATES[resource.kind])
    if resource.state == "scheduled":
        _text(resource.deletion_due, r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
        datetime.fromisoformat(resource.deletion_due)
    else:
        _require(resource.deletion_due is None)
    return resource


def validate_identity(identity):
    try:
        _require(type(identity) is Identity and _identity(asdict(identity)) == identity)
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        raise RecordError() from None


def _generation(value):
    data = dict(_fields(value, Generation))
    data["resources"] = tuple(_resource(item) for item in _items(data["resources"]))
    generation = Generation(**data)
    _text(generation.generation_id, r"[0-9a-f]{32}")
    _text(generation.release_sha, r"[0-9a-f]{40}")
    _require(type(generation.phase) is str and generation.phase in {"provisioning", "removing", "workload_removed"})
    _require(type(generation.external_cleanup_complete) is bool)
    if generation.phase == "provisioning":
        _require(generation.removal_id is None)
    else:
        _text(generation.removal_id, r"[0-9a-f]{32}")
    if generation.phase != "workload_removed":
        _require(not generation.external_cleanup_complete)
    for kind in ("host", "stack", "key", "vault"):
        _require(sum(r.kind == kind for r in generation.resources) <= 1)
    for resource in generation.resources:
        if generation.phase == "workload_removed" and resource.kind in {"host", "stack"}:
            _require(resource.state in {"terminated", "destroyed"})
        if resource.kind in {"secret", "key", "vault"} and resource.state != "present":
            _require(generation.phase == "workload_removed" and generation.external_cleanup_complete)
    return generation


def _operation(value):
    data = dict(_fields(value, Operation))
    data["attempt"] = _attempt(data["attempt"])
    operation = Operation(**data)
    for identifier in (operation.operation_id, operation.generation_id):
        _text(identifier, r"[0-9a-f]{32}")
    _text(operation.release_sha, r"[0-9a-f]{40}")
    _require(type(operation.kind) is str and operation.kind in {"provision", "cleanup"})
    _require(type(operation.state) is str and operation.state in {"uncertain", "resolved"})
    return operation


def _receipt(value):
    data = _fields(value, Receipt)
    return Receipt(_identity(data["identity"]), _attempt(data["attempt"]),
                   _text(data["removal_id"], r"[0-9a-f]{32}"),
                   tuple(_text(item, r"[0-9a-f]{32}") for item in _items(data["generation_ids"])))


def _unique(values):
    _require(len(values) == len(set(values)))


def _relations(record):
    generations = {g.generation_id: g for g in record.generations}
    _unique([g.generation_id for g in record.generations])
    _unique([r.resource_id for g in record.generations for r in g.resources])
    _unique([op.operation_id for op in record.operations])
    _require(sum(g.phase != "workload_removed" for g in record.generations) <= 1)
    pending = []
    for operation in record.operations:
        _require(operation.generation_id in generations)
        generation = generations[operation.generation_id]
        _require(operation.release_sha == generation.release_sha)
        if operation.state == "uncertain":
            pending.append(operation.generation_id)
            _require(not (generation.phase == "workload_removed" and operation.kind == "provision"))
    _unique(pending)
    if record.receipt is not None:
        _require(record.receipt.identity == record.identity and not pending)
        _require(record.receipt.generation_ids == tuple(generations))
        for generation in record.generations:
            _require(generation.phase == "workload_removed" and generation.external_cleanup_complete)
            _require(all(r.state in {"terminated", "destroyed", "deleted"} for r in generation.resources))


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def decode(body: bytes) -> Record:
    try:
        _require(type(body) is bytes and len(body) <= MAX_BYTES)
        data = _fields(json.loads(body, object_pairs_hook=_pairs), Record)
        _require(type(data["schema"]) is int and data["schema"] == 1)
        record = Record(_identity(data["identity"]), _attempt(data["owner"]),
                        tuple(_generation(item) for item in _items(data["generations"])),
                        tuple(_operation(item) for item in _items(data["operations"])),
                        None if data["receipt"] is None else _receipt(data["receipt"]))
        _relations(record)
        return record
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        raise RecordError() from None


def encode(record: Record) -> bytes:
    try:
        _require(type(record) is Record)
        body = json.dumps(asdict(record), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        _require(decode(body) == record)
        return body
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise RecordError() from None


def _receipt_bytes(receipt):
    return json.dumps({"schema": 1, "receipt": asdict(receipt)},
                      sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")


def decode_receipt(body: bytes) -> Receipt:
    """Standalone canonical receipt; its schema is independent of the journal."""
    try:
        _require(type(body) is bytes and len(body) <= MAX_RECEIPT_BYTES)
        data = json.loads(body, object_pairs_hook=_pairs)
        _require(type(data) is dict and set(data) == {"schema", "receipt"})
        _require(type(data["schema"]) is int and data["schema"] == 1)
        receipt = _receipt(data["receipt"])
        _unique(receipt.generation_ids)
        _require(_receipt_bytes(receipt) == body)
        return receipt
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        raise RecordError() from None


def encode_receipt(receipt: Receipt) -> bytes:
    try:
        _require(type(receipt) is Receipt)
        body = _receipt_bytes(receipt)
        _require(decode_receipt(body) == receipt)
        return body
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise RecordError() from None
