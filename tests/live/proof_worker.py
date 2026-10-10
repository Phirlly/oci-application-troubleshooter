"""Fixed supervised worker. All provider output stays in the discarded child IO."""
import os
from datetime import datetime
from pathlib import Path
import sys

from .proof_flow import advance, observe
from .proof_services import ProofServices
from .proof_state import ProofStore, new_state
from .recovery_proof import STATE_PATH, audit_diagnostic_exits, audit_scan_exits, parser
from troubleshooter_deployment.oci_audit import AuditError
from troubleshooter_deployment.records import Scope


def credentials(profile):
    # This is only called inside the explicitly opted-in, supervised worker.
    import oci
    original = oci.config.from_file(profile_name=profile)
    if original.get("pass_phrase") or original.get("security_token_file"):
        raise ValueError("unsupported_proof_authentication")
    key_path = Path(original["key_file"]).expanduser()
    with key_path.open("rb") as file:
        key = file.read(65537)
    if len(key) > 65536:
        raise ValueError("unsupported_proof_key")
    return {**{k: original[k] for k in ("tenancy", "user", "fingerprint", "region")},
            "key_content": key.decode("ascii")}


def main():
    arguments = parser()
    arguments.add_argument("--lock-fd", type=int, required=True)
    args = arguments.parse_args()
    if not args.allow_live or not args.profile:
        return 2
    if args.action != "observe" and (args.search_start is not None or args.search_minutes is not None):
        return 2
    try:
        with ProofStore(STATE_PATH, inherited_fd=args.lock_fd) as store:
            state = store.read()
            if state is None and (args.action != "run" or not args.compartment):
                return 2
            config = credentials(args.profile)
            # Synthetic repository/attempt provenance is deliberately local.
            scope = Scope(1, "recovery-proof", config["tenancy"],
                          args.compartment if state is None else state["scope"]["compartment_id"], config["region"])
            if state is not None and (state["scope"] != scope.__dict__ or state["principal"] != config["user"]
                                      or args.compartment not in (None, scope.compartment_id)):
                return 2
            with ProofServices(config, scope) as services:
                if state is None:
                    state = new_state(scope, config["user"], services.bucket.namespace())
                    store.write(state)
                if args.action == "observe":
                    start = datetime.fromisoformat(args.search_start) if args.search_start else None
                    observe(store, state, services, search_start=start,
                            search_minutes=60 if args.search_minutes is None else args.search_minutes)
                else:
                    advance(store, state, services, cleanup=args.action == "cleanup")
        return 0
    except AuditError as error:
        if args.action == "observe":
            return audit_diagnostic_exits().get(error.transport_reason, audit_scan_exits().get(error.scan_reason, 1))
        return 1
    except Exception:
        return 1
    finally:
        os.close(args.lock_fd)


if __name__ == "__main__":
    sys.exit(main())
