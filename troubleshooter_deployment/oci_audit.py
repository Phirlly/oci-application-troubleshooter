"""Bounded Audit candidate observations; these NEVER authorize mutation/cleanup."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re

from oci.exceptions import ConnectTimeout, RequestException, ServiceError
from oci.retry import NoneRetryStrategy

from .oci_client import bounded_client
from .oci_transport import TRANSPORT_REASONS, TransportError, oci_operation
from .recovery_bucket import require_text, validate_acknowledgement
from .request_budget import BudgetError


SERVICE_STATUSES = (400, 401, 403, 404, 408, 409, 429, 500, 502, 503, 504)
SCAN_REASONS = ("query", "request", "service", "response", "event_limit", "pagination", "page_limit", "budget",
                *("service_" + str(status) for status in SERVICE_STATUSES))


class AuditError(RuntimeError):
    def __init__(self, transport_reason=None, *, scan_reason=None):
        self.transport_reason = (transport_reason if type(transport_reason) is str
                                 and transport_reason in TRANSPORT_REASONS else None)
        self.scan_reason = scan_reason if type(scan_reason) is str and scan_reason in SCAN_REASONS else None
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
        phase = "query"
        try:
            validate_acknowledgement(ack)
            require_text(correlation, r"[0-9a-f]{32}")
            if (ack.request.scope != self._scope or type(operation) is not str
                    or operation not in {"CreateBucket", "DeleteBucket"}):
                raise AuditError(scan_reason=phase)
            for instant in (start, end):
                if (type(instant) is not datetime or instant.utcoffset() != timedelta(0)
                        or instant.second or instant.microsecond):
                    raise AuditError(scan_reason=phase)
            if not timedelta(0) < end - start <= timedelta(hours=1):
                raise AuditError(scan_reason=phase)
            matches, seen, count, page = [], set(), 0, None
            with oci_operation():
                for _ in range(5):
                    phase = "request"
                    result = self._client.list_events(
                        self._scope.compartment_id, start.astimezone(timezone.utc), end.astimezone(timezone.utc),
                        retry_strategy=NoneRetryStrategy(), **({} if page is None else {"page": page}),
                    )
                    phase = "response"
                    if result.status != 200 or type(result.data) is not list:
                        raise AuditError(scan_reason=phase)
                    count += len(result.data)
                    if count > 1024:
                        raise AuditError(scan_reason="event_limit")
                    if any(type(event) is not dict for event in result.data):
                        raise AuditError(scan_reason=phase)
                    for event in result.data:
                        candidate = _candidate(event, ack, operation, correlation)
                        if candidate is not None:
                            matches.append(candidate)
                    if "opc-next-page" not in result.headers:
                        break
                    phase = "pagination"
                    page = require_text(result.headers["opc-next-page"], r"[!-~]{1,1024}")
                    if page in seen:
                        raise AuditError(scan_reason=phase)
                    seen.add(page)
                else:
                    raise AuditError(scan_reason="page_limit")
            return AuditScan(start, end, tuple(matches))
        except TransportError as error:
            raise AuditError(error.reason) from None
        except AuditError:
            raise
        except BudgetError:
            raise AuditError(scan_reason="budget") from None
        except ServiceError as error:
            reason = ("service_" + str(error.status) if type(error.status) is int
                      and error.status in SERVICE_STATUSES else "service")
            raise AuditError(scan_reason=reason) from None
        except (RequestException, ConnectTimeout):
            raise AuditError(scan_reason="request") from None
        except Exception:
            # Partial scans and errors yield no apparently complete observation.
            raise AuditError(scan_reason=phase) from None
