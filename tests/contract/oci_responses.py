"""Synthetic OCI HTTP responses beneath the real SDK serialization/session layers."""
from dataclasses import replace
from io import BytesIO
import json
from unittest.mock import patch

from oci._vendor.requests import Response
from oci._vendor.requests.structures import CaseInsensitiveDict

from tests.deployment.fixtures import IDENTITY

OCI_IDENTITY = replace(IDENTITY, scope=replace(IDENTITY.scope, tenancy_id="test.tenancy.oc1..synthetic"))
CONFIG = {"tenancy": OCI_IDENTITY.scope.tenancy_id, "user": "test.user.oc1..synthetic",
          "fingerprint": ":".join(["00"] * 16), "region": "us-ashburn-1",
          "key_content": "synthetic-not-a-private-key"}
BUCKET = {
    "id": OCI_IDENTITY.bucket_id, "namespace": "synthetic-ns", "name": "synthetic-bucket",
    "compartmentId": OCI_IDENTITY.scope.compartment_id,
    "metadata": {"deployment_epoch": OCI_IDENTITY.epoch}, "publicAccessType": "NoPublicAccess",
    "storageTier": "Standard", "versioning": "Disabled", "bucketScope": "NAMESPACE",
    "isReadOnly": False, "kmsKeyId": None,
}


class SyntheticSigner:
    @property
    def without_content_headers(self):
        return self

    def __call__(self, request):
        request.headers["authorization"] = "synthetic-signature"
        return request


class RawBody(BytesIO):
    def read(self, size=-1, decode_content=False):
        return super().read(size)

    def release_conn(self):
        pass


def response(data=None, *, body=None, status=200, headers=None):
    content = json.dumps(BUCKET if data is None else data).encode() if body is None else body
    result = Response()
    result.status_code = status
    result.headers = CaseInsensitiveDict({"Content-Type": "application/json", "Content-Length": str(len(content)),
                                         "etag": '"revision-1"', **(headers or {})})
    result.raw = RawBody(content)
    result.url = "https://objectstorage.us-ashburn-1.oraclecloud.com/synthetic"
    return result


def signing_stub():
    return patch("oci.object_storage.object_storage_client.Signer", return_value=SyntheticSigner())
