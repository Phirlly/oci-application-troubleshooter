"""Bounded nonsecret Resource Manager variable carrier for one frozen receipt."""
from hashlib import sha256

from .codec import decode_receipt, encode_receipt

PREFIX = "troubleshooter_cleanup_receipt_"
CHUNK_BYTES = 6000
KEYS = {PREFIX + "0", PREFIX + "1", PREFIX + "sha256"}


class ReceiptVariablesError(ValueError):
    def __init__(self):
        super().__init__("invalid_or_conflicting_cleanup_receipt_variables")


def _validate(variables):
    if (type(variables) is not dict or len(variables) > 250
            or any(type(k) is not str or not k or type(v) is not str
                   or len(k.encode("utf-8")) + len(v.encode("utf-8")) > 8192
                   for k, v in variables.items())):
        raise ReceiptVariablesError()


def _pack(receipt):
    body = encode_receipt(receipt)
    return {PREFIX + "0": body[:CHUNK_BYTES].decode("ascii"),
            PREFIX + "1": body[CHUNK_BYTES:].decode("ascii"),
            PREFIX + "sha256": sha256(body).hexdigest()}


def read_receipt(variables):
    try:
        _validate(variables)
        present = {k: v for k, v in variables.items() if k.startswith(PREFIX)}
        if not present:
            return None
        if set(present) != KEYS:
            raise ReceiptVariablesError()
        body = (present[PREFIX + "0"] + present[PREFIX + "1"]).encode("ascii")
        if sha256(body).hexdigest() != present[PREFIX + "sha256"]:
            raise ReceiptVariablesError()
        receipt = decode_receipt(body)
        if _pack(receipt) != present:
            raise ReceiptVariablesError()
        return receipt
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise ReceiptVariablesError() from None


def merge_receipt(variables, receipt):
    """Preserve every unrelated variable and refuse to replace another receipt."""
    try:
        existing = read_receipt(variables)
        if existing is not None and existing != receipt:
            raise ReceiptVariablesError()
        merged = dict(variables, **_pack(receipt))
        _validate(merged)
        return merged
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise ReceiptVariablesError() from None
