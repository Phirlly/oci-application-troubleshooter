import json
import unittest
from unittest.mock import patch

from oci.resource_manager.models import CreateStackDetails, CreateZipUploadConfigSourceDetails

from troubleshooter_deployment.deployment_config import parse_tfvars
from troubleshooter_deployment.oci_client import bounded_client
from tests.contract.oci_responses import CONFIG, SyntheticSigner, response


class ResourceManagerConfigurationTests(unittest.TestCase):
    def test_single_target_reaches_actual_sdk_request_without_quoted_strings_or_credentials(self):
        document = {"region": "us-ashburn-1",
                    "compartment_ocid": "ocid1.compartment.oc1..synthetic"}
        target = parse_tfvars(json.dumps(document))
        scope = target.scope(101, "demo", CONFIG["tenancy"])
        with patch("oci.resource_manager.resource_manager_client.Signer",
                   return_value=SyntheticSigner()):
            client = bounded_client(CONFIG, scope, "resourcemanager")
        self.addCleanup(client.base_client.session.close)
        with patch("oci.base_client.OCIHTTPAdapter.send", return_value=response({})) as send:
            client.create_stack(CreateStackDetails(
                compartment_id=scope.compartment_id,
                config_source=CreateZipUploadConfigSourceDetails(zip_file_base64_encoded="AA=="),
                terraform_version="1.5.x", variables=target.resource_manager_variables(),
            ), opc_retry_token="synthetic-retry-token")
        self.assertEqual(send.call_count, 1)
        request = send.call_args.args[0]
        self.assertEqual(request.url, "https://resourcemanager.us-ashburn-1.oraclecloud.com/20180917/stacks")
        body = json.loads(request.body)
        self.assertEqual(body["compartmentId"], scope.compartment_id)
        self.assertEqual(body["variables"], document)
        for credential in [CONFIG["key_content"], CONFIG["user"], CONFIG["fingerprint"], CONFIG["tenancy"]]:
            self.assertNotIn(credential, request.body)


if __name__ == "__main__":
    unittest.main()
