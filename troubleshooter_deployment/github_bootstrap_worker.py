"""Fixed child of github_runner; no output or local recovery files."""
import os
import signal

from .actions_context import ActionsContextError, RunnerConfigurationError, bootstrap_arguments, checked_out_sha


def main():
    try:
        # The parent blocks termination during Popen registration. Undo the
        # inherited mask in the child; the parent still owns forced teardown.
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGINT, signal.SIGTERM})
        arguments = bootstrap_arguments(os.environ, checked_out_sha())
        from .runner_bootstrap import BootstrapPending
        from .runner_stack_journal import StackJournalReady, bootstrap_stack_journal
        result = bootstrap_stack_journal(**arguments)
        if type(result) is StackJournalReady:
            return 0
        return 12 if type(result) is BootstrapPending else 13
    except ActionsContextError:
        return 10
    except RunnerConfigurationError:
        return 11
    except Exception:
        return 13


if __name__ == "__main__":
    raise SystemExit(main())
