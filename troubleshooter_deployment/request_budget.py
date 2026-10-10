"""Best-effort POSIX lifecycle budget; an outer process watchdog is still required."""
from contextlib import contextmanager
import math
import signal
from threading import current_thread, main_thread
from time import monotonic

SUPPORTED = all(hasattr(signal, name) for name in ("SIGALRM", "ITIMER_REAL", "setitimer", "getitimer"))


class BudgetError(RuntimeError):
    def __init__(self):
        super().__init__("operation_budget_unavailable_or_exceeded")


@contextmanager
def operation_budget(seconds=20):
    if (not SUPPORTED or current_thread() is not main_thread()
            or type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds <= 0
            or signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0)):
        raise BudgetError()
    previous = signal.getsignal(signal.SIGALRM)
    if previous is None:
        raise BudgetError()
    started = monotonic()

    def expired(signum, frame):
        raise BudgetError()

    signal.signal(signal.SIGALRM, expired)
    try:
        signal.setitimer(signal.ITIMER_REAL, seconds)
        yield
        # C work can defer the handler. Never accept a late successful result.
        if monotonic() - started >= seconds:
            raise BudgetError()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
