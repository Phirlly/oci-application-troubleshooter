"""Linux observations and bounded operations for the dedicated OCI data disk."""
import json
import os
from pathlib import Path
import stat
import time

from .host_io import atomic_write, command, read_json
from .inputs import HostError, encoded, unique_fields
from .storage import Disk, MOUNT, checked_disk, require

ROOT = Path("/var/lib/ocitroubleshooter")


def descendants(node):
    yield node
    for child in node.get("children", []):
        yield from descendants(child)


def observe_disk(tree, device, boot, holders, signatures):
    try:
        roots = tree["blockdevices"]
        boot_roots = [node for node in roots if node["name"] == boot and node["type"] == "disk"]
        require(len(boot_roots) == 1)
        boot_nodes = list(descendants(boot_roots[0]))
        require(any("/" in (node.get("mountpoints") or []) for node in boot_nodes))
        matches = [node for root in roots for node in descendants(root) if node["name"] == device]
        require(len(matches) == 1)
        item = matches[0]
        require(item["type"] == "disk" and type(item["size"]) is int
                and type(item["ro"]) is bool and type(item["mountpoints"]) is list)
        return Disk(device, item["size"], any(node["name"] == device for node in boot_nodes),
                    tuple(child["name"] for child in item.get("children", [])), tuple(holders),
                    item["ro"], tuple(m for m in item["mountpoints"] if m),
                    signatures[0]["type"] if signatures else "",
                    (signatures[0].get("uuid") or "") if signatures else "",
                    tuple(s["type"] for s in signatures))
    except (KeyError, TypeError, ValueError):
        raise HostError("storage_identity_invalid") from None


def scan_zero(descriptor, size, *, deadline):
    remaining = size
    zero = bytes(4 * 1024**2)
    while remaining:
        require(time.monotonic() < deadline)
        chunk = os.read(descriptor, min(len(zero), remaining))
        if not chunk or chunk != zero[:len(chunk)]:
            raise HostError("storage_not_blank")
        remaining -= len(chunk)


def directory(path, uid, gid, mode):
    """Only initialize a new directory; refuse changed ownership on reuse."""
    path = Path(path)
    try:
        path.mkdir(mode=mode)
    except FileExistsError:
        pass
    else:
        os.chown(path, uid, gid)
        os.chmod(path, mode)
    details = path.lstat()
    require(stat.S_ISDIR(details.st_mode) and details.st_uid == uid and details.st_gid == gid
            and stat.S_IMODE(details.st_mode) == mode)


class LinuxVolume:
    def __init__(self, inputs):
        self.inputs = inputs
        self.record = ROOT / "volume.json"
        self.marker = Path(MOUNT) / "identity.json"

    def inspect(self):
        command(["udevadm", "settle", "--timeout=10"], timeout=15)
        device = str(Path(self.inputs.data_device).resolve(strict=True))
        boot = str(Path("/dev/oracleoci/oraclevda").resolve(strict=True))
        require(stat.S_ISBLK(os.stat(device).st_mode) and stat.S_ISBLK(os.stat(boot).st_mode))
        tree = json.loads(command(["lsblk", "--json", "--bytes", "--paths", "--tree",
                                   "--output", "NAME,TYPE,SIZE,RO,MOUNTPOINTS"]),
                          object_pairs_hook=unique_fields)
        signatures = json.loads(command(["wipefs", "--no-act", "--json", "--output", "TYPE,UUID", device]),
                                object_pairs_hook=unique_fields)["signatures"]
        holders = tuple(p.name for p in (Path("/sys/class/block") / Path(device).name / "holders").iterdir())
        return observe_disk(tree, device, boot, holders, signatures)

    def read_record(self):
        if not self.record.exists() and not self.record.is_symlink():
            return None
        return read_json(self.record)

    def save_record(self, record):
        atomic_write(self.record, encoded(record))

    def require_zero(self, disk):
        require(self.inspect() == disk and not disk.mounts)
        descriptor = os.open(disk.device, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            details = os.fstat(descriptor)
            require(stat.S_ISBLK(details.st_mode) and details.st_rdev == os.stat(disk.device).st_rdev)
            scan_zero(descriptor, disk.size, deadline=time.monotonic() + 900)
        finally:
            os.close(descriptor)

    def format(self, disk, expected_uuid):
        require(self.inspect() == disk and not disk.mounts)
        checked_disk(disk)
        require(not disk.signatures and not disk.filesystem and not disk.uuid)
        command(["mke2fs", "-q", "-t", "ext4", "-U", expected_uuid,
                 "-E", "lazy_itable_init=0,lazy_journal_init=0", disk.device], timeout=600)

    def check(self, disk, replay=False):
        require(self.inspect() == disk and not disk.mounts)
        command(["e2fsck"] + (["-p", "-E", "journal_only"] if replay else ["-n"]) + [disk.device], timeout=300)

    def verify_mount(self, disk, expected_uuid):
        result = json.loads(command(["findmnt", "--json", "--mountpoint", MOUNT,
                                     "--output", "TARGET,MAJ:MIN,FSTYPE,OPTIONS,FSROOT"]),
                            object_pairs_hook=unique_fields)["filesystems"]
        require(len(result) == 1)
        item, identity = result[0], os.stat(disk.device).st_rdev
        require(item["target"] == MOUNT and item["fstype"] == "ext4" and item["fsroot"] == "/"
                and item["maj:min"] == "{}:{}".format(os.major(identity), os.minor(identity))
                and {"rw", "nodev", "nosuid", "noexec"} <= set(item["options"].split(",")))

    def verify_marker(self, expected, allow_create=False):
        if not self.marker.exists() and not self.marker.is_symlink():
            require(allow_create)
            atomic_write(self.marker, encoded(expected))
        require(read_json(self.marker) == expected)

    def application_directory(self):
        directory(Path(MOUNT) / "application", 10001, 10001, 0o700)
