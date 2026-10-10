"""Private, bounded developer-probe state with local process exclusion."""
from dataclasses import asdict
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import stat
from uuid import uuid4

from troubleshooter_deployment.codec import decode, validate_scope
from troubleshooter_deployment.records import Scope
from troubleshooter_deployment.recovery_bucket import BucketRequest, require_text, validate_request

MAX_STATE = 65536
RESOURCE_STATES = {"new", "uncertain", "acknowledged", "delete_uncertain", "deleted"}


class ProofStateError(RuntimeError):
    def __init__(self):
        super().__init__("proof_state_unavailable_invalid_or_locked")


def new_state(scope, principal, namespace):
    from .proof_stack import ZIP_SHA256
    epoch = uuid4().hex
    state = {
        "schema": 1, "zip_sha256": ZIP_SHA256, "scope": asdict(scope), "principal": principal, "namespace": namespace,
        "epoch": epoch, "name": "recovery-proof-" + epoch, "stack_name": "recovery-proof-" + epoch,
        "stack_token": uuid4().hex, "create_correlation": uuid4().hex, "delete_correlation": uuid4().hex,
        "removal": uuid4().hex, "started": datetime.now(timezone.utc).replace(second=0, microsecond=0).isoformat(),
        "stack_status": "new", "stack_id": None, "bucket_status": "new", "bucket_id": None,
        "receipt_status": "new", "record": None, "audit_counts": None, "audit_window": None,
        "unknown_bucket_fields": [],
    }
    validate_state(state)
    return state


FIELDS = {
    "schema", "zip_sha256", "scope", "principal", "namespace", "epoch", "name", "stack_name", "stack_token",
    "create_correlation", "delete_correlation", "removal", "started", "stack_status", "stack_id",
    "bucket_status", "bucket_id", "receipt_status", "record", "audit_counts", "audit_window",
    "unknown_bucket_fields",
}


def bucket_request(state):
    return BucketRequest(Scope(**state["scope"]), state["namespace"], state["name"],
                         state["epoch"], state["principal"], state["create_correlation"])


