"""Bounded Audit candidate observations; these NEVER authorize mutation/cleanup."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re

from oci.retry import NoneRetryStrategy

from .oci_client import bounded_client
from .oci_transport import oci_operation
from .recovery_bucket import require_text, validate_acknowledgement


class AuditError(RuntimeError):
    def __init__(self):
        super().__init__("audit_scan_unavailable_or_incomplete")


@dataclass(frozen=True)
class AuditCandidate:
    event_id: str
    event_time: str
    event_type: str
    event_type_version: str
    source: str


@dataclass(frozen=True)
class AuditScan:
    processed_start: datetime
    processed_end: datetime
    matches: tuple[AuditCandidate, ...]
    complete: bool = True


def _candidate(event, ack, operation, correlation):
    """A missing or changed service field yields no candidate, never a guess."""
    try:
        request = ack.request
        data = event["data"]
        identity, api_request, response = data["identity"], data["request"], data["response"]
        method, path, status = ("POST", f"/n/{request.namespace}/b", "200") if operation == "CreateBucket" else (
            "DELETE", f"/n/{request.namespace}/b/{request.name}", "204")
        checks = [
            (event["cloudEventsVersion"], "0.1"), (event["contentType"], "application/json"),
            (data["eventName"], operation), (data["resourceName"], request.name),
            (data["resourceId"], ack.identity.bucket_id), (data["compartmentId"], request.scope.compartment_id),
            (identity["principalId"], request.principal), (identity["tenantId"], request.scope.tenancy_id),
            (api_request["action"], method), (api_request["path"], path), (response["status"], status),
        ]
        if any(type(actual) is not str or actual != expected for actual, expected in checks):
            return None
        headers = api_request["headers"]
        if type(headers) is not dict:
            return None
        correlations = [value for key, value in headers.items() if key.lower() == "opc-client-request-id"]
        if correlations != [[correlation]]:
            return None
        event_id = require_text(event["eventId"], r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
        event_time = require_text(event["eventTime"], r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?Z")
        datetime.fromisoformat(event_time)
        values = [require_text(event[key], r"[A-Za-z0-9_.:-]{1,256}")
                  for key in ("eventType", "eventTypeVersion", "source")]
        return AuditCandidate(event_id, event_time, *values)
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


class OciBucketAudit:
    def __init__(self, config, scope):
        try:
            self._client = bounded_client(config, scope, "audit")
            self._scope = scope
        except Exception:
            raise AuditError() from None

    def close(self):
        try:
            self._client.base_client.session.close()
        except Exception:
            raise AuditError() from None

    def observe(self, ack, operation, correlation, start, end):
        try:
            validate_acknowledgement(ack)
            require_text(correlation, r"[0-9a-f]{32}")
            if (ack.request.scope != self._scope or type(operation) is not str
                    or operation not in {"CreateBucket", "DeleteBucket"}):
                raise AuditError()
            for instant in (start, end):
                if (type(instant) is not datetime or instant.utcoffset() != timedelta(0)
                        or instant.second or instant.microsecond):
                    raise AuditError()
            if not timedelta(0) < end - start <= timedelta(hours=1):
                raise AuditError()
            matches, seen, count, page = [], set(), 0, None
            with oci_operation():
                for _ in range(5):
                    result = self._client.list_events(
                        self._scope.compartment_id, start.astimezone(timezone.utc), end.astimezone(timezone.utc),
                        retry_strategy=NoneRetryStrategy(), **({} if page is None else {"page": page}),
                    )
                    if result.status != 200 or type(result.data) is not list:
                        raise AuditError()
                    count += len(result.data)
                    if count > 1024 or any(type(event) is not dict for event in result.data):
                        raise AuditError()
                    for event in result.data:
                        candidate = _candidate(event, ack, operation, correlation)
                        if candidate is not None:
                            matches.append(candidate)
                    if "opc-next-page" not in result.headers:
                        break
                    page = require_text(result.headers["opc-next-page"], r"[!-~]{1,1024}")
                    if page in seen:
                        raise AuditError()
                    seen.add(page)
                else:
                    raise AuditError()
            return AuditScan(start, end, tuple(matches))
        except Exception:
            # Partial scans and errors yield no apparently complete observation.
            raise AuditError() from None
