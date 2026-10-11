"""Public-safe binding retained only after a validated stack creation ACK.

The caller supplies positive ACK/GET evidence. Neither a constructed value nor
a hash proves ownership independently of trusted, retained controller history.
"""
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import re

from .bootstrap_commitment import StackBootstrapCommitment, commitment_payload, parse_commitment


class StackOwnershipError(ValueError):
    def __init__(self):
        super().__init__("invalid_stack_ownership_commitment")


@dataclass(frozen=True)
class StackOwnershipCommitment:
    bootstrap: StackBootstrapCommitment
    stack_sha256: str
    schema: int = 1


def parse_ownership(payload):
    try:
        if (type(payload) is not dict or set(payload) != {"schema", "bootstrap", "stack_sha256"}
                or type(payload["schema"]) is not int or payload["schema"] != 1
                or type(payload["stack_sha256"]) is not str
                or re.fullmatch(r"[0-9a-f]{64}", payload["stack_sha256"]) is None):
            raise StackOwnershipError()
        return StackOwnershipCommitment(parse_commitment(payload["bootstrap"]), payload["stack_sha256"])
    except (ValueError, TypeError, KeyError, RecursionError):
        raise StackOwnershipError() from None


def ownership_payload(ownership):
    try:
        if type(ownership) is not StackOwnershipCommitment:
            raise StackOwnershipError()
        payload = asdict(ownership)
        parse_ownership(payload)
        return payload
    except (ValueError, TypeError, RecursionError):
        raise StackOwnershipError() from None


def freeze_stack_ownership(bootstrap, stack_id):
    """Bind an already acknowledged ID, or compare a discovery candidate."""
    try:
        if type(stack_id) is not str or re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", stack_id) is None:
            raise StackOwnershipError()
        binding = {"schema": 1, "bootstrap": commitment_payload(bootstrap), "stack_id": stack_id}
        digest = sha256(json.dumps(binding, sort_keys=True, separators=(",", ":")).encode("ascii")).hexdigest()
        return StackOwnershipCommitment(bootstrap, digest)
    except (ValueError, TypeError, RecursionError):
        raise StackOwnershipError() from None
