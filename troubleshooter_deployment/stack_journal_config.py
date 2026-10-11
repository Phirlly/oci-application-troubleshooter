"""Deterministic declaration-only migration; initial-stack v1 stays frozen."""
from base64 import b64encode
from io import BytesIO
from zipfile import ZIP_STORED, ZipFile, ZipInfo

from .stack_bootstrap import BOOTSTRAP_VARIABLE
from .stack_journal_variables import KEYS


def declaration_zip():
    names = KEYS | {BOOTSTRAP_VARIABLE}
    source = "\n".join(f'variable "{name}" {{\n  type    = string\n  default = ""\n}}\n' for name in sorted(names))
    stream = BytesIO()
    with ZipFile(stream, "w", compression=ZIP_STORED) as archive:
        entry = ZipInfo("main.tf", date_time=(2026, 1, 1, 0, 0, 0))
        entry.create_system, entry.external_attr = 3, 0o100644 << 16
        archive.writestr(entry, source.encode("ascii"))
    return b64encode(stream.getvalue()).decode("ascii")
