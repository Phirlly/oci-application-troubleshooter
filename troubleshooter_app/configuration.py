"""Nonsecret, deployment-supplied application configuration."""
from dataclasses import dataclass
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from .credentials import unique_fields


def read_bounded(path: Path, maximum: int):
    with path.open("rb") as source:
        raw = source.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError("invalid_configuration")
    return raw


@dataclass(frozen=True)
class RuntimeConfig:
    public_origin: str
    release_sha: str

    @classmethod
    def load(cls, path: Path):
        try:
            value = json.loads(read_bounded(path, 4096), object_pairs_hook=unique_fields)
            if type(value) is not dict or set(value) != {"public_origin", "release_sha"}:
                raise ValueError()
            origin, release = value["public_origin"], value["release_sha"]
            if (type(origin) is not str or not re.fullmatch(r"https://[a-z0-9.-]+(?::[0-9]+)?", origin)
                    or type(release) is not str or not re.fullmatch(r"[a-f0-9]{40}", release)):
                raise ValueError()
            parsed = urlsplit(origin)
            port = parsed.port
            canonical = "https://" + parsed.hostname + (f":{port}" if port not in (None, 443) else "")
            if origin != canonical or port == 0:
                raise ValueError()
            return cls(origin, release)
        except (OSError, ValueError, TypeError):
            raise ValueError("invalid_configuration") from None
