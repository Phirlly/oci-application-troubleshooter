import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from troubleshooter_host.inputs import HostError, HostInputs, encoded
from troubleshooter_host.linux_volume import LinuxVolume, observe_disk, scan_zero, directory
from troubleshooter_host.storage import Disk, MOUNT, SIZE
from tests.host.test_inputs import metadata


def tree():
    return {"blockdevices": [
        {"name": "/dev/sda", "type": "disk", "size": SIZE, "ro": False,
         "mountpoints": [None], "children": [{"name": "/dev/sda3", "mountpoints": ["/"]}]},
        {"name": "/dev/sdb", "type": "disk", "size": SIZE, "ro": False,
         "mountpoints": [None]},
    ]}


class LinuxVolumeTests(unittest.TestCase):
    def setUp(self):
        self.inputs = HostInputs.from_imds(metadata())
        self.disk = Disk("/dev/sdb", SIZE, False, (), (), False, (), "", "", ())

    def test_observation_requires_whole_device_outside_proven_boot_tree(self):
        self.assertEqual(observe_disk(tree(), "/dev/sdb", "/dev/sda", (), []), self.disk)
        for mutate in [
            lambda t: t["blockdevices"][0]["children"][0].update(mountpoints=["/boot"]),
            lambda t: t["blockdevices"][1].update(type="loop"),
            lambda t: t["blockdevices"].append(t["blockdevices"][1]),
            lambda t: t["blockdevices"][1].update(size=True),
            lambda t: t["blockdevices"][1].update(mountpoints="/"),
        ]:
            value = tree()
            mutate(value)
            with self.assertRaises(HostError):
                observe_disk(value, "/dev/sdb", "/dev/sda", (), [])
        observed = observe_disk(tree(), "/dev/sda", "/dev/sda", (), [])
        self.assertTrue(observed.boot_related)

    def test_signatures_are_fresh_and_ambiguous_signatures_remain_visible(self):
        value = observe_disk(tree(), "/dev/sdb", "/dev/sda", ("holder",),
                             [{"type": "ext4", "uuid": "first"}, {"type": "xfs", "uuid": "other"}])
        self.assertEqual(value.signatures, ("ext4", "xfs"))
        self.assertEqual(value.holders, ("holder",))

    def test_linux_observation_settles_udev_and_probes_exact_alias_without_writes(self):
        volume = LinuxVolume(self.inputs)
        responses = [b"", encoded(tree()), encoded({"signatures": []})]
        with patch("troubleshooter_host.linux_volume.command", side_effect=responses) as run, patch.object(
                Path, "resolve", side_effect=[Path("/dev/sdb"), Path("/dev/sda")]), patch.object(
                Path, "iterdir", return_value=iter(())), patch("troubleshooter_host.linux_volume.os.stat",
                    return_value=SimpleNamespace(st_mode=stat.S_IFBLK)):
            self.assertEqual(volume.inspect(), self.disk)
            self.assertEqual(run.call_args_list[0].args[0], ["udevadm", "settle", "--timeout=10"])
            self.assertEqual(run.call_args_list[-1].args[0],
                             ["wipefs", "--no-act", "--json", "--output", "TYPE,UUID", "/dev/sdb"])

    def test_directory_reuse_rejects_links_and_changed_permissions_without_chowning(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "data"
            directory(path, os.geteuid(), os.getegid(), 0o700)
            directory(path, os.geteuid(), os.getegid(), 0o700)
            path.chmod(0o755)
            with self.assertRaises(HostError):
                directory(path, os.geteuid(), os.getegid(), 0o700)
            self.assertEqual(path.stat().st_mode & 0o777, 0o755)
            alias = Path(name) / "alias"
            alias.symlink_to(path)
            with self.assertRaises(HostError):
                directory(alias, os.geteuid(), os.getegid(), 0o755)

    def test_zero_scan_checks_opened_device_identity_and_closes_on_failure(self):
        volume = LinuxVolume(self.inputs)
        with patch.object(volume, "inspect", return_value=self.disk), patch(
                "troubleshooter_host.linux_volume.os.open", return_value=123), patch(
                "troubleshooter_host.linux_volume.os.fstat", return_value=SimpleNamespace(st_mode=stat.S_IFBLK, st_rdev=42)), patch(
                "troubleshooter_host.linux_volume.os.stat", return_value=SimpleNamespace(st_rdev=42)), patch(
                "troubleshooter_host.linux_volume.os.close") as close, patch(
                "troubleshooter_host.linux_volume.scan_zero") as scan:
            volume.require_zero(self.disk)
            self.assertEqual(scan.call_args.args, (123, SIZE))
            close.assert_called_once_with(123)
            scan.side_effect = HostError("storage_not_blank")
            with self.assertRaises(HostError):
                volume.require_zero(self.disk)
            self.assertEqual(close.call_count, 2)

    def test_full_zero_scan_checks_every_byte_size_and_deadline(self):
        with tempfile.TemporaryFile() as source:
            source.write(bytes(8192))
            source.seek(0)
            scan_zero(source.fileno(), 8192, deadline=float("inf"))
            source.seek(4096)
            source.write(b"X")
            source.seek(0)
            with self.assertRaisesRegex(HostError, "storage_not_blank"):
                scan_zero(source.fileno(), 8192, deadline=float("inf"))
            source.seek(0)
            with self.assertRaises(HostError):
                scan_zero(source.fileno(), 8192, deadline=0)
            source.seek(0)
            with self.assertRaises(HostError):
                scan_zero(source.fileno(), 20000, deadline=float("inf"))

    def test_format_and_checks_revalidate_and_never_force(self):
        volume = LinuxVolume(self.inputs)
        with patch.object(volume, "inspect", return_value=self.disk), patch(
                "troubleshooter_host.linux_volume.command") as run:
            volume.format(self.disk, self.inputs.filesystem_uuid)
            argv = run.call_args.args[0]
            self.assertEqual(argv[0], "mke2fs")
            self.assertNotIn("-F", argv)
            self.assertEqual(argv[-1], "/dev/sdb")
            self.assertIn(self.inputs.filesystem_uuid, argv)
            volume.check(self.disk, replay=True)
            self.assertEqual(run.call_args.args[0], ["e2fsck", "-E", "journal_only", "/dev/sdb"])
            volume.check(self.disk)
            self.assertEqual(run.call_args.args[0], ["e2fsck", "-n", "/dev/sdb"])
        with patch.object(volume, "inspect", side_effect=HostError("changed")), patch(
                "troubleshooter_host.linux_volume.command") as run:
            with self.assertRaises(HostError):
                volume.format(self.disk, self.inputs.filesystem_uuid)
            run.assert_not_called()

    def test_mount_validation_rejects_parent_fallback_and_wrong_device_or_options(self):
        volume = LinuxVolume(self.inputs)
        mount = {"filesystems": [{"target": MOUNT, "maj:min": "8:16", "fstype": "ext4",
                                  "options": "rw,nodev,nosuid,noexec,relatime", "fsroot": "/"}]}
        with patch("troubleshooter_host.linux_volume.os.stat", return_value=SimpleNamespace(st_rdev=os.makedev(8, 16))), patch(
                "troubleshooter_host.linux_volume.command", return_value=encoded(mount)) as run:
            volume.verify_mount(self.disk, self.inputs.filesystem_uuid)
            self.assertIn("--mountpoint", run.call_args.args[0])
        for key, value in [("target", "/"), ("maj:min", "8:0"), ("fstype", "xfs"),
                           ("options", "rw,nodev,nosuid"), ("fsroot", "/subdir")]:
            invalid = json.loads(json.dumps(mount))
            invalid["filesystems"][0][key] = value
            with patch("troubleshooter_host.linux_volume.os.stat", return_value=SimpleNamespace(st_rdev=os.makedev(8, 16))), patch(
                    "troubleshooter_host.linux_volume.command", return_value=encoded(invalid)):
                with self.assertRaises(HostError):
                    volume.verify_mount(self.disk, self.inputs.filesystem_uuid)

    def test_record_and_marker_are_durable_and_preserve_unknown_data(self):
        with tempfile.TemporaryDirectory() as name:
            volume = LinuxVolume(self.inputs)
            volume.record = Path(name) / "volume.json"
            volume.marker = Path(name) / "identity.json"
            self.assertIsNone(volume.read_record())
            record = {"schema": 1}
            volume.save_record(record)
            self.assertEqual(volume.read_record(), record)
            with self.assertRaises(HostError):
                volume.verify_marker(record)
            volume.verify_marker(record, allow_create=True)
            volume.verify_marker(record)
            with self.assertRaises(HostError):
                volume.verify_marker({"schema": 2}, allow_create=True)
            self.assertEqual(volume.marker.read_bytes(), encoded(record))