def validate_state(state):
    try:
        from .proof_stack import ZIP_SHA256
        if type(state) is not dict or set(state) != FIELDS or type(state["schema"]) is not int or state["schema"] != 1:
            raise ProofStateError()
        if state["zip_sha256"] != ZIP_SHA256:
            raise ProofStateError()
        validate_scope(Scope(**state["scope"]))
        validate_request(bucket_request(state))
        for key in ("stack_token", "delete_correlation", "removal"):
            require_text(state[key], r"[0-9a-f]{32}")
        if (state["create_correlation"] == state["delete_correlation"]
                or state["name"] != "recovery-proof-" + state["epoch"]
                or state["stack_name"] != state["name"]):
            raise ProofStateError()
        require_text(state["started"], r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:00\+00:00")
        datetime.fromisoformat(state["started"])
        for kind in ("stack", "bucket"):
            status, resource_id = state[kind + "_status"], state[kind + "_id"]
            if type(status) is not str or status not in RESOURCE_STATES:
                raise ProofStateError()
            if status in {"new", "uncertain"}:
                if resource_id is not None:
                    raise ProofStateError()
            else:
                require_text(resource_id, r"[A-Za-z0-9_.:-]{1,256}")
        if state["bucket_status"] != "new" and state["stack_id"] is None:
            raise ProofStateError()
        if state["stack_status"] in {"delete_uncertain", "deleted"} and state["bucket_status"] not in {"new", "deleted"}:
            raise ProofStateError()
        if state["receipt_status"] not in {"new", "uncertain", "confirmed", "survived"}:
            raise ProofStateError()
        if state["record"] is None:
            if state["receipt_status"] != "new" or state["bucket_status"] in {"delete_uncertain", "deleted"}:
                raise ProofStateError()
        else:
            require_text(state["record"], r"[ -~]{1,16000}")
            record = decode(state["record"].encode())
            if (record.identity.scope != Scope(**state["scope"]) or record.identity.bucket_id != state["bucket_id"]
                    or record.identity.epoch != state["epoch"] or record.receipt is None
                    or record.receipt.removal_id != state["removal"] or record.generations or record.operations):
                raise ProofStateError()
        if state["receipt_status"] == "survived" and state["bucket_status"] != "deleted":
            raise ProofStateError()
        counts = state["audit_counts"]
        if counts is not None and (type(counts) is not list or len(counts) != 2
                                  or any(type(n) is not int or not 0 <= n <= 1024 for n in counts)):
            raise ProofStateError()
        window = state["audit_window"]
        if (counts is None) != (window is None):
            raise ProofStateError()
        if window is not None:
            from datetime import timedelta
            if type(window) is not list or len(window) != 2:
                raise ProofStateError()
            for instant in window:
                require_text(instant, r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:00\+00:00")
            if not timedelta(0) < datetime.fromisoformat(window[1]) - datetime.fromisoformat(window[0]) <= timedelta(hours=1):
                raise ProofStateError()
        names = state["unknown_bucket_fields"]
        if type(names) is not list or len(names) > 32:
            raise ProofStateError()
        for name in names:
            require_text(name, r"[A-Za-z][A-Za-z0-9_]{0,63}")
    except Exception:
        raise ProofStateError() from None


def _pairs(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            raise ProofStateError()
        data[key] = value
    return data


def _private(fd, directory=False):
    info = os.fstat(fd)
    expected = 0o700 if directory else 0o600
    if (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != expected
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or (not directory and info.st_nlink != 1)):
        raise ProofStateError()


def ensure_directory(path):
    """Make the directory entry durable before admitting any cloud dispatch."""
    path.mkdir(mode=0o700, exist_ok=True)
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


class ProofStore:
    def __init__(self, path, inherited_fd=None):
        self.path = Path(path)
        self.fd = self.lock_fd = None
        self.inherited_fd = inherited_fd

    def __enter__(self):
        try:
            if self.path.absolute() != self.path.resolve():
                raise ProofStateError()
            ensure_directory(self.path)
            self.fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            _private(self.fd, directory=True)
            self.lock_fd = os.open("lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
            _private(self.lock_fd)
            if self.inherited_fd is None:
                fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                inherited, opened = os.fstat(self.inherited_fd), os.fstat(self.lock_fd)
                if (inherited.st_dev, inherited.st_ino) != (opened.st_dev, opened.st_ino):
                    raise ProofStateError()
                os.close(self.lock_fd)
                self.lock_fd = os.dup(self.inherited_fd)
                fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise ProofStateError() from None

    def __exit__(self, *unused):
        # No LOCK_UN: parent and inherited child share the open-file lock.
        for fd in (self.lock_fd, self.fd):
            if fd is not None:
                os.close(fd)
        self.fd = self.lock_fd = None

    def read(self):
        try:
            try:
                fd = os.open("state.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
            except FileNotFoundError:
                return None
            with os.fdopen(fd, "rb") as file:
                _private(file.fileno())
                body = file.read(MAX_STATE + 1)
            if len(body) > MAX_STATE:
                raise ProofStateError()
            state = json.loads(body, object_pairs_hook=_pairs)
            validate_state(state)
            return state
        except Exception:
            raise ProofStateError() from None

    def write(self, state):
        temporary = None
        try:
            validate_state(state)
            body = json.dumps(state, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")
            if len(body) > MAX_STATE:
                raise ProofStateError()
            temporary = "state-" + uuid4().hex + ".tmp"
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
            with os.fdopen(fd, "wb") as file:
                file.write(body)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, "state.json", src_dir_fd=self.fd, dst_dir_fd=self.fd)
            temporary = None
            os.fsync(self.fd)
        except Exception:
            raise ProofStateError() from None
        finally:
            if temporary is not None:
                os.unlink(temporary, dir_fd=self.fd)
