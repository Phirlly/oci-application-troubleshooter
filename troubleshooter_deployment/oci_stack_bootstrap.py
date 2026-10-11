"""One initial CreateStack ACK plus independent readback; no jobs or adoption."""
from dataclasses import dataclass
import json
import re

from oci.retry import NoneRetryStrategy

from .oci_client import bounded_client
from .oci_transport import oci_operation, strong_etag
from .stack_bootstrap import InitialStackRequest, require_retry_window, validate_initial_stack


class StackBootstrapError(RuntimeError):
    def __init__(self):
        super().__init__("initial_stack_unavailable_or_mismatched")


@dataclass(frozen=True, repr=False)
class InitialStackAcknowledgement:
    """Only this adapter's validated ACK/GET/no-jobs path supplies ownership."""

    request: InitialStackRequest
    stack_id: str
    etag: str


class OciStackBootstrap:
    def __init__(self, config, request):
        try:
            validate_initial_stack(request)
            if config["user"] != request.principal:
                raise StackBootstrapError()
            self._client = bounded_client(config, request.scope, "resourcemanager")
        except Exception:
            raise StackBootstrapError() from None
        self._request = request

    def close(self):
        try:
            self._client.base_client.session.close()
        except Exception:
            raise StackBootstrapError() from None

    @staticmethod
    def _empty(result):
        if result.status != 200 or type(result.data) is not list or result.data or "opc-next-page" in result.headers:
            raise StackBootstrapError()

    def check_name_available(self):
        """Conflict check only; not an absence proof or creation authorization."""
        try:
            with oci_operation():
                self._empty(self._client.list_stacks(compartment_id=self._request.scope.compartment_id,
                    display_name=self._request.display_name, sort_by="DISPLAYNAME", limit=1,
                    retry_strategy=NoneRetryStrategy()))
        except Exception:
            raise StackBootstrapError() from None

    def _observed(self, result):
        data = result.data
        expected = json.loads(self._request.body)
        if result.status != 200 or type(data) is not dict:
            raise StackBootstrapError()
        identifier = data.get("id")
        if type(identifier) is not str or re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", identifier) is None:
            raise StackBootstrapError()
        for field in ("compartmentId", "displayName", "terraformVersion", "variables", "freeformTags"):
            if type(data.get(field)) is not type(expected[field]) or data[field] != expected[field]:
                raise StackBootstrapError()
        source = data.get("configSource")
        if (data.get("lifecycleState") != "ACTIVE" or type(source) is not dict
                or source.get("configSourceType") != "ZIP_UPLOAD" or source.get("workingDirectory") not in (None, "")):
            raise StackBootstrapError()
        return identifier, strong_etag(result.headers.get("etag"))

    def create(self, created_at):
        """Dispatch once under the caller's confirmed original commitment.

        Every failure is uncertain. Caller must preserve that commitment and
        must not mint another token. This method never retries or polls.
        """
        try:
            with oci_operation():
                require_retry_window(created_at)
                ack_id, _ = self._observed(self._client.create_stack(self._request.body,
                    opc_retry_token=self._request.token, retry_strategy=NoneRetryStrategy()))
                read_id, revision = self._observed(self._client.get_stack(ack_id, retry_strategy=NoneRetryStrategy()))
                if read_id != ack_id:
                    raise StackBootstrapError()
                self._empty(self._client.list_jobs(stack_id=ack_id, limit=1, retry_strategy=NoneRetryStrategy()))
            return InitialStackAcknowledgement(self._request, ack_id, revision)
        except Exception:
            raise StackBootstrapError() from None
