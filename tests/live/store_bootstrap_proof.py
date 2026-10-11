"""Opt-in, supervised developer proof; never a presenter deployment command."""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys
from uuid import uuid4

from .proof_state import ProofStore, ensure_directory
from .recovery_proof import supervise

STATE_PATH = Path(__file__).resolve().parents[2] / ".cache" / "store-bootstrap-proof"


def parser():
    arguments = argparse.ArgumentParser(description=__doc__)
    arguments.add_argument("action", choices=("run", "observe", "cleanup"))
    arguments.add_argument("--allow-live", action="store_true")
    arguments.add_argument("--profile", help="Explicit OCI API-key profile; keys are never arguments.")
    arguments.add_argument("--compartment", help="Approved isolated compartment for initial setup.")
    arguments.add_argument("--lock-fd", type=int, help=argparse.SUPPRESS)
    return arguments


def worker(args, store):
    # Called only after opt-in and parent/child lock validation. No provider output
    # leaves this supervised child. A fixed private slot is never reset/replaced.
    from .proof_worker import credentials
    from .store_bootstrap_state import new_state
    from .store_bootstrap_services import BootstrapProof
    from troubleshooter_deployment.oci_recovery_bucket import OciRecoveryBucket
    from troubleshooter_deployment.records import Scope

    state = store.read()
    if state is None and (args.action != "run" or not args.compartment):
        return 2
    config = credentials(args.profile)
    if state is not None:
        scope = Scope(**state["scope"])
        if (config["user"] != state["principal"] or config["tenancy"] != scope.tenancy_id
                or config["region"] != scope.region or args.compartment not in (None, scope.compartment_id)):
            return 2
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(state["created_at"])).total_seconds()
        if not 0 <= age < 24 * 3600:
            return 2
    else:
        scope = Scope(1, "store-proof-" + uuid4().hex, config["tenancy"], args.compartment, config["region"])
        bucket = OciRecoveryBucket(config, scope)
        try:
            state = new_state(scope, config["user"], bucket.namespace())
            store.write(state)
        finally:
            bucket.close()
    with BootstrapProof(config, store, state) as proof:
        return 0 if getattr(proof, args.action)() else 1


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    arguments = parser()
    args = arguments.parse_args(argv)
    if not args.allow_live or not args.profile:
        arguments.error("--allow-live and --profile are required; no cloud calls made")
    try:
        from .store_bootstrap_state import validate_state
        ensure_directory(STATE_PATH.parent)
        with ProofStore(STATE_PATH, inherited_fd=args.lock_fd, validator=validate_state) as store:
            if args.lock_fd is not None:
                return worker(args, store)
            status = supervise(argv, store, worker="tests.live.store_bootstrap_proof")
            state = store.read()
            print(status)
            if state is not None:
                print("phase=" + state["phase"] + " cas=" + state["cas_status"])
            expected = "complete" if args.action == "cleanup" else "ready"
            return 0 if (status == "worker_finished_check_state" and state is not None
                         and state["phase"] == expected and state["cas_status"] == "confirmed") else 1
    except Exception:
        print("proof_unavailable_preserve_state")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
