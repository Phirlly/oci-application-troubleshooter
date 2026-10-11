"""Private, closed state for one opt-in recovery-store fixture, never GitHub state."""
from dataclasses import asdict, replace
from datetime import datetime, timezone
from hashlib import sha256
from uuid import uuid4

from troubleshooter_deployment.bucket_bootstrap import acknowledge, acknowledgement, enroll
from troubleshooter_deployment.cleanup import prepare_purge
from troubleshooter_deployment.codec import decode, encode, validate_scope
from troubleshooter_deployment.oci_stack_bootstrap import InitialStackAcknowledgement
from troubleshooter_deployment.oci_transport import strong_etag
from troubleshooter_deployment.records import Attempt, Identity, Record, Scope
from troubleshooter_deployment.recovery_bucket import BucketAcknowledgement, require_text
from troubleshooter_deployment.stack_bootstrap import build_initial_stack
from .proof_state import ProofStateError

PHASES = ("new", "stack_uncertain", "stack_ready", "bucket_uncertain", "bucket_ready",
          "journal_uncertain", "ready", "purge_uncertain", "purge_ready",
          "object_delete_uncertain", "object_deleted", "bucket_delete_uncertain",
          "bucket_deleted", "stack_delete_uncertain", "complete")
FIELDS = {"schema", "phase", "scope", "principal", "namespace", "created_at", "request_sha256",
          "stack_id", "stack_etag", "bucket_id", "record", "journal_revision", "cas_status",
          "removal", "delete_correlation", "object_delete"}
# This proof does not represent a real GitHub workflow/run or completed attempt.
ATTEMPT = Attempt(1, 1, "0" * 40, "0" * 40)


def initial_request(state):
    return build_initial_stack(Scope(**state["scope"]), ATTEMPT, state["principal"])


def anchor(state):
    return InitialStackAcknowledgement(initial_request(state), state["stack_id"], state["stack_etag"])


def intent(state):
    return enroll(initial_request(state), state["stack_id"], state["namespace"], ATTEMPT,
                  datetime.fromisoformat(state["created_at"]).timestamp())


def bucket_ack(state):
    request = intent(state).request
    return BucketAcknowledgement(request, Identity(request.scope, state["bucket_id"], request.epoch))


def ready_marker(state):
    return replace(acknowledge(intent(state), bucket_ack(state)), phase="ready")


def new_state(scope, principal, namespace):
    state = {"schema": 1, "phase": "new", "scope": asdict(scope), "principal": principal,
             "namespace": namespace, "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "request_sha256": None, "stack_id": None, "stack_etag": None, "bucket_id": None,
             "record": None, "journal_revision": None, "cas_status": "new", "removal": uuid4().hex,
             "delete_correlation": uuid4().hex, "object_delete": None}
    state["request_sha256"] = sha256(initial_request(state).body).hexdigest()
    validate_state(state)
    return state


def validate_state(state):
    try:
        if (type(state) is not dict or set(state) != FIELDS or type(state["schema"]) is not int
                or state["schema"] != 1 or type(state["phase"]) is not str or state["phase"] not in PHASES):
            raise ProofStateError()
        scope = Scope(**state["scope"])
        validate_scope(scope)
        if scope.repository_id != 1 or not scope.environment.startswith("store-proof-"):
            raise ProofStateError()
        require_text(state["namespace"], r"[A-Za-z0-9_-]{1,256}")
        require_text(state["created_at"], r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
        datetime.fromisoformat(state["created_at"])
        if state["request_sha256"] != sha256(initial_request(state).body).hexdigest():
            raise ProofStateError()
        for field in ("removal", "delete_correlation"):
            require_text(state[field], r"[0-9a-f]{32}")
        index = PHASES.index(state["phase"])
        for field, first in (("stack_id", "stack_ready"), ("stack_etag", "stack_ready"),
                             ("bucket_id", "bucket_ready"), ("record", "journal_uncertain"),
                             ("journal_revision", "ready"), ("object_delete", "object_delete_uncertain")):
            if (state[field] is not None) != (index >= PHASES.index(first)):
                raise ProofStateError()
        if state["stack_id"] is not None:
            require_text(state["stack_id"], r"[A-Za-z0-9_.:-]{1,256}")
            strong_etag(state["stack_etag"])
            if state["delete_correlation"] == intent(state).request.correlation:
                raise ProofStateError()
        if state["bucket_id"] is not None:
            acknowledgement(ready_marker(state))
        if state["record"] is not None:
            expected = Record(bucket_ack(state).identity, ATTEMPT)
            if index >= PHASES.index("purge_uncertain"):
                expected = prepare_purge(expected, state["removal"])
            if decode(state["record"].encode()) != expected or state["record"] != encode(expected).decode():
                raise ProofStateError()
        if state["journal_revision"] is not None:
            strong_etag(state["journal_revision"])
        if state["cas_status"] not in ("new", "uncertain", "confirmed"):
            raise ProofStateError()
        if index < PHASES.index("ready") and state["cas_status"] != "new":
            raise ProofStateError()
        if index > PHASES.index("ready") and state["cas_status"] != "confirmed":
            raise ProofStateError()
        if state["object_delete"] is not None:
            expected = {"name": "journal/" + bucket_ack(state).identity.epoch + ".json",
                        "revision": state["journal_revision"],
                        "sha256": sha256(state["record"].encode()).hexdigest()}
            if state["object_delete"] != expected:
                raise ProofStateError()
    except Exception:
        raise ProofStateError() from None
