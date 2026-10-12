"""Synthetic inputs only: no OCI identities, network calls or real credentials."""

VALUES = {
    "region": "us-ashburn-1",
    "compartment_ocid": "ocid1.compartment.oc1..synthetic",
    "automation": {
        "generation_id": "a" * 32,
        "release_sha": "b" * 40,
        "tenancy_ocid": "ocid1.tenancy.oc1..synthetic",
        "home_region": "us-phoenix-1",
        "availability_domain": "SYN:US-ASHBURN-AD-1",
        "image_ocid": "ocid1.image.oc1.iad.synthetic",
        "shape": "VM.Standard.E4.Flex",
        "artifact_namespace": "synthetic",
        "artifact_bucket": "synthetic-artifacts",
        "artifact_object": "releases/" + "b" * 40 + "/application.tar",
        "artifact_sha256": "c" * 64,
        "artifact_image_id": "sha256:" + "d" * 64,
        "presenter_secret_ocid": "ocid1.vaultsecret.oc1.iad.synthetic",
        "presenter_secret_version": 1,
    },
}


def input_cases():
    yield VALUES, True
    for field, value in [
        ("artifact_namespace", "acme-dev"),
        ("artifact_namespace", "a" * 31 + "-" + "b" * 32),
        ("artifact_object", "a" * 255),
        ("presenter_secret_version", 2147483647),
        ("shape", "VM.Standard.E5.Flex"),
    ]:
        yield {**VALUES, "automation": {**VALUES["automation"], field: value}}, True
    for field, value in [
        ("generation_id", "a" * 31), ("release_sha", "b" * 41),
        ("tenancy_ocid", "ocid1.compartment.oc1..synthetic"),
        ("home_region", "not-a-region"), ("availability_domain", "bad\n"),
        ("image_ocid", "latest"), ("shape", "VM.Standard.A1.Flex"),
        ("artifact_namespace", "bad'namespace"), ("artifact_namespace", ""),
        ("artifact_namespace", "a" * 65), ("artifact_namespace", "UPPERCASE"),
        ("artifact_namespace", "bad/namespace"), ("artifact_namespace", "bad\n"),
        ("artifact_namespace", "unicode-é"), ("artifact_namespace", None),
        ("artifact_bucket", "UPPERCASE"),
        ("artifact_object", "bad'object"), ("artifact_object", "a" * 256),
        ("artifact_object", "unicode-é"), ("artifact_object", None),
        ("artifact_sha256", "c" * 63),
        ("artifact_image_id", "latest"), ("artifact_image_id", None),
        ("presenter_secret_ocid", "ocid1.vaultsecret.oc1.iad." + "a" * 256),
        ("presenter_secret_version", 0), ("presenter_secret_version", 1.5),
        ("presenter_secret_version", 2147483648), ("presenter_secret_version", None),
    ]:
        yield {**VALUES, "automation": {**VALUES["automation"], field: value}}, False
    yield {**VALUES, "automation": None}, False
