"""Opt-in supervised stack-journal developer test; never presenter setup."""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys
from uuid import uuid4

from .proof_state import ProofStore, ensure_directory
from .recovery_proof import supervise

STATE_PATH = Path(__file__).resolve().parents[2] / ".cache" / "stack-journal-proof"


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("action", choices=("run-interrupted", "run", "observe", "cleanup"))
    result.add_argument("--allow-live", action="store_true")
    result.add_argument("--profile")
    result.add_argument("--compartment")
    result.add_argument("--lock-fd", type=int, help=argparse.SUPPRESS)
    return result


def worker(args, store):
    from .proof_worker import credentials
    from .stack_journal_state import new_state
    from .stack_journal_services import StackJournalProof
    from troubleshooter_deployment.records import Scope
    state = store.read()
    if state is None and (args.action != "run-interrupted" or not args.compartment):
        return 2
    config = credentials(args.profile)
    if state is None:
        scope = Scope(1, "stack-journal-proof-" + uuid4().hex, config["tenancy"], args.compartment, config["region"])
        state = new_state(scope, config["user"])
        store.write(state)
    else:
        scope = Scope(**state["scope"])
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(state["created_at"])).total_seconds()
        if (not 0 <= age < 86400 or config["user"] != state["principal"]
                or config["tenancy"] != scope.tenancy_id or config["region"] != scope.region
                or args.compartment not in (None, scope.compartment_id)):
            return 2
    with StackJournalProof(config, store, state) as proof:
        return 0 if getattr(proof, args.action.replace("-", "_"))() else 1


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    arguments = parser()
    args = arguments.parse_args(argv)
    if not args.allow_live or not args.profile:
        arguments.error("--allow-live and --profile are required; no cloud calls made")
    try:
        from .stack_journal_state import validate_state
        ensure_directory(STATE_PATH.parent)
        with ProofStore(STATE_PATH, inherited_fd=args.lock_fd, validator=validate_state) as store:
            if args.lock_fd is not None:
                return worker(args, store)
            status = supervise(argv, store, worker="tests.live.stack_journal_proof")
            state = store.read()
            print(status)
            if state is not None:
                print("phase=" + state["phase"] + " cas=" + state["cas"])
            expected = {"run-interrupted": "anchor_ready", "run": "ready", "observe": "ready", "cleanup": "complete"}[args.action]
            return 0 if (status == "worker_finished_check_state" and state is not None
                         and state["phase"] == expected
                         and state["cas"] == ("new" if args.action == "run-interrupted" else "confirmed")) else 1
    except Exception:
        print("proof_unavailable_preserve_state")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
