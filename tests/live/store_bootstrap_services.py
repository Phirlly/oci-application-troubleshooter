"""One isolated fixture using production adapters; no retries or runner adoption."""
from contextlib import ExitStack
from dataclasses import replace
from hashlib import sha256
import json

from oci.exceptions import ServiceError
from oci.resource_manager.models import UpdateStackDetails
from oci.retry import NoneRetryStrategy

from troubleshooter_deployment.bucket_bootstrap import acknowledge, require_dispatch_window
from troubleshooter_deployment.bucket_deletion import read_deletion
from troubleshooter_deployment.cleanup import prepare_purge
from troubleshooter_deployment.codec import encode
from troubleshooter_deployment.journal import Journal
from troubleshooter_deployment.oci_bucket_bootstrap import OciBucketBootstrapStore
from troubleshooter_deployment.oci_client import bounded_client
from troubleshooter_deployment.oci_journal import OciJournalStore
from troubleshooter_deployment.oci_receipt import OciReceiptStore
from troubleshooter_deployment.oci_recovery_bucket import OciRecoveryBucket
from troubleshooter_deployment.oci_stack_bootstrap import OciStackBootstrap
from troubleshooter_deployment.oci_transport import oci_operation, strong_etag
from troubleshooter_deployment.receipt_variables import read_receipt
from troubleshooter_deployment.records import Record
from .store_bootstrap_state import (ATTEMPT, anchor, bucket_ack, initial_request, intent,
                                    ready_marker, validate_state)


