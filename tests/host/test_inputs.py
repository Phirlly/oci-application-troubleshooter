import base64
from copy import deepcopy
import json
import unittest

from troubleshooter_host.inputs import HostInputs, HostError, secret_content


def metadata():
    return {
        "canonicalRegionName": "us-ashburn-1",
        "metadata": {
            "generation_id": "a" * 32, "release_sha": "b" * 40,
            "public_origin": "https://synthetic.apigateway.example.test",
            "data_volume_ocid": "ocid1.volume.oc1.iad.synthetic",
            "data_device": "/dev/oracleoci/oraclevdb",
            "artifact_namespace": "acme-dev", "artifact_bucket": "synthetic-artifacts",
            "artifact_object": "releases/application.tar", "artifact_sha256": "c" * 64,
            "artifact_image_id": "sha256:" + "d" * 64,
            "presenter_secret_ocid": "ocid1.vaultsecret.oc1.iad.synthetic",
            "presenter_secret_version": "1",
        },
    }


class HostInputTests(unittest.TestCase):
    def test_frozen_identity_excludes_only_rotatable_secret_version(self):
        first = HostInputs.from_imds(metadata())
        changed = metadata()
        changed["metadata"]["presenter_secret_version"] = "2"
        second = HostInputs.from_imds(changed)
        self.assertEqual(first.identity, second.identity)
        self.assertNotEqual(first.revision, second.revision)
        self.assertEqual(first.filesystem_uuid, second.filesystem_uuid)
        self.assertEqual(first.app_configuration,
                         {"public_origin": metadata()["metadata"]["public_origin"], "release_sha": "b" * 40})
        changed["metadata"]["data_volume_ocid"] = "ocid1.volume.oc1.iad.another"
        third = HostInputs.from_imds(changed)
        self.assertNotEqual(first.identity, third.identity)
        self.assertNotEqual(first.filesystem_uuid, third.filesystem_uuid)

    def test_metadata_excludes_provider_owned_user_data_from_host_contract(self):
        value = metadata()
        value["metadata"]["user_data"] = "provider-owned-code"
        value["metadata"]["ssh_authorized_keys"] = ""
        self.assertEqual(HostInputs.from_imds(value), HostInputs.from_imds(metadata()))

    def test_invalid_inputs_fail_with_fixed_error(self):
        for field, value in [
            ("generation_id", "a" * 31), ("release_sha", "b" * 41),
            ("public_origin", "http://example.test"), ("public_origin", "https://example.test/path"),
            ("data_device", "/dev/oracleoci/oraclevda"),
            ("data_volume_ocid", "ocid1.instance.oc1.iad.synthetic"),
            ("artifact_namespace", "bad/namespace"), ("artifact_bucket", "bad'quote"),
            ("artifact_object", "bad\n"), ("artifact_image_id", "latest"),
            ("artifact_sha256", None), ("artifact_namespace", "x" * 65),
            ("presenter_secret_version", "0"), ("presenter_secret_version", "1.5"),
            ("presenter_secret_version", "2147483648"), ("presenter_secret_version", 1),
            ("presenter_secret_ocid", "ocid1.secret.oc1.iad.synthetic"),
        ]:
            with self.subTest(field=field, value=value):
                document = metadata()
                document["metadata"][field] = value
                with self.assertRaisesRegex(HostError, "^host_configuration_invalid$"):
                    HostInputs.from_imds(document)
        for value in [None, [], {}, {"canonicalRegionName": "bad", "metadata": {}},
                      {**metadata(), "canonicalRegionName": "bad"}, {**metadata(), "metadata": None}]:
            with self.assertRaises(HostError):
                HostInputs.from_imds(value)

    def test_secret_binds_exact_id_version_encoding_and_size(self):
        inputs = HostInputs.from_imds(metadata())
        raw = b'{"schema":1,"synthetic":"verifier"}'
        bundle = {"data": {"secret-id": inputs.presenter_secret_ocid, "version-number": 1,
                            "secret-bundle-content": {"content-type": "BASE64",
                                                      "content": base64.b64encode(raw).decode()}}}
        self.assertEqual(secret_content(json.dumps(bundle).encode(), inputs), raw)
        for field, value in [("secret-id", "another"), ("version-number", 2), ("version-number", True)]:
            bad = deepcopy(bundle)
            bad["data"][field] = value
            with self.assertRaisesRegex(HostError, "^presenter_secret_invalid$"):
                secret_content(json.dumps(bad).encode(), inputs)
        for value in ["!", base64.b64encode(b"x" * 1025).decode(), ""]:
            bad = deepcopy(bundle)
            bad["data"]["secret-bundle-content"]["content"] = value
            with self.assertRaises(HostError):
                secret_content(json.dumps(bad).encode(), inputs)
        for raw in [b"{}", b"null", b"x" * 16385, b'{"data":{},"data":{}}']:
            with self.assertRaises(HostError):
                secret_content(raw, inputs)
