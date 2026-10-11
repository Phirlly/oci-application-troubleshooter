from dataclasses import replace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from troubleshooter_deployment.bootstrap_commitment import freeze_stack_request
from troubleshooter_deployment.oci_owned_stack import OciOwnedStack, OwnedStackError, OwnedStackObservation
from troubleshooter_deployment.stack_bootstrap import BOOTSTRAP_VARIABLE, build_initial_stack
from troubleshooter_deployment.stack_ownership import freeze_stack_ownership
from tests.contract.oci_responses import CONFIG, OCI_IDENTITY, SyntheticSigner, response
from tests.contract.test_oci_stack_bootstrap import STACK_ID, stack_data
from tests.deployment.fixtures import ATTEMPT


class OciOwnedStackTests(unittest.TestCase):
    def setUp(self):
        patch("oci.resource_manager.resource_manager_client.Signer", return_value=SyntheticSigner()).start()
        self.send = patch("oci.base_client.OCIHTTPAdapter.send").start()
        self.addCleanup(patch.stopall)
        self.request = build_initial_stack(OCI_IDENTITY.scope, ATTEMPT, CONFIG["user"])
        self.bootstrap = freeze_stack_request(self.request.scope, ATTEMPT, self.request.body)
        self.ownership = freeze_stack_ownership(self.bootstrap, STACK_ID)
        self.client = OciOwnedStack(CONFIG, self.request, self.ownership)
        self.addCleanup(self.client.close)

    def test_complete_listing_matches_immutable_id_then_gets_evolved_stack_without_jobs(self):
        data = stack_data(self.request)
        data["variables"]["workload_setting"] = "changed"
        data["freeformTags"]["extra"] = "allowed"
        data["terraformVersion"] = "changed"
        data["configSource"] = {"configSourceType": "ZIP_UPLOAD", "workingDirectory": "workload"}
        self.send.side_effect = [
            response([{"id": "different-stack"}, {"id": STACK_ID}], headers={"opc-next-page": "opaque+/="}),
            response([]), response(data, headers={"etag": '"fresh"'}),
        ]
        found = self.client.read()
        self.assertIsInstance(found, OwnedStackObservation)
        self.assertEqual((found.stack_id, found.etag, found.request), (STACK_ID, '"fresh"', self.request))
        requests = [call.args[0] for call in self.send.call_args_list]
        self.assertEqual([item.method for item in requests], ["GET"] * 3)
        query = {"compartmentId": [self.request.scope.compartment_id], "displayName": [self.request.display_name],
                 "sortBy": ["DISPLAYNAME"], "limit": ["10"]}
        self.assertEqual(parse_qs(urlsplit(requests[0].url).query), query)
        self.assertEqual(parse_qs(urlsplit(requests[1].url).query), {**query, "page": ["opaque+/="]})
        self.assertEqual(urlsplit(requests[2].url).path, "/20180917/stacks/" + STACK_ID)

    def test_names_tags_and_empty_results_never_substitute_for_exact_id(self):
        data = stack_data(self.request)
        for listing in [[], [{**data, "id": "other-stack"}]]:
            self.send.reset_mock()
            self.send.side_effect = [response(listing)]
            with self.assertRaises(OwnedStackError):
                self.client.read()
            self.assertEqual(self.send.call_count, 1)

    def test_partial_malformed_denied_or_repeated_pages_never_reach_get(self):
        cases = [
            [response({})], [response([None])], [response([{}])],
            [response([{"id": "bad/id"}])], [response([{"id": STACK_ID}] * 2)],
            [response([{"id": str(n)} for n in range(11)])], [response([], status=403)],
            [response([{"id": STACK_ID}], headers={"opc-next-page": ""})],
            [response([{"id": STACK_ID}], headers={"opc-next-page": "x" * 1025})],
            [response([{"id": STACK_ID}], headers={"opc-next-page": "next"}), response([], status=403)],
            [response([{"id": STACK_ID}], headers={"opc-next-page": "next"}),
             response([], headers={"opc-next-page": "next"})],
            [response([{"id": STACK_ID}], headers={"opc-next-page": "next"}), response([{"id": STACK_ID}])],
            [response([], headers={"opc-next-page": str(n)}) for n in range(5)],
        ]
        for replies in cases:
            self.send.reset_mock()
            self.send.side_effect = replies
            with self.assertRaises(OwnedStackError):
                self.client.read()
            self.assertEqual(self.send.call_count, len(replies))
            self.assertTrue(all(urlsplit(call.args[0].url).path == "/20180917/stacks"
                                for call in self.send.call_args_list))

    def test_independent_readback_requires_exact_identity_and_reserved_metadata(self):
        original = stack_data(self.request)
        changes = [{"id": "other-stack"}, {"compartmentId": "other"}, {"displayName": "other"},
                   {"lifecycleState": "CREATING"}, {"lifecycleState": "DELETED"}, {"lifecycleState": None},
                   {"freeformTags": {}}, {"freeformTags": []}, {"variables": {}}, {"variables": []},
                   {"variables": {BOOTSTRAP_VARIABLE: "changed"}},
                   {"variables": {**original["variables"], "invalid": 123}},
                   {"freeformTags": {**original["freeformTags"], "invalid": 123}}]
        for change in changes:
            self.send.side_effect = [response([{"id": STACK_ID}]), response({**original, **change})]
            with self.assertRaises(OwnedStackError):
                self.client.read()
        for result in [response(original, status=404), response([]),
                       response(original, headers={"etag": "W/weak"}), response(original, headers={"etag": None})]:
            self.send.side_effect = [response([{"id": STACK_ID}]), result]
            with self.assertRaises(OwnedStackError):
                self.client.read()

    def test_invalid_inputs_and_failed_transport_remain_sanitized(self):
        for config, request, ownership in [
            ({**CONFIG, "user": "other"}, self.request, self.ownership),
            (CONFIG, replace(self.request, token="bad"), self.ownership),
            (CONFIG, self.request, replace(self.ownership, bootstrap=replace(self.bootstrap, request_sha256="f" * 64))),
            (CONFIG, self.request, replace(self.ownership, schema=True)),
        ]:
            with self.assertRaises(OwnedStackError):
                OciOwnedStack(config, request, ownership)
        self.send.assert_not_called()
        self.send.side_effect = OSError("private-provider-value")
        with self.assertRaises(OwnedStackError) as error:
            self.client.read()
        self.assertNotIn("private-provider-value", str(error.exception))
        self.assertEqual(self.send.call_count, 1)
        self.send.side_effect = [response([{"id": STACK_ID}]), response(stack_data(self.request))]
        with patch("troubleshooter_deployment.request_budget.monotonic", side_effect=[0, 21]):
            with self.assertRaises(OwnedStackError):
                self.client.read()
        with patch.object(self.client._client.base_client.session, "close", side_effect=OSError("private")):
            with self.assertRaises(OwnedStackError):
                self.client.close()