class BootstrapProof:
    def __init__(self, config, store, state):
        validate_state(state)
        if config["user"] != state["principal"]:
            raise ValueError("proof_principal_mismatch")
        self.config, self.store, self.state = config, store, state
        self.resources = ExitStack()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.resources.close()

    def _adapter(self, adapter, *args):
        result = adapter(self.config, *args)
        self.resources.callback(result.close)
        return result

    def _client(self, service):
        result = bounded_client(self.config, initial_request(self.state).scope, service)
        self.resources.callback(result.base_client.session.close)
        return result

    def _save(self, **changes):
        updated = {**self.state, **changes}
        self.store.write(updated)
        self.state = updated

    def _parts(self):
        ack = bucket_ack(self.state)
        bucket = self._adapter(OciRecoveryBucket, ack.identity.scope)
        carrier = self._adapter(OciBucketBootstrapStore, anchor(self.state))
        storage = self._adapter(OciJournalStore, ack.identity, ack.request.namespace, ack.request.name)
        return ack, bucket, carrier, Journal(storage, ack.identity, ATTEMPT)

    def _exact(self, journal):
        saved = journal.load()
        if (encode(saved.record).decode() != self.state["record"]
                or saved.revision != self.state["journal_revision"]):
            raise ValueError("proof_journal_changed")
        return saved

    def run(self):
        if self.state["phase"] == "ready" and self.state["cas_status"] == "confirmed":
            return self.observe()
        if self.state["phase"] != "new":
            return False
        initial = initial_request(self.state)
        stack = self._adapter(OciStackBootstrap, initial)
        stack.check_name_available()
        self._save(phase="stack_uncertain")
        owned = stack.create(self.state["created_at"])
        self._save(phase="stack_ready", stack_id=owned.stack_id, stack_etag=owned.etag)
        carrier = self._adapter(OciBucketBootstrapStore, owned)
        pending = intent(self.state)
        observed = carrier.read()
        if observed.record is not None:
            raise ValueError("proof_stack_already_enrolled")
        saved = carrier.persist(observed, pending)
        from time import time
        require_dispatch_window(pending, time())
        bucket = self._adapter(OciRecoveryBucket, initial.scope)
        self._save(phase="bucket_uncertain")
        ack = bucket.create(pending.request)
        self._save(phase="bucket_ready", bucket_id=ack.identity.bucket_id)
        bucket.read(ack)
        initializing = acknowledge(pending, ack)
        saved = carrier.persist(saved, initializing)
        storage = self._adapter(OciJournalStore, ack.identity, ack.request.namespace, ack.request.name)
        journal = Journal(storage, ack.identity, ATTEMPT)
        self._save(phase="journal_uncertain", record=encode(Record(ack.identity, ATTEMPT)).decode())
        initialized = journal.initialize(ack.identity)
        independently_read = journal.load()
        if independently_read != initialized:
            raise ValueError("proof_initialization_changed")
        carrier.persist(saved, replace(initializing, phase="ready"))
        self._save(phase="ready", journal_revision=initialized.revision)
        self._cas_probe()
        return True

    def _cas_probe(self):
        client = self._client("resourcemanager")
        with oci_operation():
            before = client.get_stack(self.state["stack_id"], retry_strategy=NoneRetryStrategy())
            if before.status != 200 or strong_etag(before.headers.get("etag")) == self.state["stack_etag"]:
                raise ValueError("proof_cas_revision_unchanged")
            self._save(cas_status="uncertain")
            try:
                client.update_stack(self.state["stack_id"], UpdateStackDetails(freeform_tags=before.data["freeformTags"]),
                    if_match=self.state["stack_etag"], retry_strategy=NoneRetryStrategy())
            except ServiceError as error:
                if error.status != 412:
                    raise
            else:
                raise ValueError("proof_stale_revision_accepted")
            after = client.get_stack(self.state["stack_id"], retry_strategy=NoneRetryStrategy())
            if after.status != 200 or after.data != before.data or after.headers.get("etag") != before.headers.get("etag"):
                raise ValueError("proof_cas_changed_stack")
        self._save(cas_status="confirmed")

    def observe(self):
        if self.state["phase"] != "ready" or self.state["cas_status"] != "confirmed":
            return False
        ack, bucket, carrier, journal = self._parts()
        if carrier.read().record != ready_marker(self.state):
            raise ValueError("proof_carrier_changed")
        bucket.read(ack)
        self._exact(journal)
        return True

    def cleanup(self):
        if self.state["phase"] == "complete":
            return True
        if self.state["phase"] != "ready" or self.state["cas_status"] != "confirmed":
            return False
        # One finite path. Any interruption retains its phase for investigation;
        # this test launcher never retries an uncertain create/write/delete.
        ack, bucket, carrier, journal = self._parts()
        if carrier.read().record != ready_marker(self.state):
            raise ValueError("proof_carrier_changed")
        bucket.read(ack)
        before = self._exact(journal)
        record = prepare_purge(before.record, self.state["removal"])
        self._save(phase="purge_uncertain", record=encode(record).decode())
        updated = journal.commit(before, record)
        if journal.load() != updated:
            raise ValueError("proof_purge_changed")
        self._save(phase="purge_ready", journal_revision=updated.revision)
        receipts = self._adapter(OciReceiptStore, ack.identity, self.state["stack_id"])
        if receipts.persist(record) != record.receipt or receipts.read() != record.receipt:
            raise ValueError("proof_receipt_mismatch")
        self._exact(journal)
        frozen = {"name": "journal/" + ack.identity.epoch + ".json", "revision": updated.revision,
                  "sha256": sha256(encode(record)).hexdigest()}
        objects = self._client("objectstorage")
        self._save(phase="object_delete_uncertain", object_delete=frozen)
        with oci_operation():
            deleted = objects.delete_object(ack.request.namespace, ack.request.name, frozen["name"],
                if_match=frozen["revision"], retry_strategy=NoneRetryStrategy())
            if deleted.status != 204:
                raise ValueError("proof_object_deletion_unknown")
        self._save(phase="object_deleted")
        deletion = bucket.prepare_delete(ack, record, receipts.read(), self.state["delete_correlation"])
        if receipts.persist_deletion(record, deletion) != deletion or receipts.read_deletion(record.receipt) != deletion:
            raise ValueError("proof_delete_intent_mismatch")
        self._save(phase="bucket_delete_uncertain")
        bucket.delete_empty(ack, record, receipts.read(), deletion)
        self._save(phase="bucket_deleted")
        if receipts.read() != record.receipt:
            raise ValueError("proof_receipt_did_not_survive")
        self._delete_stack(record, deletion)
        self._save(phase="complete")
        return True

    def _delete_stack(self, record, deletion):
        client = self._client("resourcemanager")
        with oci_operation():
            observed = client.get_stack(self.state["stack_id"], retry_strategy=NoneRetryStrategy())
            data, expected = observed.data, json.loads(initial_request(self.state).body)
            if (observed.status != 200 or type(data) is not dict or data.get("id") != self.state["stack_id"]
                    or data.get("lifecycleState") != "ACTIVE"
                    or any(data.get(k) != expected[k] for k in ("compartmentId", "displayName", "terraformVersion"))):
                raise ValueError("proof_stack_mismatch")
            for field in ("variables", "freeformTags"):
                if type(data.get(field)) is not dict or any(data[field].get(k) != v for k, v in expected[field].items()):
                    raise ValueError("proof_stack_metadata_mismatch")
            source = data.get("configSource")
            if (type(source) is not dict or source.get("configSourceType") != "ZIP_UPLOAD"
                    or source.get("workingDirectory") not in (None, "")):
                raise ValueError("proof_stack_source_changed")
            from troubleshooter_deployment.bucket_bootstrap import read_bootstrap
            if (read_bootstrap(initial_request(self.state), self.state["stack_id"], data["freeformTags"],
                               data.get("definedTags", {})) != ready_marker(self.state)
                    or read_receipt(data["variables"]) != record.receipt or read_deletion(data["variables"]) != deletion):
                raise ValueError("proof_stack_receipt_changed")
            revision = strong_etag(observed.headers.get("etag"))
            jobs = client.list_jobs(stack_id=self.state["stack_id"], limit=1, retry_strategy=NoneRetryStrategy())
            if jobs.status != 200 or type(jobs.data) is not list or jobs.data or "opc-next-page" in jobs.headers:
                raise ValueError("proof_stack_has_jobs")
            self._save(phase="stack_delete_uncertain")
            result = client.delete_stack(self.state["stack_id"], if_match=revision, retry_strategy=NoneRetryStrategy())
            if result.status != 204:
                raise ValueError("proof_stack_deletion_unknown")
