"""Independent cleanup receipts on a positively owned, retained RM stack.

The caller must establish stack ownership and durably prepare the journal
receipt first. This adapter does not discover/create stacks or delete data.
"""
import json
import re

from oci.exceptions import ServiceError
from oci.resource_manager.models import UpdateStackDetails
from oci.retry import NoneRetryStrategy

from .codec import MAX_BYTES, encode, validate_identity
from .oci_client import bounded_client
from .oci_transport import oci_operation, strong_etag
from .receipt_variables import merge_receipt, read_receipt


class ReceiptError(RuntimeError):
    def __init__(self):
        super().__init__("oci_receipt_unavailable_or_mismatched")


class ReceiptConflict(RuntimeError):
    def __init__(self):
        super().__init__("oci_receipt_revision_changed_reread_required")


class ReceiptUncertain(RuntimeError):
    def __init__(self):
        super().__init__("oci_receipt_write_unknown_readback_required")


class OciReceiptStore:
    def __init__(self, config, identity, stack_id):
        try:
            validate_identity(identity)
            if type(stack_id) is not str or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", stack_id):
                raise ReceiptError()
            self._client = bounded_client(config, identity.scope, "resourcemanager")
        except Exception:
            raise ReceiptError() from None
        self._identity, self._stack_id = identity, stack_id

    def close(self):
        try:
            self._client.base_client.session.close()
        except Exception:
            raise ReceiptError() from None

    def _observed(self, result):
        stack = result.data
        required = {"id": self._stack_id, "compartmentId": self._identity.scope.compartment_id,
                    "lifecycleState": "ACTIVE"}
        if (result.status != 200 or type(stack) is not dict
                or any(type(stack.get(k)) is not str or stack.get(k) != v for k, v in required.items())):
            raise ReceiptError()
        revision = strong_etag(result.headers.get("etag"))
        variables = stack.get("variables")
        receipt = read_receipt(variables)
        if receipt is not None and receipt.identity != self._identity:
            raise ReceiptError()
        return revision, variables, receipt

    def _get(self):
        return self._observed(self._client.get_stack(self._stack_id, retry_strategy=NoneRetryStrategy()))

    def read(self):
        try:
            with oci_operation():
                _, _, receipt = self._get()
            return receipt
        except Exception:
            # A missing/denied stack never means an absent receipt.
            raise ReceiptError() from None

    def persist(self, record):
        dispatched = False
        try:
            encode(record)
            if record.identity != self._identity or record.receipt is None:
                raise ReceiptError()
            with oci_operation():
                revision, variables, existing = self._get()
                merged = merge_receipt(variables, record.receipt)
                if existing is None:
                    # Match the pinned BaseClient's JSON serialization bound.
                    if len(json.dumps({"variables": merged}).encode("utf-8")) > MAX_BYTES:
                        raise ReceiptError()
                    dispatched = True
                    try:
                        result = self._client.update_stack(
                            self._stack_id, UpdateStackDetails(variables=merged),
                            if_match=revision, retry_strategy=NoneRetryStrategy(),
                        )
                    except ServiceError as error:
                        if error.status == 412:
                            raise ReceiptConflict() from None
                        raise
                    _, acknowledged, _ = self._observed(result)
                    if acknowledged != merged:
                        raise ReceiptUncertain()
                    _, observed, existing = self._get()
                    if observed != merged or existing != record.receipt:
                        raise ReceiptUncertain()
            # Always comes from an independent GET, never the PUT acknowledgement.
            return existing
        except ReceiptConflict:
            raise
        except Exception:
            if dispatched:
                raise ReceiptUncertain() from None
            raise ReceiptError() from None
