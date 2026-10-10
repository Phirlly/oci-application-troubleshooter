"""Offline process-control fixture. Never imports OCI or opens a network."""
import argparse
from pathlib import Path
import time

from .proof_state import ProofStore


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock-fd", type=int, required=True)
    parser.add_argument("action")
    parser.add_argument("--state")
    parser.add_argument("--ready")
    args = parser.parse_args()
    if args.state:
        with ProofStore(Path(args.state), inherited_fd=args.lock_fd):
            Path(args.ready).write_text("ready")
            time.sleep(30)
    else:
        time.sleep(30)
