"""Frozen nonsecret host inputs and exact presenter-secret response binding."""
import base64
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import re
import uuid


class HostError(ValueError):
    """Only fixed public stage names may cross the host logging boundary."""


def unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")


@dataclass(frozen=True)
class HostInputs:
    region: str
    generation_id: str
    release_sha: str
    public_origin: str
    data_volume_ocid: str
    data_device: str
    artifact_namespace: str
    artifact_bucket: str
    artifact_object: str
    artifact_sha256: str
    artifact_image_id: str
    presenter_secret_ocid: str
    presenter_secret_version: str

    @classmethod
    def from_imds(cls, value):
        patterns = {
            "generation_id": r"[a-f0-9]{32}", "release_sha": r"[a-f0-9]{40}",
            "public_origin": r"https://[a-z0-9]+(?:[.-][a-z0-9]+)*",
            "data_volume_ocid": r"ocid1\.volume\.oc1\.[a-z0-9-]+\.[A-Za-z0-9]+",
            "data_device": r"/dev/oracleoci/oraclevdb",
            "artifact_namespace": r"[a-z0-9-]{1,64}",
            "artifact_bucket": r"[a-z0-9][a-z0-9-]{0,62}",
            "artifact_object": r"[a-z0-9][a-z0-9/_.-]{0,254}",
            "artifact_sha256": r"[a-f0-9]{64}", "artifact_image_id": r"sha256:[a-f0-9]{64}",
            "presenter_secret_ocid": r"ocid1\.vaultsecret\.oc1\.[a-z0-9-]+\.[A-Za-z0-9]+",
            "presenter_secret_version": r"[1-9][0-9]{0,9}",
        }
        try:
            region, metadata = value["canonicalRegionName"], value["metadata"]
            if type(region) is not str or len(region) > 64 or not re.fullmatch(
                    r"[a-z][a-z0-9]*(?:-[a-z0-9]+)+-[1-9][0-9]*", region):
                raise ValueError()
            fields = {key: metadata[key] for key in patterns}
            for key, item in fields.items():
                if type(item) is not str or len(item) > 255 or not re.fullmatch(patterns[key], item):
                    raise ValueError()
            if int(fields["presenter_secret_version"]) > 2147483647:
                raise ValueError()
            return cls(region=region, **fields)
        except (KeyError, TypeError, ValueError):
            raise HostError("host_configuration_invalid") from None

    @property
    def identity(self):
        value = asdict(self)
        del value["presenter_secret_version"]
        return value

    @property
    def revision(self):
        # Only nonsecret metadata; never hash a password or verifier here.
        return sha256(encoded(asdict(self))).hexdigest()

    @property
    def filesystem_uuid(self):
        return str(uuid.uuid5(uuid.NAMESPACE_URL, "oci-troubleshooter:" + self.generation_id
                              + ":" + self.data_volume_ocid))

    @property
    def app_configuration(self):
        return {"public_origin": self.public_origin, "release_sha": self.release_sha}


def secret_content(raw, inputs):
    try:
        if type(raw) is not bytes or len(raw) > 16384:
            raise ValueError()
        value = json.loads(raw, object_pairs_hook=unique_fields)["data"]
        content = value["secret-bundle-content"]
        if (value["secret-id"] != inputs.presenter_secret_ocid
                or type(value["version-number"]) is not int
                or value["version-number"] != int(inputs.presenter_secret_version)
                or content["content-type"] != "BASE64" or type(content["content"]) is not str):
            raise ValueError()
        decoded = base64.b64decode(content["content"], validate=True)
        if not 1 <= len(decoded) <= 1024 or base64.b64encode(decoded).decode() != content["content"]:
            raise ValueError()
        return decoded
    except (KeyError, TypeError, ValueError, UnicodeError):
        raise HostError("presenter_secret_invalid") from None
