"""Read an exact previously acknowledged stack; no jobs or mutation authority."""
from dataclasses import dataclass
import json
import re

from oci.retry import NoneRetryStrategy

from .bootstrap_commitment import freeze_stack_request
from .oci_client import bounded_client
from .oci_transport import oci_operation, strong_etag
from .stack_bootstrap import BOOTSTRAP_VARIABLE, InitialStackRequest, validate_initial_stack
from .stack_ownership import freeze_stack_ownership, ownership_payload


class OwnedStackError(RuntimeError):
    def __init__(self):
        super().__init__("confirmed_stack_unavailable_or_mismatched")


@dataclass(frozen=True, repr=False)
class OwnedStackObservation:
    """Current identity observation, explicitly not an initial no-jobs ACK."""

    request: InitialStackRequest
    stack_id: str
    etag: str


class OciOwnedStack:
    def __init__(self, config, request, ownership):
        try:
            validate_initial_stack(request)
            ownership_payload(ownership)
            if (config["user"] != request.principal
                    or ownership.bootstrap != freeze_stack_request(request.scope, request.attempt, request.body)):
                raise OwnedStackError()
            self._client = bounded_client(config, request.scope, "resourcemanager")
        except Exception:
            raise OwnedStackError() from None
        self._request, self._ownership = request, ownership

    def close(self):
        try:
            self._client.base_client.session.close()
        except Exception:
            raise OwnedStackError() from None

    def _candidate(self):
        page, found, seen_ids, seen_pages = None, None, set(), set()
        for _ in range(5):
            result = self._client.list_stacks(compartment_id=self._request.scope.compartment_id,
                display_name=self._request.display_name, sort_by="DISPLAYNAME", limit=10, page=page,
                retry_strategy=NoneRetryStrategy())
            if result.status != 200 or type(result.data) is not list or len(result.data) > 10:
                raise OwnedStackError()
            for item in result.data:
                identifier = item["id"]
                binding = freeze_stack_ownership(self._ownership.bootstrap, identifier)
                if identifier in seen_ids:
                    raise OwnedStackError()
                seen_ids.add(identifier)
                if binding == self._ownership:
                    if found is not None:
                        raise OwnedStackError()
                    found = identifier
            if "opc-next-page" not in result.headers:
                if found is None:
                    raise OwnedStackError()
                return found
            page = result.headers["opc-next-page"]
            if (type(page) is not str or re.fullmatch(r"[!-~]{1,1024}", page) is None or page in seen_pages):
                raise OwnedStackError()
            seen_pages.add(page)
        raise OwnedStackError()

    def read(self):
        try:
            with oci_operation():
                identifier = self._candidate()
                result = self._client.get_stack(identifier, retry_strategy=NoneRetryStrategy())
                data = result.data
                expected = json.loads(self._request.body)
                if (result.status != 200 or type(data) is not dict or data.get("id") != identifier
                        or data.get("lifecycleState") != "ACTIVE"
                        or data.get("compartmentId") != expected["compartmentId"]
                        or data.get("displayName") != expected["displayName"]):
                    raise OwnedStackError()
                for name in ("variables", "freeformTags"):
                    values = data.get(name)
                    if (type(values) is not dict
                            or any(type(k) is not str or not k or type(v) is not str for k, v in values.items())):
                        raise OwnedStackError()
                if (data["variables"].get(BOOTSTRAP_VARIABLE) != expected["variables"][BOOTSTRAP_VARIABLE]
                        or any(data["freeformTags"].get(k) != v for k, v in expected["freeformTags"].items())):
                    raise OwnedStackError()
                revision = strong_etag(result.headers.get("etag"))
            return OwnedStackObservation(self._request, identifier, revision)
        except Exception:
            raise OwnedStackError() from None
