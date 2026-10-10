"""Independent cleanup receipts on a positively owned, retained RM stack.

The caller must establish stack ownership and durably prepare the journal
receipt first. This adapter does not discover/create stacks or delete data.
"""
import json
import re

from oci.exceptions import ServiceError
from oci.resource_manager.models import UpdateStackDetails
from oci.retry import NoneRetryStrategy

from .bucket_deletion import encode_intent, merge_deletion, read_deletion
from .codec import MAX_BYTES, encode, encode_receipt, validate_identity
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
        deletion = read_deletion(variables)
        return revision, variables, receipt, deletion

    def _get(self):
        return self._observed(self._client.get_stack(self._stack_id, retry_strategy=NoneRetryStrategy()))

    def read(self):
        try:
            with oci_operation():
                _, _, receipt, _ = self._get()
            return receipt
        except Exception:
            # A missing/denied stack never means an absent receipt.
            raise ReceiptError() from None

    def persist(self, record):
        return self._persist(record)[0]

    def read_deletion(self, receipt):
        try:
            encode_receipt(receipt)
            if receipt.identity != self._identity:
                raise ReceiptError()
            with oci_operation():
                _, _, observed, deletion = self._get()
                if observed != receipt:
                    raise ReceiptError()
            return deletion
        except Exception:
            raise ReceiptError() from None

    def persist_deletion(self, record, intent):
        try:
            encode(record)
            encode_intent(intent, record.receipt)
        except Exception:
            raise ReceiptError() from None
        return self._persist(record, intent)[1]

    def _persist(self, record, intent=None):
        dispatched = False
        try:
            encode(record)
            if record.identity != self._identity or record.receipt is None:
                raise ReceiptError()
            with oci_operation():
                revision, variables, existing, deletion = self._get()
                if intent is None:
                    merged = merge_receipt(variables, record.receipt)
                else:
                    if existing != record.receipt:
                        raise ReceiptError()
                    merged = merge_deletion(variables, intent)
                if merged != variables:
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
                    _, acknowledged, _, _ = self._observed(result)
                    if acknowledged != merged:
                        raise ReceiptUncertain()
                    _, observed, existing, deletion = self._get()
                    if (observed != merged or existing != record.receipt
                            or (intent is not None and deletion != intent)):
                        raise ReceiptUncertain()
            # Always comes from an independent GET, never the PUT acknowledgement.
            return existing, deletion
        except ReceiptConflict:
            raise
        except Exception:
            if dispatched:
                raise ReceiptUncertain() from None
            raise ReceiptError() from None
