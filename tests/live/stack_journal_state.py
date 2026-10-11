"""Closed private state for one unapplied stack-journal developer fixture."""
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
from uuid import uuid4

from troubleshooter_deployment.cleanup import prepare_purge
from troubleshooter_deployment.codec import decode, encode, validate_scope
from troubleshooter_deployment.oci_stack_bootstrap import InitialStackAcknowledgement
from troubleshooter_deployment.oci_transport import strong_etag
from troubleshooter_deployment.policy import bootstrap
from troubleshooter_deployment.records import Attempt, Scope, StackIdentity
from troubleshooter_deployment.recovery_bucket import require_text
from troubleshooter_deployment.stack_bootstrap import build_initial_stack
from .proof_state import ProofStateError

ATTEMPT = Attempt(1, 1, "0" * 40, "0" * 40)
PHASES = ("new", "stack_uncertain", "anchor_ready", "journal_uncertain", "ready",
          "purge_uncertain", "purge_ready", "delete_uncertain", "complete")
FIELDS = {"schema", "scope", "principal", "created_at", "request_sha256", "phase",
          "stack_id", "stack_etag", "record", "revision", "cas", "removal"}


def initial_request(state):
    return build_initial_stack(Scope(**state["scope"]), ATTEMPT, state["principal"])


def anchor(state):
    return InitialStackAcknowledgement(initial_request(state), state["stack_id"], state["stack_etag"])


def new_state(scope, principal):
    state = {"schema": 1, "scope": asdict(scope), "principal": principal,
             "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "request_sha256": None, "phase": "new", "stack_id": None, "stack_etag": None,
             "record": None, "revision": None, "cas": "new", "removal": uuid4().hex}
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
        if scope.repository_id != 1 or not scope.environment.startswith("stack-journal-proof-"):
            raise ProofStateError()
        require_text(state["created_at"], r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
        datetime.fromisoformat(state["created_at"])
        require_text(state["removal"], r"[0-9a-f]{32}")
        if state["request_sha256"] != sha256(initial_request(state).body).hexdigest():
            raise ProofStateError()
        index = PHASES.index(state["phase"])
        for key, first in (("stack_id", "anchor_ready"), ("stack_etag", "anchor_ready"),
                           ("record", "journal_uncertain"), ("revision", "ready")):
            if (state[key] is not None) != (index >= PHASES.index(first)):
                raise ProofStateError()
        if state["stack_id"] is not None:
            require_text(state["stack_id"], r"[A-Za-z0-9_.:-]{1,256}")
            strong_etag(state["stack_etag"])
        if state["record"] is not None:
            record = decode(state["record"].encode("ascii"))
            if (type(record.identity) is not StackIdentity or record.identity.scope != scope
                    or record.identity.stack_id != state["stack_id"]):
                raise ProofStateError()
            expected = bootstrap(record.identity, record.identity, ATTEMPT)
            if index >= PHASES.index("purge_uncertain"):
                expected = prepare_purge(expected, state["removal"])
            if record != expected or encode(expected).decode("ascii") != state["record"]:
                raise ProofStateError()
        if state["revision"] is not None:
            strong_etag(state["revision"])
        if (state["cas"] not in ("new", "uncertain", "confirmed")
                or (index < PHASES.index("ready") and state["cas"] != "new")
                or (index > PHASES.index("ready") and state["cas"] != "confirmed")):
            raise ProofStateError()
    except Exception:
        raise ProofStateError() from None
