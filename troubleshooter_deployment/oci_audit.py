"""Bounded Audit observations and exact request-success reconciliation."""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from oci.exceptions import ConnectTimeout, RequestException, ServiceError
from oci.loggingsearch.models import SearchLogsDetails
from oci.retry import NoneRetryStrategy

from .bucket_deletion import encode_intent
from .oci_client import bounded_client
from .oci_transport import TRANSPORT_REASONS, TransportError, oci_operation
from .recovery_bucket import (
    BucketCreationEvidence, creation_digest_v1, require_text, validate_acknowledgement, validate_request,
)
from .records import DeleteAcknowledgement
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
    data_schema: str
    source: str


@dataclass(frozen=True)
class AuditScan:
    search_start: datetime
    search_end: datetime
    matches: tuple[AuditCandidate, ...]
    # Accepted search pages cannot establish index/ingestion completeness.
    complete: bool = field(default=False, init=False)


def _candidate(event, request, operation, correlation, if_match=None, content_sha256=None):
    """A missing or changed service field yields no candidate, never a guess."""
    try:
        data = event["data"]
        identity, api_request, response = data["identity"], data["request"], data["response"]
        method, path, status = ("POST", f"/n/{request.namespace}/b", "200") if operation == "CreateBucket" else (
            "DELETE", f"/n/{request.namespace}/b/{request.name}", "204")
        checks = [
            (event["specversion"], "1.0"), (event["dataschema"], "2.0"),
            (event.get("datacontenttype", "application/json"), "application/json"),
            (event["type"], "com.oraclecloud.objectstorage." + operation.lower()),
            (data["eventName"], operation), (data["additionalDetails"]["bucketName"], request.name),
            (data["additionalDetails"]["namespace"], request.namespace),
            (data["resourceId"], path), (data["compartmentId"], request.scope.compartment_id),
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
        if if_match is not None:
            revisions = [value for key, value in headers.items() if key.lower() == "if-match"]
            if revisions != [[if_match]]:
                return None
        if content_sha256 is not None:
            digests = [value for key, value in headers.items() if key.lower() == "x-content-sha256"]
            if digests != [[content_sha256]]:
                return None
        event_id = require_text(event["id"], r"[!-~]{1,256}")
        event_time = require_text(event["time"], r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?Z")
        datetime.fromisoformat(event_time)
        source = require_text(event["source"], r"[!-~]{1,1024}")
        return AuditCandidate(event_id, event_time, event["type"], event["dataschema"], source)
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


class OciBucketAudit:
    def __init__(self, config, scope):
        try:
            self._client = bounded_client(config, scope, "loggingsearch")
            self._scope, self._principal = scope, config["user"]
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
        except Exception:
            raise AuditError(scan_reason="query") from None
        return self._observe(ack.request, operation, correlation, start, end)

    def reconcile_creation(self, request, start, end):
        """Confirm exact request success, not current identity or search coverage.

        Caller supplies retained original intent/window under exclusive control
        and fences its dispatcher. This never authorizes another CreateBucket.
        """
        try:
            digest = creation_digest_v1(request)
            if request.principal != self._principal:
                raise ValueError()
        except Exception:
            raise AuditError(scan_reason="query") from None
        scan = self._observe(request, "CreateBucket", request.correlation, start, end, content_sha256=digest)
        if len(scan.matches) != 1:
            return None
        return BucketCreationEvidence(request, digest)

    def observe_deletion(self, intent, receipt, start, end):
        """Observe an exact frozen request; never infer a deletion acknowledgement."""
        try:
            encode_intent(intent, receipt)
        except Exception:
            raise AuditError(scan_reason="query") from None
        return self._observe(intent.ack.request, "DeleteBucket", intent.correlation, start, end, intent.if_match)

    def reconcile_deletion(self, intent, receipt, start, end):
        """Confirm one dispatched request, never ownership or search completeness.

        Caller fences the original dispatcher and supplies independent durable
        receipt/intent readback. Empty/duplicate evidence never permits a retry.
        """
        scan = self.observe_deletion(intent, receipt, start, end)
        if len(scan.matches) != 1:
            return None
        return DeleteAcknowledgement(intent.ack.identity, receipt.removal_id)

    def _observe(self, request, operation, correlation, start, end, if_match=None, content_sha256=None):
        phase = "query"
        try:
            validate_request(request)
            require_text(correlation, r"[0-9a-f]{32}")
            if (request.scope != self._scope or type(operation) is not str
                    or operation not in {"CreateBucket", "DeleteBucket"}):
                raise AuditError(scan_reason=phase)
            for instant in (start, end):
                if (type(instant) is not datetime or instant.utcoffset() != timedelta(0)
                        or instant.second or instant.microsecond):
                    raise AuditError(scan_reason=phase)
            if not timedelta(0) < end - start <= timedelta(hours=1):
                raise AuditError(scan_reason=phase)
            query = (f'search "{self._scope.compartment_id}/_Audit" | where '
                     "data.eventName = 'CreateBucket' or data.eventName = 'DeleteBucket'")
            details = SearchLogsDetails(time_start=start.astimezone(timezone.utc),
                                        time_end=end.astimezone(timezone.utc),
                                        search_query=query, is_return_field_info=False)
            matches, seen, page = [], set(), None
            with oci_operation():
                for _ in range(5):
                    phase = "request"
                    result = self._client.search_logs(
                        details, limit=10, retry_strategy=NoneRetryStrategy(),
                        **({} if page is None else {"page": page}),
                    )
                    phase = "response"
                    if (result.status != 200 or type(result.data) is not dict
                            or type(result.data.get("results")) is not list):
                        raise AuditError(scan_reason=phase)
                    rows = result.data["results"]
                    if len(rows) > 10:
                        raise AuditError(scan_reason="event_limit")
                    for row in rows:
                        if (type(row) is not dict or type(row.get("data")) is not dict
                                or type(row["data"].get("datetime")) is not int
                                or type(row["data"].get("logContent")) is not dict):
                            raise AuditError(scan_reason=phase)
                        candidate = _candidate(row["data"]["logContent"], request, operation, correlation,
                                               if_match, content_sha256)
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
