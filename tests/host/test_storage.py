from dataclasses import replace
import unittest

from troubleshooter_host.inputs import HostError, HostInputs
from troubleshooter_host.storage import Disk, prepare_filesystem, finish_mount
from tests.host.test_inputs import metadata


class Volume:
    def __init__(self, inputs):
        self.inputs, self.record, self.calls = inputs, None, []
        self.disk = Disk("/dev/sdb", 50 * 1024**3, False, (), (), False, (), "", "", ())
        self.zero = True
        self.healthy = True
        self.marker = None

    def inspect(self):
        self.calls.append("inspect")
        return self.disk

    def read_record(self):
        return self.record

    def save_record(self, record):
        self.record = dict(record)
        self.calls.append("save:" + record["phase"])

    def require_zero(self, disk):
        self.calls.append("zero")
        if not self.zero:
            raise HostError("storage_not_blank")

    def format(self, disk, expected_uuid):
        self.calls.append("format")
        self.disk = replace(disk, filesystem="ext4", uuid=expected_uuid, signatures=("ext4",))

    def check(self, disk, replay=False):
        self.calls.append("replay" if replay else "check")
        if not self.healthy:
            raise HostError("storage_check_failed")

    def verify_mount(self, disk, expected_uuid):
        self.calls.append("verify_mount")
        if disk.mounts != ("/var/lib/ocitroubleshooter/data",):
            raise HostError("storage_mount_invalid")

    def verify_marker(self, expected, allow_create=False):
        self.calls.append("marker")
        if self.marker != expected:
            if self.marker is not None or not allow_create:
                raise HostError("storage_identity_invalid")
            self.marker = expected

    def application_directory(self):
        self.calls.append("application_directory")


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.inputs = HostInputs.from_imds(metadata())
        self.volume = Volume(self.inputs)

    def initialized(self):
        prepare_filesystem(self.inputs, self.volume)
        self.volume.disk = replace(self.volume.disk, mounts=("/var/lib/ocitroubleshooter/data",))
        finish_mount(self.inputs, self.volume)
        self.volume.calls.clear()

    def test_first_format_has_durable_intent_and_preserves_application_on_retry(self):
        prepare_filesystem(self.inputs, self.volume)
        self.assertLess(self.volume.calls.index("zero"), self.volume.calls.index("save:formatting"))
        self.assertLess(self.volume.calls.index("save:formatting"), self.volume.calls.index("format"))
        self.assertEqual(self.volume.record["phase"], "formatted")
        self.volume.calls.clear()
        prepare_filesystem(self.inputs, self.volume)
        self.assertNotIn("format", self.volume.calls)
        self.assertIn("replay", self.volume.calls)
        self.volume.disk = replace(self.volume.disk, mounts=("/var/lib/ocitroubleshooter/data",))
        finish_mount(self.inputs, self.volume)
        self.assertEqual(self.volume.record["phase"], "initialized")

    def test_unsafe_devices_are_rejected_before_any_write(self):
        for change in [dict(size=1), dict(boot_related=True), dict(children=("sdb1",)),
                       dict(holders=("dm-0",)), dict(readonly=True), dict(mounts=("/",)),
                       dict(signatures=("xfs",)), dict(filesystem="ext4"), dict(uuid="unknown")]:
            with self.subTest(change=change):
                volume = Volume(self.inputs)
                volume.disk = replace(volume.disk, **change)
                with self.assertRaises(HostError):
                    prepare_filesystem(self.inputs, volume)
                self.assertFalse(any(c.startswith("save:") or c == "format" for c in volume.calls))

    def test_nonzero_unrecognized_content_is_never_formatted(self):
        self.volume.zero = False
        with self.assertRaises(HostError):
            prepare_filesystem(self.inputs, self.volume)
        self.assertNotIn("format", self.volume.calls)
        self.assertIsNone(self.volume.record)

    def test_exact_mounted_volume_is_reused_without_fsck(self):
        self.initialized()
        prepare_filesystem(self.inputs, self.volume)
        finish_mount(self.inputs, self.volume)
        self.assertNotIn("check", self.volume.calls)
        self.assertNotIn("replay", self.volume.calls)
        self.assertNotIn("format", self.volume.calls)

    def test_initialized_unmounted_volume_replays_journal_then_checks(self):
        self.initialized()
        self.volume.disk = replace(self.volume.disk, mounts=())
        prepare_filesystem(self.inputs, self.volume)
        self.assertLess(self.volume.calls.index("replay"), self.volume.calls.index("check"))
        self.assertNotIn("format", self.volume.calls)

    def test_first_mount_interruption_before_initialized_record_replays_only_completed_filesystem(self):
        prepare_filesystem(self.inputs, self.volume)
        self.volume.disk = replace(self.volume.disk, mounts=("/var/lib/ocitroubleshooter/data",))
        original = self.volume.save_record
        self.volume.save_record = lambda record: (_ for _ in ()).throw(HostError("interrupted"))
        with self.assertRaises(HostError):
            finish_mount(self.inputs, self.volume)
        self.volume.save_record = original
        self.assertEqual(self.volume.record["phase"], "formatted")
        self.assertIsNotNone(self.volume.marker)
        self.volume.disk = replace(self.volume.disk, mounts=())
        self.volume.calls.clear()
        prepare_filesystem(self.inputs, self.volume)
        self.assertLess(self.volume.calls.index("replay"), self.volume.calls.index("check"))
        self.assertNotIn("format", self.volume.calls)

    def test_partial_format_and_lost_initialized_filesystem_are_never_overwritten(self):
        prepare_filesystem(self.inputs, self.volume)
        self.volume.record["phase"] = "formatting"
        self.volume.healthy = False
        self.volume.calls.clear()
        with self.assertRaises(HostError):
            prepare_filesystem(self.inputs, self.volume)
        self.assertNotIn("replay", self.volume.calls)
        self.assertNotIn("format", self.volume.calls)
        self.volume.record["phase"] = "initialized"
        self.volume.disk = replace(self.volume.disk, filesystem="", uuid="", signatures=())
        with self.assertRaises(HostError):
            prepare_filesystem(self.inputs, self.volume)
        self.assertNotIn("format", self.volume.calls)

    def test_missing_mount_foreign_marker_or_changed_identity_stops_activation(self):
        prepare_filesystem(self.inputs, self.volume)
        with self.assertRaises(HostError):
            finish_mount(self.inputs, self.volume)
        self.volume.disk = replace(self.volume.disk, mounts=("/var/lib/ocitroubleshooter/data",))
        self.volume.marker = {"foreign": True}
        with self.assertRaises(HostError):
            finish_mount(self.inputs, self.volume)
        self.assertNotIn("application_directory", self.volume.calls)
        self.volume.record["identity"] = {}
        with self.assertRaises(HostError):
            prepare_filesystem(self.inputs, self.volume)
