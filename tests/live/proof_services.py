"""Bind the opt-in experiment to production adapters; no orchestration on import."""
from contextlib import ExitStack

from troubleshooter_deployment.oci_audit import OciBucketAudit
from troubleshooter_deployment.oci_receipt import OciReceiptStore
from troubleshooter_deployment.oci_recovery_bucket import OciRecoveryBucket, BucketError
from .proof_stack import ProofStack


class ProofServices:
    def __init__(self, config, scope):
        self.config, self.scope = config, scope
        self.resources = ExitStack()

    def __enter__(self):
        try:
            self.bucket = OciRecoveryBucket(self.config, self.scope)
            self.resources.callback(self.bucket.close)
            self.stack = ProofStack(self.config, self.scope)
            self.resources.callback(self.stack.close)
            self.reader = OciBucketAudit(self.config, self.scope)
            self.resources.callback(self.reader.close)
            return self
        except Exception:
            self.resources.close()
            raise

    def __exit__(self, *args):
        self.resources.close()

    def create_stack(self, state):
        return self.stack.create(state)

    def check_stack(self, state):
        self.stack.check(state)

    def delete_stack(self, state):
        self.stack.delete(state)

    def create_bucket(self, request):
        return self.bucket.create(request)

    def check_bucket(self, ack):
        return self.bucket.read(ack)

    def prepare_deletion(self, state, ack, record, receipt):
        store = OciReceiptStore(self.config, ack.identity, state["stack_id"])
        try:
            intent = store.read_deletion(receipt)
            if intent is None:
                intent = self.bucket.prepare_delete(ack, record, receipt, state["delete_correlation"])
                intent = store.persist_deletion(record, intent)
            if intent.ack != ack or intent.correlation != state["delete_correlation"]:
                raise BucketError()
            return intent
        finally:
            store.close()

    def delete_bucket(self, ack, record, receipt, intent):
        return self.bucket.delete_empty(ack, record, receipt, intent)

    def persist(self, state, record):
        store = OciReceiptStore(self.config, record.identity, state["stack_id"])
        try:
            return store.persist(record)
        finally:
            store.close()

    def read_receipt(self, state, ack):
        store = OciReceiptStore(self.config, ack.identity, state["stack_id"])
        try:
            return store.read()
        finally:
            store.close()

    def audit(self, ack, operation, correlation, start, end):
        return self.reader.observe(ack, operation, correlation, start, end)

    def audit_deletion(self, state, ack, receipt, start, end):
        store = OciReceiptStore(self.config, ack.identity, state["stack_id"])
        try:
            intent = store.read_deletion(receipt)
            if intent is None or intent.ack != ack or intent.correlation != state["delete_correlation"]:
                raise BucketError()
        finally:
            store.close()
        return self.reader.observe_deletion(intent, receipt, start, end)
