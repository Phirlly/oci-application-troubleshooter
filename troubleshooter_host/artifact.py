"""Read the exact private application image and verifier using instance identity."""
from hashlib import sha256
import json
from pathlib import Path
import tempfile

from .host_io import command
from .inputs import HostError, secret_content, unique_fields

MAX_ARCHIVE = 2 * 1024**3


def oci(inputs):
    return ["oci", "--auth", "instance_principal", "--region", inputs.region,
            "--config-file", "/dev/null", "--cli-rc-file", "/dev/null",
            "--connection-timeout", "5", "--read-timeout", "30", "--no-retry", "--output", "json"]


def verify_image(inputs):
    try:
        result = json.loads(command(["podman", "image", "inspect", inputs.artifact_image_id]),
                            object_pairs_hook=unique_fields)
        if (len(result) != 1 or result[0]["Id"] not in (inputs.artifact_image_id, inputs.artifact_image_id[7:])
                or result[0]["Architecture"] != "amd64" or result[0]["Os"] != "linux"
                or result[0]["Config"]["User"] != "10001:10001"
                or result[0]["Config"]["Entrypoint"] != ["python", "-m", "troubleshooter_app"]):
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise HostError("artifact_invalid") from None


def acquire_image(inputs, parent):
    with tempfile.TemporaryDirectory(prefix="artifact-", dir=parent) as temporary:
        archive = Path(temporary) / "application.tar"
        command(oci(inputs) + ["os", "object", "get", "--namespace-name", inputs.artifact_namespace,
                              "--bucket-name", inputs.artifact_bucket, "--name", inputs.artifact_object,
                              "--file", str(archive), "--no-multipart"], timeout=600, file_limit=MAX_ARCHIVE)
        digest, total = sha256(), 0
        with archive.open("rb") as source:
            while True:
                part = source.read(4 * 1024**2)
                if not part:
                    break
                total += len(part)
                if total > MAX_ARCHIVE:
                    raise HostError("artifact_invalid")
                digest.update(part)
        if not total or digest.hexdigest() != inputs.artifact_sha256:
            raise HostError("artifact_invalid")
        command(["podman", "load", "--input", str(archive)], timeout=300)
    verify_image(inputs)


def acquire_verifier(inputs):
    return secret_content(command(oci(inputs) + ["secrets", "secret-bundle", "get", "--secret-id",
                                                 inputs.presenter_secret_ocid, "--version-number",
                                                 inputs.presenter_secret_version],
                                  timeout=60, maximum=16384), inputs)
