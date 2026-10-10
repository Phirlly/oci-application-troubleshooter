"""Frozen conditional deletion bound to an independently retained receipt."""
from dataclasses import asdict, dataclass
from hashlib import sha256
import json

from .codec import encode_receipt
from .oci_transport import strong_etag
from .receipt_variables import read_receipt
from .records import Identity, Scope
from .recovery_bucket import (
    BucketAcknowledgement, BucketRequest, require_text, validate_acknowledgement,
)

PREFIX = "troubleshooter_bucket_delete_"
KEY = PREFIX + "intent"
MAX_INTENT_BYTES = 6000


class DeletionIntentError(ValueError):
    def __init__(self):
        super().__init__("invalid_or_conflicting_bucket_deletion_intent")


@dataclass(frozen=True)
class BucketDeleteIntent:
    ack: BucketAcknowledgement
    removal_id: str
    receipt_sha256: str
    correlation: str
    if_match: str


def encode_intent(intent, receipt):
    try:
        if type(intent) is not BucketDeleteIntent:
            raise DeletionIntentError()
        digest = sha256(encode_receipt(receipt)).hexdigest()
        validate_acknowledgement(intent.ack)
        require_text(intent.correlation, r"[0-9a-f]{32}")
        strong_etag(intent.if_match)
        if (intent.ack.identity != receipt.identity or intent.removal_id != receipt.removal_id
                or intent.receipt_sha256 != digest or intent.correlation == intent.ack.request.correlation):
            raise DeletionIntentError()
        body = json.dumps({"schema": 1, **asdict(intent)}, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("ascii")
        if len(body) > MAX_INTENT_BYTES:
            raise DeletionIntentError()
        return body
    except Exception:
        raise DeletionIntentError() from None


def decode_intent(body, receipt):
    try:
        if type(body) is not bytes or len(body) > MAX_INTENT_BYTES:
            raise DeletionIntentError()
        data = json.loads(body)
        if type(data) is not dict or type(data.get("schema")) is not int or data.pop("schema") != 1:
            raise DeletionIntentError()
        ack = data.pop("ack")
        request, identity = ack["request"], ack["identity"]
        request["scope"], identity["scope"] = Scope(**request["scope"]), Scope(**identity["scope"])
        ack["request"], ack["identity"] = BucketRequest(**request), Identity(**identity)
        intent = BucketDeleteIntent(ack=BucketAcknowledgement(**ack), **data)
        # Exact bytes also reject duplicate keys, extra whitespace and alternate encodings.
        if encode_intent(intent, receipt) != body:
            raise DeletionIntentError()
        return intent
    except Exception:
        raise DeletionIntentError() from None


def read_deletion(variables):
    try:
        receipt = read_receipt(variables)  # Also validates total RM capacity.
        present = {k: v for k, v in variables.items() if k.startswith(PREFIX)}
        if not present:
            return None
        if set(present) != {KEY} or receipt is None:
            raise DeletionIntentError()
        return decode_intent(present[KEY].encode("ascii"), receipt)
    except Exception:
        raise DeletionIntentError() from None


def merge_deletion(variables, intent):
    """Require the frozen receipt already present; never overwrite another intent."""
    try:
        existing = read_deletion(variables)
        if existing is not None and existing != intent:
            raise DeletionIntentError()
        body = encode_intent(intent, read_receipt(variables))
        merged = dict(variables, **{KEY: body.decode("ascii")})
        read_deletion(merged)
        return merged
    except Exception:
        raise DeletionIntentError() from None
