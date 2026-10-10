"""Conditional Object Storage journal for a positively identified existing bucket.

Name-addressed requests cannot atomically fence replacement by an equally
privileged principal. Bootstrap and live IAM proof remain separate gates.
"""
from contextlib import contextmanager
import os
import re

from oci.circuit_breaker import NoCircuitBreakerStrategy
from oci.exceptions import ServiceError
from oci.object_storage import ObjectStorageClient
from oci.retry import NoneRetryStrategy

from .codec import decode, validate_identity
from .journal import WriteConflict, WriteUncertain
from .oci_transport import BoundedOciSession
from .request_budget import operation_budget


class StoreError(RuntimeError):
    def __init__(self):
        super().__init__("oci_journal_unavailable_or_mismatched")


def _etag(value):
    if (type(value) is not str or not 0 < len(value) <= 1024 or value.startswith("W/")
            or value == "*" or not re.fullmatch(r'"[!#-+\--~]+"|[!#-+\--~]+', value)):
        raise StoreError()
    return value


@contextmanager
def _operation():
    # NoneRetryStrategy does not disable this independent BaseClient retry path.
    key = "OCI_HEADER_PARSING_ERROR_MAX_RETRIES"
    with operation_budget():
        previous = os.environ.get(key)
        os.environ[key] = "0"
        try:
            yield
        finally:
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous


class OciJournalStore:
    def __init__(self, config, identity, namespace, bucket):
        try:
            validate_identity(identity)
            if (type(config) is not dict or set(config) != {"tenancy", "user", "fingerprint", "region", "key_content"}
                    or config["tenancy"] != identity.scope.tenancy_id or config["region"] != identity.scope.region
                    or any(type(value) is not str or not value for value in config.values())
                    or any(type(value) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", value)
                           for value in (namespace, bucket))):
                raise StoreError()
            origin = f"https://objectstorage.{identity.scope.region}.oraclecloud.com"
            self._client = ObjectStorageClient(
                dict(config), service_endpoint=origin, retry_strategy=NoneRetryStrategy(), timeout=(5, 5),
                circuit_breaker_strategy=NoCircuitBreakerStrategy(), enable_strict_url_encoding=True,
                skip_deserialization=True,
                client_level_realm_specific_endpoint_template_enabled=False,
                client_level_dualstack_endpoints_enabled=False,
            )
            self._client.base_client.session.close()
            self._client.base_client.session = BoundedOciSession(origin)
            self._client.base_client.logger.disabled = True
        except Exception:
            raise StoreError() from None
        self._identity, self._namespace, self._bucket = identity, namespace, bucket
        self._object = f"journal/{identity.epoch}.json"

    def close(self):
        try:
            self._client.base_client.session.close()
        except Exception:
            raise StoreError() from None

    def _check_bucket(self):
        result = self._client.get_bucket(self._namespace, self._bucket, retry_strategy=NoneRetryStrategy())
        bucket = result.data
        required = {"id": self._identity.bucket_id, "name": self._bucket, "namespace": self._namespace,
                    "compartmentId": self._identity.scope.compartment_id,
                    "publicAccessType": "NoPublicAccess", "storageTier": "Standard",
                    "versioning": "Disabled", "bucketScope": "NAMESPACE", "isReadOnly": False}
        if (result.status != 200 or type(bucket) is not dict
                or any(type(bucket.get(key)) is not type(value) or bucket.get(key) != value
                       for key, value in required.items())
                or type(bucket.get("metadata")) is not dict
                or type(bucket["metadata"].get("deployment_epoch")) is not str
                or bucket["metadata"]["deployment_epoch"] != self._identity.epoch
                or bucket.get("kmsKeyId") is not None):
            raise StoreError()

    def _body(self, body):
        if decode(body).identity != self._identity:
            raise StoreError()

    def read(self):
        try:
            with _operation():
                self._check_bucket()
                result = self._client.get_object(self._namespace, self._bucket, self._object,
                                                 retry_strategy=NoneRetryStrategy())
                if result.status != 200:
                    raise StoreError()
                revision, body = _etag(result.headers.get("etag")), result.data.content
                self._body(body)
                self._check_bucket()
            return revision, body
        except Exception:
            # A 404 can mean denied access. It never authorizes initialization.
            raise StoreError() from None

    def write(self, expected, body):
        dispatched = False
        try:
            self._body(body)
            condition = {"if_none_match": "*"} if expected is None else {"if_match": _etag(expected)}
            with _operation():
                self._check_bucket()
                dispatched = True
                try:
                    result = self._client.put_object(
                        self._namespace, self._bucket, self._object, body, content_length=len(body),
                        content_type="application/json", retry_strategy=NoneRetryStrategy(), **condition,
                    )
                except ServiceError as error:
                    if error.status == 412:
                        raise WriteConflict() from None
                    raise
                if result.status != 200:
                    raise WriteUncertain()
                revision = _etag(result.headers.get("etag"))
                self._check_bucket()
            return revision
        except WriteConflict:
            raise
        except Exception:
            if dispatched:
                raise WriteUncertain() from None
            raise StoreError() from None
