"""Opt-in root Linux fixtures. No OCI calls, real disks, host mounts or firewall edits.

Run only on an isolated development runner. Namespace checks never enter the
host network namespace. A temporary sparse file is the only formatted storage.
This does not prove OCI device identity, SELinux, IAM or cloud-init execution.
"""
from dataclasses import replace
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from troubleshooter_deployment.host_startup import HOST_UNITS
from troubleshooter_host.inputs import HostInputs
from troubleshooter_host import linux_volume
from troubleshooter_host.storage import Disk, SIZE
from tests.host.test_inputs import metadata

INTERRUPTIONS = {signal.SIGALRM, signal.SIGINT, signal.SIGTERM}


def run(argv, *, timeout=240, **kwargs):
    process = None
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, INTERRUPTIONS)
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, start_new_session=True,
                                   preexec_fn=lambda: signal.pthread_sigmask(signal.SIG_SETMASK, previous), **kwargs)
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)
        output, errors = process.communicate(timeout=timeout)
        if process.returncode:
            raise RuntimeError("Linux fixture failed: " + argv[0] + "\n" + errors[-5000:])
        return output
    finally:
        # The namespace probe owns descendants; cancellation must reap the
        # whole group even if its leader exits before its synthetic server.
        signal.pthread_sigmask(signal.SIG_BLOCK, INTERRUPTIONS)
        try:
            if process is not None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=10)
                process.stdout.close()
                process.stderr.close()
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def systemd_check(scratch):
    units = [str(ROOT / "host" / name) for name in HOST_UNITS if not name.endswith(".conf")]
    environment = {**os.environ, "SYSTEMD_UNIT_PATH": str(ROOT / "host") + ":"}
    run(["systemd-analyze", "verify", "--man=no", *units], env=environment)
    # Prove the checker detects the concrete pre-fix default-ordering cycle.
    bad = scratch / "var-lib-ocitroubleshooter-data.mount"
    bad.write_text((ROOT / "host" / bad.name).read_text().replace(",_netdev", ""))
    environment["SYSTEMD_UNIT_PATH"] = str(scratch) + ":" + str(ROOT / "host") + ":"
    result = subprocess.run(["systemd-analyze", "verify", "--man=no", str(bad), units[0]],
                            env=environment, capture_output=True, text=True, timeout=30)
    if not result.returncode or "cycle" not in result.stderr.lower():
        raise RuntimeError("systemd negative fixture did not detect ordering cycle")


def storage_check(scratch):
    backing, mount = scratch / "owned-volume.img", scratch / "mounted"
    with backing.open("wb") as output:
        output.truncate(SIZE)
    mount.mkdir()
    try:
        previous = signal.pthread_sigmask(signal.SIG_BLOCK, INTERRUPTIONS)
        try:
            device = run(["losetup", "--find", "--show", str(backing)]).strip()
            if not re.fullmatch(r"/dev/loop[0-9]+", device):
                raise RuntimeError("owned loop identity")
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous)
        require_size = int(run(["blockdev", "--getsize64", device]).strip())
        if require_size != SIZE:
            raise RuntimeError("owned loop size")
        inputs = HostInputs.from_imds(metadata())
        volume = linux_volume.LinuxVolume(inputs)
        disk = Disk(device, SIZE, False, (), (), False, (), "", "", ())
        # Only the physical-OCI discovery is substituted; the device handle,
        # zero scan, formatter, fsck, kernel mount and ownership are real.
        with patch.object(volume, "inspect", return_value=disk):
            volume.require_zero(disk)
            volume.format(disk, inputs.filesystem_uuid)
        signatures = json.loads(run(["wipefs", "--no-act", "--json", "--output", "TYPE,UUID", device]))["signatures"]
        if signatures != [{"type": "ext4", "uuid": inputs.filesystem_uuid}]:
            raise RuntimeError("created filesystem identity")
        disk = replace(disk, filesystem="ext4", uuid=inputs.filesystem_uuid, signatures=("ext4",))
        with patch.object(volume, "inspect", return_value=disk):
            volume.check(disk)
            volume.check(disk, replay=True)
        run(["mount", "--options", "rw,nodev,nosuid,noexec", device, str(mount)])
        with patch.object(linux_volume, "MOUNT", str(mount)):
            volume.verify_mount(disk, inputs.filesystem_uuid)
            volume.application_directory()
            volume.application_directory()
        application = mount / "application"
        if (application.stat().st_uid, application.stat().st_gid, application.stat().st_mode & 0o777) != (10001, 10001, 0o700):
            raise RuntimeError("application directory ownership")
    finally:
        previous = signal.pthread_sigmask(signal.SIG_BLOCK, INTERRUPTIONS)
        remaining = signal.alarm(0)
        try:
            # Query the exact backing path even if allocation/mount replies
            # were interrupted. Never detach a discovered unrelated device.
            loops = json.loads(run(["losetup", "--json", "--list", "--output", "NAME,BACK-FILE"]))["loopdevices"]
            owned = [item["name"] for item in loops if item["back-file"] == str(backing)]
            if os.path.ismount(mount):
                source = json.loads(run(["findmnt", "--json", "--mountpoint", str(mount), "--output", "SOURCE"]))["filesystems"]
                if len(source) != 1 or source[0]["source"] not in owned:
                    raise RuntimeError("Refusing unknown fixture mount")
                run(["umount", str(mount)])
            for identity in owned:
                if not re.fullmatch(r"/dev/loop[0-9]+", identity):
                    raise RuntimeError("owned loop cleanup identity")
                run(["losetup", "--detach", identity])
        finally:
            signal.alarm(remaining)
            signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def interrupted(_number, _frame):
    raise RuntimeError("Linux fixture interrupted")


def main():
    if sys.platform != "linux" or os.geteuid() != 0:
        raise RuntimeError("Requires explicit root execution on an isolated Linux runner")
    for number in (signal.SIGALRM, signal.SIGINT, signal.SIGTERM):
        signal.signal(number, interrupted)
    signal.alarm(270)
    try:
        scratch = Path(tempfile.mkdtemp(prefix="troubleshooter-linux-"))
        try:
            systemd_check(scratch)
            storage_check(scratch)
        except BaseException:
            # Preserve owned backing data if cleanup itself could not finish.
            raise
        else:
            shutil.rmtree(scratch)
        run(["unshare", "--net", sys.executable, str(Path(__file__).with_name("host_network_probe.py"))])
    finally:
        signal.alarm(0)
    print("PASS: systemd ordering, owned loop filesystem/mount/permissions and isolated bridge firewall.")
    print("OCI device selection, SELinux, package installation, IAM and guest reboot remain separate gates.")


if __name__ == "__main__":
    main()
