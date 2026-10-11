"""One-presenter login limits; never trust forwarded client identities."""
from collections import deque
import threading
import time


class LoginBusy(Exception):
    pass


class Authentication:
    def __init__(self, verifier, store, *, clock=time.monotonic):
        self.verifier, self.store, self.clock = verifier, store, clock
        self.hash_slot = threading.BoundedSemaphore(1)
        self.attempts = deque()

    def login(self, password):
        if not self.hash_slot.acquire(blocking=False):
            raise LoginBusy()
        try:
            now = self.clock()
            while self.attempts and self.attempts[0] <= now - 60:
                self.attempts.popleft()
            if len(self.attempts) >= 5:
                raise LoginBusy()
            self.attempts.append(now)
            return self.store.create() if self.verifier.matches(password) else None
        finally:
            self.hash_slot.release()
