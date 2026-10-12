"""Initialize only the generation's positively identified, empty data volume."""
from dataclasses import dataclass

from .inputs import HostError

MOUNT = "/var/lib/ocitroubleshooter/data"
SIZE = 50 * 1024**3


@dataclass(frozen=True)
class Disk:
    device: str
    size: int
    boot_related: bool
    children: tuple
    holders: tuple
    readonly: bool
    mounts: tuple
    filesystem: str
    uuid: str
    signatures: tuple


def require(condition):
    if not condition:
        raise HostError("storage_identity_invalid")


def checked_record(inputs, record):
    require(type(record) is dict and set(record) == {"schema", "identity", "uuid", "phase"})
    require(type(record["schema"]) is int and record["schema"] == 1
            and record["identity"] == inputs.identity and record["uuid"] == inputs.filesystem_uuid
            and record["phase"] in ("formatting", "formatted", "initialized"))
    return record


def checked_disk(disk):
    require(type(disk) is Disk and disk.size == SIZE and not disk.boot_related
            and not disk.children and not disk.holders and not disk.readonly)
    require(disk.mounts in ((), (MOUNT,)))


def filesystem_matches(inputs, disk):
    return (disk.filesystem == "ext4" and disk.uuid == inputs.filesystem_uuid
            and disk.signatures == ("ext4",))


def prepare_filesystem(inputs, volume):
    disk, record = volume.inspect(), volume.read_record()
    checked_disk(disk)
    if record is None:
        require(not disk.mounts and not disk.filesystem and not disk.uuid and not disk.signatures)
        volume.require_zero(disk)
        record = {"schema": 1, "identity": inputs.identity,
                  "uuid": inputs.filesystem_uuid, "phase": "formatting"}
        volume.save_record(record)
    else:
        checked_record(inputs, record)
        if not disk.filesystem:
            require(record["phase"] == "formatting" and not disk.mounts
                    and not disk.uuid and not disk.signatures)
            volume.require_zero(disk)

    if not disk.filesystem:
        volume.format(disk, inputs.filesystem_uuid)
        disk = volume.inspect()
        checked_disk(disk)
    require(filesystem_matches(inputs, disk))
    if disk.mounts:
        require(record["phase"] in ("formatted", "initialized"))
        volume.verify_mount(disk, inputs.filesystem_uuid)
        return
    if record["phase"] in ("formatted", "initialized"):
        volume.check(disk, replay=True)
    volume.check(disk)
    if record["phase"] == "formatting":
        volume.save_record({**record, "phase": "formatted"})


def finish_mount(inputs, volume):
    record = checked_record(inputs, volume.read_record())
    disk = volume.inspect()
    checked_disk(disk)
    require(record["phase"] in ("formatted", "initialized") and filesystem_matches(inputs, disk))
    volume.verify_mount(disk, inputs.filesystem_uuid)
    volume.verify_marker({"schema": 1, "generation_id": inputs.generation_id,
                          "volume_ocid": inputs.data_volume_ocid, "uuid": inputs.filesystem_uuid},
                         allow_create=record["phase"] == "formatted")
    volume.application_directory()
    if record["phase"] != "initialized":
        volume.save_record({**record, "phase": "initialized"})
