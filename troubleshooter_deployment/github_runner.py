"""Supervised GitHub bootstrap proof. This does not deploy the demo."""
import os
from pathlib import Path
import signal
import subprocess
import sys
from threading import current_thread, main_thread

from .actions_context import ROOT, ActionsContextError, checked_out_sha, validate_context

SUPPORTED = os.name == "posix" and all(hasattr(signal, name) for name in ("pthread_sigmask", "SIG_BLOCK", "SIG_SETMASK"))
MESSAGES = {
    0: "BOOTSTRAP_VERIFIED: deployment journal initialized or resumed. The demo is not deployed.",
    10: "CONTEXT_INVALID: use the reviewed main workflow and its exact checkout on GitHub-hosted Linux.",
    11: "CONFIGURATION_INVALID: check the demo environment OCI secrets and DEPLOYMENT_TFVARS_JSON.",
    12: "BOOTSTRAP_PENDING: recovery could not be confirmed. Preserve history and rerun the same release; do not replace resources.",
    13: "BOOTSTRAP_FAILED: setup could not be verified. Preserve history and resources.",
    14: "OUTCOME_UNKNOWN: the worker stopped. Preserve history and rerun the same release to reconcile.",
    15: "SUMMARY_UNAVAILABLE: the result could not be published. Preserve history and resources.",
}


def supervise(*, timeout=300):
    if not SUPPORTED or current_thread() is not main_thread():
        return 13
    signals = {signal.SIGINT, signal.SIGTERM}
    previous = {sig: signal.getsignal(sig) for sig in signals}
    process, code = None, 13
    tearing_down, cancelled = False, False

    def interrupted(signum, frame):
        nonlocal cancelled
        cancelled = True
        if not tearing_down:
            raise KeyboardInterrupt()

    try:
        for sig in signals:
            signal.signal(sig, interrupted)
        # Register the child before a pending cancellation can unwind Popen.
        mask = signal.pthread_sigmask(signal.SIG_BLOCK, signals)
        try:
            process = subprocess.Popen(
                [sys.executable, "-B", "-m", "troubleshooter_deployment.github_bootstrap_worker"],
                cwd=ROOT, start_new_session=True, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, mask)
        result = process.wait(timeout=timeout)
        code = result if result in {0, 10, 11, 12, 13} else 13
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        code = 14
    except Exception:
        code = 13
    finally:
        tearing_down = True
        mask = signal.pthread_sigmask(signal.SIG_BLOCK, signals)
        try:
            if process is not None and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except OSError:
                    code = 14
                process.wait(timeout=5)
        except Exception:
            code = 14
        finally:
            try:
                # Deliver queued cancellations while our handlers still apply.
                signal.pthread_sigmask(signal.SIG_SETMASK, mask)
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
    return 14 if cancelled else code


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if args == ["--help"]:
        print(__doc__)
        return 0
    code = 10
    try:
        if args:
            raise ActionsContextError()
        validate_context(os.environ, checked_out_sha())
        code = supervise()
    except ActionsContextError:
        pass
    except KeyboardInterrupt:
        code = 14
    except Exception:
        code = 13
    message = MESSAGES[code]
    print(message)
    try:
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as summary:
            summary.write(message + "\n")
    except (KeyError, OSError, ValueError):
        print(MESSAGES[15])
        return 1
    return 0 if code == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
