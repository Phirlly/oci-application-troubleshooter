"""Single-dispatch creation and receipt-gated EMPTY bucket deletion.

The caller durably freezes intent before dispatch and retains uncertainty.
No adoption, implicit retry, object deletion or lost-response reconciliation.
"""
from oci.object_storage.models import Bucket, CreateBucketDetails
from oci.retry import NoneRetryStrategy

from .cleanup import may_purge
from .oci_client import bounded_client
from .oci_transport import oci_operation, strong_etag
from .records import DeleteAcknowledgement, Identity
from .recovery_bucket import (
    BucketAcknowledgement, BucketObservation, metadata, require_text,
    validate_acknowledgement, validate_request,
)


class BucketError(RuntimeError):
    def __init__(self):
        super().__init__("recovery_bucket_unavailable_or_mismatched")


class BucketUncertain(RuntimeError):
    def __init__(self):
        super().__init__("recovery_bucket_dispatch_outcome_unknown")


class OciRecoveryBucket:
    def __init__(self, config, scope):
        try:
            self._client = bounded_client(config, scope, "objectstorage")
            self._scope, self._principal = scope, config["user"]
        except Exception:
            raise BucketError() from None

    def close(self):
        try:
            self._client.base_client.session.close()
        except Exception:
            raise BucketError() from None

    def namespace(self):
        try:
            with oci_operation():
                result = self._client.get_namespace(retry_strategy=NoneRetryStrategy())
                if result.status != 200:
                    raise BucketError()
                namespace = require_text(result.data, r"[A-Za-z0-9_-]{1,256}")
            return namespace
        except Exception:
            raise BucketError() from None

    def _intent(self, request):
        validate_request(request)
        if request.scope != self._scope or request.principal != self._principal:
            raise BucketError()

    def _observed(self, result, request, identity=None):
        data = result.data
        required = {"name": request.name, "namespace": request.namespace,
                    "compartmentId": request.scope.compartment_id, "createdBy": request.principal,
                    "publicAccessType": "NoPublicAccess", "storageTier": "Standard",
                    "versioning": "Disabled", "autoTiering": "Disabled", "bucketScope": "NAMESPACE",
                    "isReadOnly": False, "objectEventsEnabled": False, "metadata": metadata(request)}
        if (result.status != 200 or type(data) is not dict or data.get("kmsKeyId") is not None
                or any(type(data.get(k)) is not type(v) or data[k] != v for k, v in required.items())):
            raise BucketError()
        bucket_id = require_text(data.get("id"), r"[A-Za-z0-9_.:-]{1,256}")
        actual = Identity(request.scope, bucket_id, request.epoch)
        if identity is not None and actual != identity:
            raise BucketError()
        unknown = tuple(sorted(set(data) - set(Bucket().attribute_map.values())))
        if len(unknown) > 32:
            raise BucketError()
        for name in unknown:
            require_text(name, r"[A-Za-z][A-Za-z0-9_]{0,63}")
        return actual, BucketObservation(strong_etag(result.headers.get("etag")), unknown)

    def create(self, request):
        dispatched = False
        try:
            self._intent(request)
            details = CreateBucketDetails(
                name=request.name, compartment_id=request.scope.compartment_id, metadata=metadata(request),
                public_access_type="NoPublicAccess", storage_tier="Standard", object_events_enabled=False,
                versioning="Disabled", auto_tiering="Disabled", bucket_scope="NAMESPACE",
            )
            with oci_operation():
                dispatched = True
                result = self._client.create_bucket(request.namespace, details,
                                                   opc_client_request_id=request.correlation,
                                                   retry_strategy=NoneRetryStrategy())
                identity, _ = self._observed(result, request)
            return BucketAcknowledgement(request, identity)
        except Exception:
            if dispatched:
                raise BucketUncertain() from None
            raise BucketError() from None

    def _read(self, ack):
        validate_acknowledgement(ack)
        self._intent(ack.request)
        result = self._client.get_bucket(ack.request.namespace, ack.request.name,
                                         fields=["autoTiering"],
                                         retry_strategy=NoneRetryStrategy())
        return self._observed(result, ack.request, ack.identity)[1]

    def read(self, ack):
        try:
            with oci_operation():
                observed = self._read(ack)
            return observed
        except Exception:
            raise BucketError() from None

    def delete_empty(self, ack, record, durable_readback, correlation):
        dispatched = False
        try:
            validate_acknowledgement(ack)
            require_text(correlation, r"[0-9a-f]{32}")
            if (record.identity != ack.identity or correlation == ack.request.correlation
                    or not may_purge(record, durable_readback)):
                raise BucketError()
            with oci_operation():
                observed = self._read(ack)
                dispatched = True
                result = self._client.delete_bucket(
                    ack.request.namespace, ack.request.name, if_match=observed.revision,
                    opc_client_request_id=correlation, retry_strategy=NoneRetryStrategy(),
                )
                if result.status != 204:
                    raise BucketUncertain()
            return DeleteAcknowledgement(ack.identity, record.receipt.removal_id)
        except Exception:
            if dispatched:
                raise BucketUncertain() from None
            raise BucketError() from None
