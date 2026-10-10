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


def supervise(args, store, *, timeout=60, worker="tests.live.proof_worker"):
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
    result.add_argument("action", choices=("run", "observe", "cleanup"))
    result.add_argument("--allow-live", action="store_true", help="Explicitly authorize this bounded cloud experiment.")
    result.add_argument("--profile", help="Explicit OCI API-key profile. Keys are never command arguments.")
    result.add_argument("--compartment", help="Approved isolated compartment for the initial run only.")
    result.add_argument("--processed-start", help="Observe only: UTC minute starting a one-hour Audit window.")
    return result


def main():
    arguments = parser()
    args = arguments.parse_args()
    if not args.allow_live or not args.profile:
        arguments.error("--allow-live and --profile are required; no cloud calls made")
    if args.processed_start and args.action != "observe":
        arguments.error("--processed-start is only valid with observe")
    try:
        ensure_directory(STATE_PATH.parent)
        with ProofStore(STATE_PATH) as store:
            status = supervise(sys.argv[1:], store)
            state = store.read()
            print(status)
            if state is not None:
                print("stack=" + state["stack_status"] + " bucket=" + state["bucket_status"]
                      + " receipt=" + state["receipt_status"])
                if state["audit_counts"] is not None:
                    print("audit_candidates=" + ",".join(map(str, state["audit_counts"])))
            return 0 if status == "worker_finished_check_state" else 1
    except Exception:
        print("proof_unavailable_preserve_state")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
