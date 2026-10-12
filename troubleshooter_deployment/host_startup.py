"""Deterministic, secret-free cloud-config from an explicit reviewed allowlist."""
import base64
import gzip
from io import BytesIO
import json

HOST_MODULES = ("__init__.py", "__main__.py", "inputs.py", "host_io.py", "storage.py",
                "linux_volume.py", "artifact.py", "network.py", "runtime.py")
HOST_UNITS = ("ocitroubleshooter-storage.service", "var-lib-ocitroubleshooter-data.mount",
              "dev-oracleoci-oraclevdb.device.d/timeout.conf", "ocitroubleshooter.service",
              "ocitroubleshooter-reconcile.service", "ocitroubleshooter-reconcile.timer")


def host_source_paths():
    return (tuple("troubleshooter_host/" + name for name in HOST_MODULES)
            + tuple("host/" + name for name in HOST_UNITS) + ("host/oracle-bootstrap.repo",))


def render_startup(read_source):
    files = []
    for name in host_source_paths():
        destination = ("/etc/systemd/system/" + name[5:] if name.startswith("host/")
                       else "/opt/ocitroubleshooter/" + name)
        if name == "host/oracle-bootstrap.repo":
            destination = "/opt/ocitroubleshooter/repos/oracle-bootstrap.repo"
        files.append({"path": destination, "owner": "root:root", "permissions": "0644",
                      "encoding": "b64", "content": base64.b64encode(read_source(name)).decode("ascii")})
    install = ["timeout", "600", "dnf", "--assumeyes", "--setopt=timeout=30", "--setopt=retries=1",
               "--setopt=reposdir=/opt/ocitroubleshooter/repos", "install"]
    config = {"write_files": files, "runcmd": [
        [*install, "oraclelinux-developer-release-el9"],
        [*install, "python3", "python39-oci-cli", "podman", "nftables", "e2fsprogs"],
        ["systemctl", "daemon-reload"],
        ["systemctl", "enable", "ocitroubleshooter.service", "ocitroubleshooter-reconcile.timer"],
        ["systemctl", "--no-block", "start", "ocitroubleshooter.service", "ocitroubleshooter-reconcile.timer"],
    ]}
    raw = b"#cloud-config\n" + json.dumps(config, sort_keys=True, separators=(",", ":")).encode("ascii")
    output = BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0) as stream:
        stream.write(raw)
    encoded = base64.b64encode(output.getvalue()).decode("ascii")
    if len(encoded) > 24000:
        raise ValueError("Oversized workload source bootstrap")
    return encoded
