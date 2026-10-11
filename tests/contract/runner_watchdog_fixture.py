"""Offline disposable child to verify production process teardown."""
import signal
import time

signal.signal(signal.SIGTERM, signal.SIG_IGN)
print("synthetic-private-output", flush=True)
time.sleep(60)
