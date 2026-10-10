"""Synthetic identities and a fault-injecting conditional store."""
from troubleshooter_deployment.records import Attempt, Identity, Scope
from troubleshooter_deployment.journal import WriteConflict, WriteUncertain

SHA = "a" * 40
ATTEMPT = Attempt(100, 1, SHA, SHA)
NEXT_ATTEMPT = Attempt(100, 2, SHA, SHA)
IDENTITY = Identity(Scope(42, "demo", "synthetic-tenancy", "synthetic-compartment", "us-ashburn-1"), "synthetic-bucket", "1" * 32)
GENERATION = "2" * 32
SECOND_GENERATION = "3" * 32
OPERATION = "4" * 32
REMOVAL = "5" * 32


class MemoryStore:
    """Test fixture only: this establishes no OCI durability guarantee."""

    def __init__(self):
        self.value = None
        self.version = 0
        self.fail = None

    def read(self):
        if self.fail == "read":
            raise OSError("synthetic read unavailable")
        return None if self.value is None else (str(self.version), self.value)

    def write(self, expected, body):
        actual = None if self.value is None else str(self.version)
        if expected != actual:
            raise WriteConflict()
        if self.fail == "before":
            raise WriteUncertain()
        self.value = body
        self.version += 1
        if self.fail == "after":
            raise WriteUncertain()
        return str(self.version)
