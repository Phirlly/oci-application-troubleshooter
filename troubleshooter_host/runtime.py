"""Prepare verified runtime files and start only the generation's image."""
import re
from pathlib import Path

from .artifact import acquire_image, acquire_verifier, verify_image
from .host_io import atomic_write, command, read_json
from .inputs import HostError, encoded
from .linux_volume import ROOT, LinuxVolume, directory
from .network import NETWORK, ensure_network
from .storage import MOUNT, finish_mount

CONTROL = Path("/run/ocitroubleshooter")
RUNTIME = CONTROL / "runtime"
CIDFILE = CONTROL / "app.cid"
VERIFY = ("import sys; from troubleshooter_app.credentials import Verifier; "
          "Verifier.from_bytes(sys.stdin.buffer.read(1025))")


def restrictions():
    return ["--pull=never", "--read-only", "--read-only-tmpfs=false", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", "--user=10001:10001", "--pids-limit=128",
            "--memory=2g", "--cpus=1", "--log-driver=none"]


def container_arguments(inputs):
    return ["podman", "run", "--rm", "--name", "ocitroubleshooter", "--cidfile", str(CIDFILE),
            "--label", "troubleshooter-generation=" + inputs.generation_id,
            *restrictions(), "--network", NETWORK, "--publish", "0.0.0.0:8080:8080",
            "--volume", str(RUNTIME) + ":/run/demo:ro,Z",
            "--volume", MOUNT + "/application:/data:rw,Z", inputs.artifact_image_id]


def prepare_runtime(inputs):
    directory(CONTROL, 0, 0, 0o700)
    directory(RUNTIME, 0, 10001, 0o750)
    finish_mount(inputs, LinuxVolume(inputs))
    atomic_write(ROOT / "attempt.json", encoded({"identity": inputs.identity, "revision": inputs.revision}))
    ensure_network(inputs)
    cached = ROOT / "artifact.json"
    expected = {"identity": inputs.identity, "image": inputs.artifact_image_id}
    if cached.exists():
        if read_json(cached) != expected:
            raise HostError("artifact_identity_invalid")
        verify_image(inputs)
    else:
        acquire_image(inputs, ROOT)
        atomic_write(cached, encoded(expected))
    verifier = acquire_verifier(inputs)
    command(["podman", "run", "--rm", "--timeout", "30", "--interactive", *restrictions(),
             "--network=none", "--entrypoint", "python", inputs.artifact_image_id, "-c", VERIFY],
            stdin=verifier, timeout=45)
    atomic_write(RUNTIME / "presenter.json", verifier, mode=0o440, gid=10001)
    atomic_write(RUNTIME / "config.json", encoded(inputs.app_configuration), mode=0o440, gid=10001)
    atomic_write(CONTROL / "active.json", encoded({"revision": inputs.revision}))


def remove_container():
    if not CIDFILE.exists():
        return
    raw = CIDFILE.read_bytes()
    if len(raw) > 65 or not re.fullmatch(b"[a-f0-9]{64}\n?", raw):
        raise HostError("container_identity_invalid")
    command(["podman", "rm", "--force", "--ignore", raw.decode().strip()])
    CIDFILE.unlink(missing_ok=True)


def reconcile(inputs):
    active = CONTROL / "active.json"
    attempted = ROOT / "attempt.json"
    previous = read_json(attempted) if attempted.exists() else None
    if previous is not None and (previous.get("identity") != inputs.identity
                                 or previous.get("revision") == inputs.revision):
        return
    if previous is None and (not active.exists() or read_json(active) == {"revision": inputs.revision}):
        return
    state = command(["systemctl", "show", "--property=ActiveState", "--value", "ocitroubleshooter.service"]).strip()
    if state == b"active":
        command(["systemctl", "--no-block", "try-restart", "ocitroubleshooter.service"])
    elif state == b"failed" and previous is not None:
        # A new validated secret version gets one bounded retry cycle. Record
        # it before reset-failed, so a timer cannot create endless retries.
        atomic_write(attempted, encoded({"identity": inputs.identity, "revision": inputs.revision}))
        command(["systemctl", "reset-failed", "ocitroubleshooter.service"])
        command(["systemctl", "--no-block", "start", "ocitroubleshooter.service"])
