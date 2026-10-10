"""Opt-in DEVELOPER proof, never a presenter deployment command."""
import argparse
import os
from pathlib import Path
import signal
import subprocess
import sys

from .proof_state import ProofStore, ensure_directory

ROOT = Path(__file__).resolve().parents[2]
STATE_PATH = ROOT / ".cache" / "recovery-proof"
OBSERVATIONS = {"observe", "observe-delete"}
DELETE_OBSERVATION_STATUSES = {
    0: "delete_request_match_observed",
    3: "delete_request_not_observed",
    4: "delete_request_match_ambiguous",
}


def experiment_path(base, experiment):
    # Closed slots, never arbitrary paths or automatic replacement of old state.
    return {"original": base, "frozen-delete": base.with_name("recovery-proof-frozen-delete")}[experiment]


def audit_diagnostic_exits():
    # Keep help and opt-in validation lightweight; load OCI only on failure.
    from troubleshooter_deployment.oci_transport import TRANSPORT_REASONS
    return {reason: code for code, reason in enumerate(TRANSPORT_REASONS, start=20)}


def audit_scan_exits():
    from troubleshooter_deployment.oci_audit import SCAN_REASONS
    return {reason: code for code, reason in enumerate(SCAN_REASONS, start=40)}


def supervise(args, store, *, timeout=60, worker="tests.live.proof_worker"):
    action = parser().parse_args(args).action
    process = None
    previous = signal.getsignal(signal.SIGTERM)

    def interrupted(signum, frame):
        raise KeyboardInterrupt()

    try:
        signal.signal(signal.SIGTERM, interrupted)
        process = subprocess.Popen(
            [sys.executable, "-m", worker, "--lock-fd", str(store.lock_fd), *args],
            cwd=ROOT, start_new_session=True, pass_fds=(store.lock_fd,),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        code = process.wait(timeout=timeout)
        if action == "observe-delete" and code in DELETE_OBSERVATION_STATUSES:
            return DELETE_OBSERVATION_STATUSES[code]
        if code != 0 and action in OBSERVATIONS:
            for reason, diagnostic_code in audit_diagnostic_exits().items():
                if code == diagnostic_code:
                    return "audit_transport_" + reason
            for reason, diagnostic_code in audit_scan_exits().items():
                if code == diagnostic_code:
                    return "audit_scan_" + reason
        return "worker_finished_check_state" if code == 0 else "worker_failed_check_state"
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        return "worker_stopped_outcome_unknown"
    finally:
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
        try:
            if process is not None and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)
        finally:
            signal.signal(signal.SIGTERM, previous)
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("action", choices=("run", "observe", "observe-delete", "cleanup"))
    result.add_argument("--experiment", choices=("original", "frozen-delete"), default="original",
                        help="Fixed developer state slot; never resets existing evidence.")
    result.add_argument("--allow-live", action="store_true", help="Explicitly authorize this bounded cloud experiment.")
    result.add_argument("--profile", help="Explicit OCI API-key profile. Keys are never command arguments.")
    result.add_argument("--compartment", help="Approved isolated compartment for the initial run only.")
    result.add_argument("--search-start", help="Observations only: UTC minute starting the search; coverage is unknown.")
    result.add_argument("--search-minutes", type=int, choices=range(1, 61), metavar="MINUTES",
                        help="Observations only: Log-search window duration, 1..60 minutes (default: 60).")
    return result


def main():
    arguments = parser()
    args = arguments.parse_args()
    if not args.allow_live or not args.profile:
        arguments.error("--allow-live and --profile are required; no cloud calls made")
    if args.action not in OBSERVATIONS and (args.search_start is not None or args.search_minutes is not None):
        arguments.error("--search-start and --search-minutes require observe or observe-delete")
    try:
        path = experiment_path(STATE_PATH, args.experiment)
        ensure_directory(path.parent)
        with ProofStore(path) as store:
            status = supervise(sys.argv[1:], store)
            state = store.read()
            print(status)
            if state is not None:
                print("stack=" + state["stack_status"] + " bucket=" + state["bucket_status"]
                      + " receipt=" + state["receipt_status"])
                if (args.action != "observe-delete" and state["audit_counts"] is not None
                        and status == "worker_finished_check_state"):
                    print("audit_candidates=" + ",".join(map(str, state["audit_counts"])))
                    print("audit_coverage=unknown")
            if args.action == "observe-delete":
                if status in DELETE_OBSERVATION_STATUSES.values():
                    print("audit_coverage=unknown")
                return 0 if status == DELETE_OBSERVATION_STATUSES[0] else 1
            return 0 if status == "worker_finished_check_state" else 1
    except Exception:
        print("proof_unavailable_preserve_state")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
