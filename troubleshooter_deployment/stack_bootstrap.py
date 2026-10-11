"""Versioned, reproducible initial RM request; no secrets or workload resources.

Keep v1 bytes stable while any v1 commitment remains unresolved. Public GitHub
records contain hashes, not this private scope/principal/configuration body.
"""
from base64 import b64encode
from dataclasses import asdict, dataclass
from datetime import datetime
from hashlib import sha256
from io import BytesIO
import json
import re
from time import time
from zipfile import ZIP_STORED, ZipFile, ZipInfo

from .bootstrap_commitment import scope_digest
from .codec import validate_attempt
from .records import Attempt, Scope

BOOTSTRAP_VARIABLE = "troubleshooter_initial_stack_v1"
# Frozen v1 declarations, independent of future receipt implementation changes.
_VARIABLES = (
    "troubleshooter_bucket_delete_intent", "troubleshooter_cleanup_receipt_0",
    "troubleshooter_cleanup_receipt_1", "troubleshooter_cleanup_receipt_sha256",
    "troubleshooter_initial_stack_v1",
)


class InitialStackError(ValueError):
    def __init__(self):
        super().__init__("invalid_initial_stack_request_or_retry_window")


@dataclass(frozen=True, repr=False)
class InitialStackRequest:
    scope: Scope
    attempt: Attempt
    principal: str
    body: bytes
    token: str
    display_name: str


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")


def _root_zip():
    source = "\n".join(f'variable "{name}" {{\n  type    = string\n  default = ""\n}}\n' for name in _VARIABLES)
    stream = BytesIO()
    with ZipFile(stream, "w", compression=ZIP_STORED) as archive:
        entry = ZipInfo("main.tf", date_time=(2026, 1, 1, 0, 0, 0))
        entry.create_system, entry.external_attr = 3, 0o100644 << 16
        archive.writestr(entry, source.encode("ascii"))
    return b64encode(stream.getvalue()).decode("ascii")


def build_initial_stack(scope, attempt, principal):
    try:
        digest = scope_digest(scope)
        validate_attempt(attempt)
        if type(principal) is not str or re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", principal) is None:
            raise InitialStackError()
        token = sha256(_json({"protocol": "initial-stack-v1", "scope": digest, "run_id": attempt.run_id})).hexdigest()
        name = "troubleshooter-" + digest[:32]
        metadata = {"schema": 1, "scope": asdict(scope), "attempt": asdict(attempt), "principal": principal}
        body = _json({"compartmentId": scope.compartment_id, "displayName": name, "terraformVersion": "1.5.x",
                      "freeformTags": {"troubleshooter_protocol": "initial-stack-v1", "troubleshooter_scope": digest},
                      "variables": {BOOTSTRAP_VARIABLE: _json(metadata).decode("ascii")},
                      "configSource": {"configSourceType": "ZIP_UPLOAD", "zipFileBase64Encoded": _root_zip()}})
        return InitialStackRequest(scope, attempt, principal, body, token, name)
    except Exception:
        raise InitialStackError() from None


def validate_initial_stack(request):
    if (type(request) is not InitialStackRequest
            or request != build_initial_stack(request.scope, request.attempt, request.principal)):
        raise InitialStackError()


def require_retry_window(created_at):
    """Conservative elapsed wall time from the earliest confirmed checkpoint.

    Service tokens last at most24h and may invalidate earlier. Check again at
    dispatch; never replace a rejected/expired token. Trusted runner clock is
    required, with a one-hour margin for normal clock and request delay.
    """
    try:
        if type(created_at) is not str or re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", created_at) is None:
            raise InitialStackError()
        age = time() - datetime.fromisoformat(created_at).timestamp()
        if not 0 <= age < 23 * 3600:
            raise InitialStackError()
    except Exception:
        raise InitialStackError() from None
