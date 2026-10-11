"""Bounded live acceptance using production adapters and synthetic local attempt."""
from contextlib import ExitStack
import json

from oci.exceptions import ServiceError
from oci.resource_manager.models import UpdateStackDetails
from oci.retry import NoneRetryStrategy

from troubleshooter_deployment.cleanup import prepare_purge
from troubleshooter_deployment.codec import encode
from troubleshooter_deployment.journal import Journal
from troubleshooter_deployment.oci_client import bounded_client
from troubleshooter_deployment.oci_stack_bootstrap import OciStackBootstrap
from troubleshooter_deployment.oci_stack_journal import OciStackJournalStore
from troubleshooter_deployment.oci_transport import oci_operation, strong_etag
from troubleshooter_deployment.policy import bootstrap
from .stack_journal_state import ATTEMPT, anchor, initial_request, validate_state


class StackJournalProof:
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

    def _save(self, **changes):
        state = {**self.state, **changes}
        self.store.write(state)
        self.state = state

    def _journal(self):
        store = self._adapter(OciStackJournalStore, anchor(self.state))
        return store, Journal(store, store.identity, ATTEMPT)

    def _exact(self, journal):
        saved = journal.load()
        if encode(saved.record).decode("ascii") != self.state["record"] or saved.revision != self.state["revision"]:
            raise ValueError("proof_journal_changed")
        return saved

    def run_interrupted(self):
        if self.state["phase"] != "new":
            return False
        stack = self._adapter(OciStackBootstrap, initial_request(self.state))
        stack.check_name_available()
        self._save(phase="stack_uncertain")
        owned = stack.create(self.state["created_at"])
        self._save(phase="anchor_ready", stack_id=owned.stack_id, stack_etag=owned.etag)
        store, _ = self._journal()
        if store.read() is not None:
            raise ValueError("proof_journal_already_present")
        return True  # Controlled process stop before any journal PUT.

    def run(self):
        if self.state["phase"] != "anchor_ready":
            return False
        store, journal = self._journal()
        record = bootstrap(store.identity, store.identity, ATTEMPT)
        self._save(phase="journal_uncertain", record=encode(record).decode("ascii"))
        saved = journal.initialize(store.identity)
        self._save(phase="ready", revision=saved.revision)
        self._exact(journal)
        client = store._client  # Test-only raw conditional probe on the same bounded client.
        self._save(cas="uncertain")
        with oci_operation() as check_deadline:
            before = client.get_stack(self.state["stack_id"], retry_strategy=NoneRetryStrategy())
            if before.status != 200 or strong_etag(before.headers.get("etag")) == self.state["stack_etag"]:
                raise ValueError("proof_etag_not_changed")
            try:
                client.update_stack(self.state["stack_id"], UpdateStackDetails(variables=before.data["variables"]),
                    if_match=self.state["stack_etag"], retry_strategy=NoneRetryStrategy())
            except ServiceError as error:
                if error.status != 412:
                    raise
            else:
                raise ValueError("proof_stale_update_accepted")
            check_deadline()
            after = client.get_stack(self.state["stack_id"], retry_strategy=NoneRetryStrategy())
            if after.status != 200 or after.data != before.data or after.headers.get("etag") != before.headers.get("etag"):
                raise ValueError("proof_stale_update_changed_stack")
        self._save(cas="confirmed")
        return True

    def observe(self):
        if self.state["phase"] != "ready" or self.state["cas"] != "confirmed":
            return False
        _, journal = self._journal()
        self._exact(journal)
        return True

    def cleanup(self):
        if self.state["phase"] != "ready" or self.state["cas"] != "confirmed":
            return False
        store, journal = self._journal()
        before = self._exact(journal)
        closed = prepare_purge(before.record, self.state["removal"])
        self._save(phase="purge_uncertain", record=encode(closed).decode("ascii"))
        saved = journal.commit(before, closed)
        self._save(phase="purge_ready", revision=saved.revision)
        self._exact(journal)
        client = store._client
        with oci_operation() as check_deadline:
            result = client.get_stack(self.state["stack_id"], retry_strategy=NoneRetryStrategy())
            revision, body, data = store._observed(result)
            source = data.get("configSource")
            if (body != encode(closed) or revision != saved.revision or data.get("terraformVersion") != "1.5.x"
                    or type(source) is not dict or source.get("configSourceType") != "ZIP_UPLOAD"
                    or source.get("workingDirectory") not in (None, "")):
                raise ValueError("proof_stack_changed")
            jobs = client.list_jobs(stack_id=self.state["stack_id"], limit=1, retry_strategy=NoneRetryStrategy())
            if jobs.status != 200 or type(jobs.data) is not list or jobs.data or "opc-next-page" in jobs.headers:
                raise ValueError("proof_stack_has_jobs")
            self._save(phase="delete_uncertain")
            check_deadline()
            deleted = client.delete_stack(self.state["stack_id"], if_match=revision, retry_strategy=NoneRetryStrategy())
            if deleted.status != 204:
                raise ValueError("proof_stack_delete_unknown")
        self._save(phase="complete")
        return True
