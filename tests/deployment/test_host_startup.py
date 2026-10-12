import base64
import gzip
import json
from pathlib import Path
import unittest

from troubleshooter_deployment.host_startup import host_source_paths, render_startup


ROOT = Path(__file__).resolve().parents[2]


class HostStartupTests(unittest.TestCase):
    def test_exact_cloud_config_roundtrip_and_determinism(self):
        files = {name: (ROOT / name).read_bytes() for name in host_source_paths()}
        value = render_startup(files.__getitem__)
        self.assertEqual(value, render_startup(files.__getitem__))
        self.assertLess(len(value), 24000)
        raw = gzip.decompress(base64.b64decode(value)).decode()
        self.assertTrue(raw.startswith("#cloud-config\n"))
        config = json.loads(raw.split("\n", 1)[1])
        destinations = set()
        for item in config["write_files"]:
            self.assertEqual(item["owner"], "root:root")
            self.assertEqual(item["permissions"], "0644")
            self.assertEqual(item["encoding"], "b64")
            self.assertNotIn(item["path"], destinations)
            destinations.add(item["path"])
            source = item["path"].replace("/opt/ocitroubleshooter/", "").replace("/etc/systemd/system/", "host/")
            if source == "repos/oracle-bootstrap.repo":
                source = "host/oracle-bootstrap.repo"
            self.assertEqual(base64.b64decode(item["content"]), files[source])
        self.assertEqual(len(destinations), len(files))
        self.assertIn(["systemctl", "--no-block", "start", "ocitroubleshooter.service", "ocitroubleshooter-reconcile.timer"], config["runcmd"])
        self.assertNotIn("ssh_authorized_keys", config)
        self.assertFalse(any("password" in line.lower() for line in config.get("runcmd", []) if isinstance(line, str)))

    def test_mount_and_app_units_preserve_dependency_and_bounded_startup(self):
        service = (ROOT / "host/ocitroubleshooter.service").read_text()
        self.assertIn("BindsTo=var-lib-ocitroubleshooter-data.mount", service)
        self.assertIn("After=network-online.target var-lib-ocitroubleshooter-data.mount", service)
        self.assertIn("ExecStartPre=/usr/bin/python3 -m troubleshooter_host prepare", service)
        self.assertIn("ExecStopPost=/usr/bin/python3 -m troubleshooter_host stop", service)
        self.assertIn("StartLimitBurst=3", service)
        self.assertIn("TimeoutStartSec=1200", service)
        mount = (ROOT / "host/var-lib-ocitroubleshooter-data.mount").read_text()
        self.assertIn("After=ocitroubleshooter-storage.service", mount)
        self.assertIn("Options=rw,nodev,nosuid,noexec,_netdev", mount)
        self.assertNotIn("nofail", mount)
