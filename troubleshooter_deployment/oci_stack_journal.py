"""Runner-only journal on an already confirmed, exclusively controlled RM stack.

No stack discovery, creation, deletion or jobs. Initial configuration migration
requires retained original provenance and no workload update before enrollment.
"""
from hashlib import sha256
import json

from oci.exceptions import ServiceError
from oci.resource_manager.models import UpdateStackDetails, UpdateZipUploadConfigSourceDetails
from oci.retry import NoneRetryStrategy

from .bucket_bootstrap import read_bootstrap
from .bucket_deletion import read_deletion
from .codec import MAX_BYTES, decode, encode
from .journal import WriteConflict, WriteUncertain
from .oci_client import bounded_client
from .oci_owned_stack import OwnedStackObservation
from .oci_stack_bootstrap import InitialStackAcknowledgement
from .oci_transport import oci_operation, strong_etag
from .policy import bootstrap, require_forward_transition
from .receipt_variables import read_receipt
from .records import StackIdentity
from .recovery_bucket import require_text
from .stack_bootstrap import BOOTSTRAP_VARIABLE, validate_initial_stack
from .stack_journal_config import declaration_zip
from .stack_journal_variables import merge_journal, read_journal

PROTOCOL_TAG = "troubleshooter_journal_protocol"


class StackJournalError(RuntimeError):
    def __init__(self):
        super().__init__("stack_journal_unavailable_or_mismatched")


def _wire_limit(value):
    if len(json.dumps(value, allow_nan=False).encode("utf-8")) > MAX_BYTES:
        raise StackJournalError()


class OciStackJournalStore:
    def __init__(self, config, anchor):
        try:
            if type(anchor) not in (InitialStackAcknowledgement, OwnedStackObservation):
                raise StackJournalError()
            validate_initial_stack(anchor.request)
            require_text(anchor.stack_id, r"[A-Za-z0-9_.:-]{1,256}")
            strong_etag(anchor.etag)
            if config["user"] != anchor.request.principal:
                raise StackJournalError()
            seed = json.dumps({"protocol": "stack-journal-v2", "stack": anchor.stack_id,
                               "initial_sha256": sha256(anchor.request.body).hexdigest()},
                              sort_keys=True, separators=(",", ":")).encode("ascii")
            self.identity = StackIdentity(anchor.request.scope, anchor.stack_id, sha256(seed).hexdigest()[:32])
            self._client = bounded_client(config, anchor.request.scope, "resourcemanager")
        except Exception:
            raise StackJournalError() from None
        self._anchor = anchor

    def close(self):
        try:
            self._client.base_client.session.close()
        except Exception:
            raise StackJournalError() from None

    def _observed(self, result):
        data = result.data
        expected = json.loads(self._anchor.request.body)
        if (result.status != 200 or type(data) is not dict
                or data.get("id") != self.identity.stack_id or data.get("lifecycleState") != "ACTIVE"
                or data.get("compartmentId") != expected["compartmentId"]
                or data.get("displayName") != expected["displayName"]):
            raise StackJournalError()
        revision = strong_etag(result.headers.get("etag"))
        variables, tags = data.get("variables"), data.get("freeformTags")
        body = read_journal(variables)
        # Also enforces provider tag bounds without altering unrelated tags.
        if read_bootstrap(self._anchor.request, self.identity.stack_id, tags, data.get("definedTags", {})) is not None:
            raise StackJournalError()
        if (variables.get(BOOTSTRAP_VARIABLE) != expected["variables"][BOOTSTRAP_VARIABLE]
                or any(tags.get(k) != v for k, v in expected["freeformTags"].items())
                or read_receipt(variables) is not None or read_deletion(variables) is not None):
            raise StackJournalError()
        markers = {k: v for k, v in tags.items() if k.startswith("troubleshooter_journal_")}
        if markers != ({} if body is None else {PROTOCOL_TAG: "stack-journal-v2"}):
            raise StackJournalError()
        if body is not None and decode(body).identity != self.identity:
            raise StackJournalError()
        return revision, body, data

    def _get(self):
        revision, body, data = self._observed(self._client.get_stack(
            self.identity.stack_id, retry_strategy=NoneRetryStrategy()))
        if body is None:
            source = data.get("configSource")
            if (data.get("terraformVersion") != "1.5.x" or type(source) is not dict
                    or source.get("configSourceType") != "ZIP_UPLOAD"
                    or source.get("workingDirectory") not in (None, "")):
                raise StackJournalError()
            jobs = self._client.list_jobs(stack_id=self.identity.stack_id, limit=1, retry_strategy=NoneRetryStrategy())
            if jobs.status != 200 or type(jobs.data) is not list or jobs.data or "opc-next-page" in jobs.headers:
                raise StackJournalError()
        return revision, body, data

    def read(self):
        try:
            with oci_operation():
                revision, body, _ = self._get()
            return None if body is None else (revision, body)
        except Exception:
            raise StackJournalError() from None

    def write(self, expected, body):
        dispatched = False
        try:
            record = decode(body)
            if record.identity != self.identity or encode(record) != body:
                raise StackJournalError()
            with oci_operation() as check_deadline:
                revision, previous, before = self._get()
                if expected is None:
                    if previous is not None:
                        raise WriteConflict()
                    if record != bootstrap(self.identity, self.identity, self._anchor.request.attempt):
                        raise StackJournalError()
                else:
                    if strong_etag(expected) != revision or previous is None:
                        raise WriteConflict()
                    require_forward_transition(decode(previous), record)
                variables = merge_journal(before["variables"], body)
                payload = {"variables": variables}
                projected = {**before, "variables": variables}
                details = UpdateStackDetails(variables=variables)
                if previous is None:
                    tags = {**before["freeformTags"], PROTOCOL_TAG: "stack-journal-v2"}
                    read_bootstrap(self._anchor.request, self.identity.stack_id, tags, before.get("definedTags", {}))
                    config = {"configSourceType": "ZIP_UPLOAD", "zipFileBase64Encoded": declaration_zip()}
                    payload.update(freeformTags=tags, configSource=config)
                    projected.update(freeformTags=tags, configSource=config)
                    details.freeform_tags = tags
                    details.config_source = UpdateZipUploadConfigSourceDetails(zip_file_base64_encoded=config["zipFileBase64Encoded"])
                _wire_limit(payload)
                _wire_limit(projected)
                check_deadline()
                dispatched = True
                try:
                    result = self._client.update_stack(self.identity.stack_id, details,
                        if_match=revision, retry_strategy=NoneRetryStrategy())
                except ServiceError as error:
                    if error.status == 412:
                        raise WriteConflict() from None
                    raise
                _, acknowledged, _ = self._observed(result)
                if acknowledged != body:
                    raise WriteUncertain()
                check_deadline()
                saved_revision, saved, after = self._get()
                if previous is None:
                    source = after.get("configSource")
                    if (type(source) is not dict or source.get("configSourceType") != "ZIP_UPLOAD"
                            or source.get("workingDirectory") not in (None, "")):
                        raise WriteUncertain()
                preserved = ("definedTags", "terraformVersion") if previous is None else (
                    "definedTags", "terraformVersion", "configSource")
                if (saved != body or after["variables"] != variables
                        or after["freeformTags"] != payload.get("freeformTags", before["freeformTags"])
                        or any(after.get(k) != before.get(k) for k in preserved)):
                    raise WriteUncertain()
            return saved_revision
        except WriteConflict:
            raise
        except Exception:
            if dispatched:
                raise WriteUncertain() from None
            raise StackJournalError() from None
