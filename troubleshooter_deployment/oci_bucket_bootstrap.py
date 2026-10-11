"""Conditional bucket bootstrap tags on a positively owned Resource Manager stack."""
from dataclasses import dataclass
import json

from oci.exceptions import ServiceError
from oci.resource_manager.models import UpdateStackDetails
from oci.retry import NoneRetryStrategy

from .bucket_bootstrap import BucketBootstrap, merge_bootstrap, read_bootstrap
from .bucket_deletion import read_deletion
from .oci_client import bounded_client
from .oci_owned_stack import OwnedStackObservation
from .oci_stack_bootstrap import InitialStackAcknowledgement
from .oci_transport import oci_operation, strong_etag
from .receipt_variables import read_receipt
from .recovery_bucket import require_text
from .request_budget import BudgetError
from .stack_bootstrap import BOOTSTRAP_VARIABLE, validate_initial_stack


class BootstrapStoreError(RuntimeError):
    def __init__(self):
        super().__init__("bucket_bootstrap_unavailable_or_mismatched")


@dataclass(frozen=True, repr=False)
class BootstrapSnapshot:
    record: BucketBootstrap | None
    revision: str


class OciBucketBootstrapStore:
    def __init__(self, config, anchor):
        try:
            if type(anchor) not in (InitialStackAcknowledgement, OwnedStackObservation):
                raise BootstrapStoreError()
            validate_initial_stack(anchor.request)
            require_text(anchor.stack_id, r"[A-Za-z0-9_.:-]{1,256}")
            strong_etag(anchor.etag)
            if config["user"] != anchor.request.principal:
                raise BootstrapStoreError()
            self._client = bounded_client(config, anchor.request.scope, "resourcemanager")
        except Exception:
            raise BootstrapStoreError() from None
        self._anchor = anchor

    def close(self):
        try:
            self._client.base_client.session.close()
        except Exception:
            raise BootstrapStoreError() from None

    def _get(self):
        anchor = self._anchor
        result = self._client.get_stack(anchor.stack_id, retry_strategy=NoneRetryStrategy())
        data = result.data
        expected = json.loads(anchor.request.body)
        if (result.status != 200 or type(data) is not dict or data.get("id") != anchor.stack_id
                or data.get("compartmentId") != expected["compartmentId"]
                or data.get("displayName") != expected["displayName"] or data.get("lifecycleState") != "ACTIVE"):
            raise BootstrapStoreError()
        for field in ("variables", "freeformTags"):
            values = data.get(field)
            if (type(values) is not dict
                    or any(type(k) is not str or not k or type(v) is not str for k, v in values.items())):
                raise BootstrapStoreError()
        if (data["variables"].get(BOOTSTRAP_VARIABLE) != expected["variables"][BOOTSTRAP_VARIABLE]
                or any(data["freeformTags"].get(k) != v for k, v in expected["freeformTags"].items())):
            raise BootstrapStoreError()
        if any(k.startswith("troubleshooter_journal_")
               for mapping in ("variables", "freeformTags") for k in data[mapping]):
            raise BootstrapStoreError()
        record = read_bootstrap(anchor.request, anchor.stack_id, data["freeformTags"], data.get("definedTags", {}))
        revision = strong_etag(result.headers.get("etag"))
        if record is None or record.phase != "ready":
            if read_receipt(data["variables"]) is not None or read_deletion(data["variables"]) is not None:
                raise BootstrapStoreError()
            jobs = self._client.list_jobs(stack_id=anchor.stack_id, limit=1, retry_strategy=NoneRetryStrategy())
            if jobs.status != 200 or type(jobs.data) is not list or jobs.data or "opc-next-page" in jobs.headers:
                raise BootstrapStoreError()
        return BootstrapSnapshot(record, revision), data

    def read(self):
        try:
            with oci_operation():
                result, _ = self._get()
            return result
        except Exception:
            raise BootstrapStoreError() from None

    def persist(self, expected, record):
        """One CAS; independent exact readback confirms success or a lost reply.

        Explicit412 never grants the enrolling invocation dispatch authority.
        The caller must not use an already-present marker as dispatch permission.
        """
        try:
            with oci_operation() as check_deadline:
                current, before = self._get()
                if type(expected) is not BootstrapSnapshot or current != expected:
                    raise BootstrapStoreError()
                tags = merge_bootstrap(self._anchor.request, self._anchor.stack_id,
                    before["freeformTags"], before.get("definedTags", {}), record)
                try:
                    self._client.update_stack(self._anchor.stack_id, UpdateStackDetails(freeform_tags=tags),
                        if_match=current.revision, retry_strategy=NoneRetryStrategy())
                except BudgetError:
                    raise
                except ServiceError as error:
                    if error.status == 412:
                        raise BootstrapStoreError() from None
                except Exception:
                    pass  # Unknown dispatch outcome; only the next exact GET can confirm it.
                # A lower layer may wrap the alarm. Do not start readback after expiry.
                check_deadline()
                saved, after = self._get()
                if (saved.record != record or after["freeformTags"] != tags
                        or any(after.get(k) != before.get(k)
                               for k in ("variables", "definedTags", "configSource", "terraformVersion"))):
                    raise BootstrapStoreError()
            return saved
        except Exception:
            raise BootstrapStoreError() from None
