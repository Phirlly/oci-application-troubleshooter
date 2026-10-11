from dataclasses import replace
import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from troubleshooter_deployment.oci_stack_bootstrap import OciStackBootstrap, StackBootstrapError
from troubleshooter_deployment.stack_bootstrap import build_initial_stack
from tests.contract.oci_responses import CONFIG, OCI_IDENTITY, SyntheticSigner, response
from tests.deployment.fixtures import ATTEMPT

CREATED = "2026-10-10T01:00:00Z"
NOW = 1791594000
STACK_ID = "test.ormstack.oc1.iad.synthetic"


def stack_data(request):
    body = json.loads(request.body)
    return {"id": STACK_ID, "compartmentId": body["compartmentId"], "displayName": body["displayName"],
            "lifecycleState": "ACTIVE", "terraformVersion": "1.5.x", "variables": body["variables"],
            "freeformTags": body["freeformTags"], "configSource": {"configSourceType": "ZIP_UPLOAD"}}


class OciStackBootstrapTests(unittest.TestCase):
    def setUp(self):
        patch("oci.resource_manager.resource_manager_client.Signer", return_value=SyntheticSigner()).start()
        patch("troubleshooter_deployment.stack_bootstrap.time", return_value=NOW).start()
        self.send = patch("oci.base_client.OCIHTTPAdapter.send").start()
        self.addCleanup(patch.stopall)
        self.request = build_initial_stack(OCI_IDENTITY.scope, ATTEMPT, CONFIG["user"])
        self.client = OciStackBootstrap(CONFIG, self.request)
        self.addCleanup(self.client.close)

    def test_exact_frozen_bytes_token_ack_get_and_empty_jobs(self):
        data = stack_data(self.request)
        self.send.side_effect = [response(data), response(data, headers={"etag": '"get-revision"'}), response([])]
        owned = self.client.create(CREATED)
        self.assertEqual((owned.stack_id, owned.etag, owned.request), (STACK_ID, '"get-revision"', self.request))
        requests = [call.args[0] for call in self.send.call_args_list]
        self.assertEqual([request.method for request in requests], ["POST", "GET", "GET"])
        self.assertEqual(requests[0].body, self.request.body)
        self.assertEqual(requests[0].headers["opc-retry-token"], self.request.token)
        self.assertEqual(urlsplit(requests[0].url).path, "/20180917/stacks")
        self.assertEqual(urlsplit(requests[1].url).path, "/20180917/stacks/" + STACK_ID)
        self.assertEqual(parse_qs(urlsplit(requests[2].url).query), {"stackId": [STACK_ID], "limit": ["1"]})

    def test_conflict_read_uses_exact_name_sort_and_never_adopts_candidate(self):
        self.send.return_value = response([])
        self.client.check_name_available()
        query = parse_qs(urlsplit(self.send.call_args.args[0].url).query)
        self.assertEqual(query, {"compartmentId": [OCI_IDENTITY.scope.compartment_id],
                                "displayName": [self.request.display_name], "sortBy": ["DISPLAYNAME"], "limit": ["1"]})
        for result in [response([stack_data(self.request)]), response([], headers={"opc-next-page": "next"}),
                       response({}), response([], status=403)]:
            self.send.return_value = result
            with self.assertRaises(StackBootstrapError):
                self.client.check_name_available()
        self.assertTrue(all(call.args[0].method == "GET" for call in self.send.call_args_list))

    def test_raw_ack_and_readback_validation_rejects_wrong_or_malformed_fields(self):
        data = stack_data(self.request)
        variants = [{"id": None}, {"id": "bad/id"}, {"compartmentId": "other"}, {"lifecycleState": "CREATING"},
                    {"displayName": "other"}, {"variables": {}}, {"variables": []}, {"freeformTags": {}},
                    {"terraformVersion": "1.6.x"}, {"configSource": []},
                    {"configSource": {"configSourceType": "GIT_CONFIG_SOURCE"}},
                    {"configSource": {"configSourceType": "ZIP_UPLOAD", "workingDirectory": "nested"}}]
        for changes in variants:
            for readback in (False, True):
                self.send.reset_mock()
                bad = response({**data, **changes})
                self.send.side_effect = [response(data), bad] if readback else [bad]
                with self.assertRaises(StackBootstrapError):
                    self.client.create(CREATED)
                self.assertEqual(self.send.call_count, 2 if readback else 1)
        self.send.side_effect = [response(data), response({**data, "id": "other-stack"})]
        with self.assertRaises(StackBootstrapError):
            self.client.create(CREATED)

    def test_root_directory_normalization_is_supported(self):
        for directory in (None, ""):
            data = stack_data(self.request)
            data["configSource"]["workingDirectory"] = directory
            self.send.side_effect = [response(data), response(data), response([])]
            self.assertEqual(self.client.create(CREATED).stack_id, STACK_ID)

    def test_incomplete_or_failed_evidence_never_returns_ownership_or_retries(self):
        data = stack_data(self.request)
        for bad in [response(data, status=201), response([], status=404), response(data, headers={"etag": "W/weak"}),
                    response(data, headers={"etag": None}), response([]), response(body=b'{"id":1,"id":2}')]:
            self.send.reset_mock()
            self.send.side_effect = [bad]
            with self.assertRaises(StackBootstrapError):
                self.client.create(CREATED)
            self.assertEqual(self.send.call_count, 1)
        for bad in [response([{"id": "job"}]), response({}), response([], status=403),
                    response([], headers={"opc-next-page": "next"})]:
            self.send.side_effect = [response(data), response(data), bad]
            with self.assertRaises(StackBootstrapError):
                self.client.create(CREATED)
        self.send.reset_mock()
        self.send.side_effect = OSError("private-provider-message")
        with self.assertRaises(StackBootstrapError) as error:
            self.client.create(CREATED)
        self.assertNotIn("private-provider-message", str(error.exception))
        self.assertEqual(self.send.call_count, 1)

    def test_invalid_request_config_and_expiry_stop_before_dispatch(self):
        for config, request in [(CONFIG, replace(self.request, token="tampered")),
                                ({**CONFIG, "user": "different-user"}, self.request),
                                ({**CONFIG, "region": "us-phoenix-1"}, self.request)]:
            with self.assertRaises(StackBootstrapError):
                OciStackBootstrap(config, request)
        with patch("troubleshooter_deployment.stack_bootstrap.time", return_value=NOW + 23 * 3600):
            with self.assertRaises(StackBootstrapError):
                self.client.create(CREATED)
        self.send.assert_not_called()

    def test_rejected_token_and_transient_errors_never_get_an_implicit_retry(self):
        for status in [400, 409, 429, 500, 503]:
            self.send.reset_mock()
            self.send.side_effect = [response({"code": "InvalidatedRetryToken", "message": "private"}, status=status)]
            with self.assertRaises(StackBootstrapError):
                self.client.create(CREATED)
            self.assertEqual(self.send.call_count, 1)

    def test_close_and_late_result_fail_sanitized(self):
        data = stack_data(self.request)
        self.send.side_effect = [response(data), response(data), response([])]
        with patch("troubleshooter_deployment.request_budget.monotonic", side_effect=[0, 21]):
            with self.assertRaises(StackBootstrapError):
                self.client.create(CREATED)
        with patch.object(self.client._client.base_client.session, "close", side_effect=OSError("private")):
            with self.assertRaises(StackBootstrapError) as error:
                self.client.close()
            self.assertNotIn("private", str(error.exception))
