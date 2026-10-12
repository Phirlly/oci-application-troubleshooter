"""Fixed systemd entrypoints; never print metadata, service output or secrets."""
import os
from pathlib import Path
import sys
import time

from .host_io import load_metadata, read_json
from .inputs import HostError
from .linux_volume import ROOT, LinuxVolume, directory
from .network import ensure_network
from .runtime import CONTROL, container_arguments, prepare_runtime, reconcile, remove_container
from .storage import MOUNT, finish_mount, prepare_filesystem


def wait_for_volume(inputs):
    for _ in range(40):
        if Path(inputs.data_device).exists():
            return
        time.sleep(3)
    raise HostError("storage_unavailable")


def main(arguments=None):
    try:
        arguments = sys.argv[1:] if arguments is None else arguments
        if len(arguments) != 1 or arguments[0] not in ("storage", "prepare", "run", "stop", "reconcile") or os.geteuid() != 0:
            raise ValueError()
        action = arguments[0]
        os.umask(0o077)
        if action == "stop":
            remove_container()
            return 0
        inputs = load_metadata()
        if action == "storage":
            directory(ROOT, 0, 0, 0o700)
            if not os.path.ismount(MOUNT):
                directory(MOUNT, 0, 0, 0o700)
            wait_for_volume(inputs)
            prepare_filesystem(inputs, LinuxVolume(inputs))
        elif action == "prepare":
            prepare_runtime(inputs)
        elif action == "reconcile":
            reconcile(inputs)
        else:
            if read_json(CONTROL / "active.json") != {"revision": inputs.revision}:
                raise ValueError()
            finish_mount(inputs, LinuxVolume(inputs))
            ensure_network(inputs)
            os.execvpe("podman", container_arguments(inputs), {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
                                                             "HOME": "/root", "LANG": "C.UTF-8"})
        return 0
    except Exception:
        print("Host startup failed: configuration or service unavailable.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
