"""Resource-free unapplied stack fixture; never used to destroy a workload."""
from base64 import b64encode
from hashlib import sha256
from io import BytesIO
import zipfile

from oci.resource_manager.models import CreateStackDetails, CreateZipUploadConfigSourceDetails
from oci.retry import NoneRetryStrategy

from troubleshooter_deployment.oci_client import bounded_client
from troubleshooter_deployment.oci_transport import oci_operation, strong_etag
from troubleshooter_deployment.recovery_bucket import require_text


def fixture_zip():
    # Deterministic, provider/resource-free request bytes. No Apply is submitted.
    variables = ("troubleshooter_cleanup_receipt_0", "troubleshooter_cleanup_receipt_1",
                 "troubleshooter_cleanup_receipt_sha256")
    text = "\n".join(f'variable "{name}" {{\n  type = string\n  default = ""\n}}\n' for name in variables)
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(zipfile.ZipInfo("main.tf", (2026, 1, 1, 0, 0, 0)), text)
    return stream.getvalue()


ZIP_SHA256 = sha256(fixture_zip()).hexdigest()  # Local request provenance, not remote attestation.


class StackError(RuntimeError):
    def __init__(self):
        super().__init__("proof_stack_unavailable_or_unconfirmed")


class ProofStack:
    def __init__(self, config, scope):
        self.client = bounded_client(config, scope, "resourcemanager")

    def close(self):
        self.client.base_client.session.close()

    def _observed(self, result, state, creating=False):
        data = result.data
        required = {"compartmentId": state["scope"]["compartment_id"], "displayName": state["stack_name"],
                    "lifecycleState": "ACTIVE", "freeformTags": {"recovery_proof": state["epoch"]}}
        if (result.status != 200 or type(data) is not dict
                or any(type(data.get(k)) is not type(v) or data[k] != v for k, v in required.items())
                or type(data.get("configSource")) is not dict
                or data["configSource"].get("configSourceType") != "ZIP_UPLOAD"
                or data["configSource"].get("workingDirectory") not in ("", None)
                or type(data.get("variables")) is not dict):
            raise StackError()
        identifier = require_text(data.get("id"), r"[A-Za-z0-9_.:-]{1,256}")
        if not creating and identifier != state["stack_id"]:
            raise StackError()
        return identifier, strong_etag(result.headers.get("etag"))

    def create(self, state):
        try:
            details = CreateStackDetails(
                compartment_id=state["scope"]["compartment_id"], display_name=state["stack_name"],
                freeform_tags={"recovery_proof": state["epoch"]}, variables={},
                config_source=CreateZipUploadConfigSourceDetails(
                    working_directory="", zip_file_base64_encoded=b64encode(fixture_zip()).decode("ascii")),
            )
            with oci_operation():
                result = self.client.create_stack(details, opc_retry_token=state["stack_token"],
                                                   retry_strategy=NoneRetryStrategy())
                identifier, _ = self._observed(result, state, creating=True)
            return identifier
        except Exception:
            raise StackError() from None

    def _get(self, state):
        return self._observed(self.client.get_stack(state["stack_id"], retry_strategy=NoneRetryStrategy()), state)

    def check(self, state):
        try:
            with oci_operation():
                self._get(state)
        except Exception:
            raise StackError() from None

    def delete(self, state):
        try:
            with oci_operation():
                _, etag = self._get(state)
                jobs = self.client.list_jobs(stack_id=state["stack_id"], limit=1, retry_strategy=NoneRetryStrategy())
                if jobs.status != 200 or jobs.data != [] or "opc-next-page" in jobs.headers:
                    raise StackError()
                result = self.client.delete_stack(state["stack_id"], if_match=etag, retry_strategy=NoneRetryStrategy())
                if result.status != 204:
                    raise StackError()
        except Exception:
            raise StackError() from None
