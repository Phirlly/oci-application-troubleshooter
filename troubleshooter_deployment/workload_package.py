"""Secret-free, deterministic Terraform source for a workload stack."""
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import stat
from zipfile import ZIP_STORED, ZipFile, ZipInfo

from .host_startup import render_startup

SOURCE_FILES = (
    ".terraform.lock.hcl", "LICENSE", "compute.tf", "gateway.tf",
    "generated_inputs.tf", "network.tf", "network_security.tf", "outputs.tf",
    "runtime_iam.tf", "variables.tf", "versions.tf",
)
MAX_SOURCE_BYTES = 128 * 1024


@dataclass(frozen=True)
class WorkloadPackage:
    content: bytes

    @property
    def sha256(self):
        return sha256(self.content).hexdigest()


def build_workload_package(root: Path) -> WorkloadPackage:
    """Only package the explicit source contract, never a checkout directory."""
    stream = BytesIO()
    try:
        if root.is_symlink() or (root / "terraform").is_symlink():
            raise ValueError("Invalid workload source directory")
        def read_source(name):
            relative = Path(name)
            if any((root / parent).is_symlink() for parent in relative.parents):
                raise ValueError("Invalid workload source directory")
            descriptor = os.open(root / relative, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as source:
                details = os.fstat(source.fileno())
                if not stat.S_ISREG(details.st_mode) or details.st_size > MAX_SOURCE_BYTES:
                    raise ValueError("Invalid workload source file")
                content = source.read(MAX_SOURCE_BYTES + 1)
            if len(content) > MAX_SOURCE_BYTES:
                raise ValueError("Oversized workload source file")
            return content

        files = {name: read_source("LICENSE" if name == "LICENSE" else "terraform/" + name)
                 for name in SOURCE_FILES}
        files["host_startup.tf.json"] = json.dumps({"locals": {"host_user_data": render_startup(read_source)}},
                                                  sort_keys=True, separators=(",", ":")).encode("ascii")
        with ZipFile(stream, "w", compression=ZIP_STORED) as archive:
            for name, content in sorted(files.items()):
                entry = ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
                entry.create_system, entry.external_attr = 3, 0o100644 << 16
                archive.writestr(entry, content)
    except OSError:
        raise ValueError("Cannot read workload source file") from None
    return WorkloadPackage(stream.getvalue())
