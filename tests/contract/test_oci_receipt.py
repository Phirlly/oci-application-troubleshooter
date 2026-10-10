from dataclasses import replace
import json
import os
import unittest
from unittest.mock import patch

from urllib3.exceptions import HeaderParsingError

from troubleshooter_deployment.cleanup import may_purge, prepare_purge
from troubleshooter_deployment.oci_receipt import (
    OciReceiptStore, ReceiptError, ReceiptConflict, ReceiptUncertain,
)
from troubleshooter_deployment.policy import bootstrap
from troubleshooter_deployment.receipt_variables import merge_receipt
from tests.deployment.fixtures import ATTEMPT, REMOVAL
from tests.contract.oci_responses import CONFIG, OCI_IDENTITY, SyntheticSigner, response

STACK_ID = "synthetic-stack"


def stack(variables=None, **changes):
    return {"id": STACK_ID, "compartmentId": OCI_IDENTITY.scope.compartment_id,
            "lifecycleState": "ACTIVE", "variables": {} if variables is None else variables,
            "freeformTags": {"keep": "unchanged"}, **changes}


class OciReceiptTests(unittest.TestCase):
    def setUp(self):
        patch("oci.resource_manager.resource_manager_client.Signer", return_value=SyntheticSigner()).start()
        self.addCleanup(patch.stopall)
        self.store = OciReceiptStore(CONFIG, OCI_IDENTITY, STACK_ID)
        self.addCleanup(self.store.close)
        self.send = patch("oci.base_client.OCIHTTPAdapter.send").start()
        self.record = prepare_purge(bootstrap(OCI_IDENTITY, OCI_IDENTITY, ATTEMPT), REMOVAL)
        self.original = {"region": "us-ashburn-1", "label": "日本語"}
        self.variables = merge_receipt(self.original, self.record.receipt)

    def successful_replies(self):
        replies = [response(stack(self.original)), response(stack(self.variables)),
                   response(stack(self.variables), headers={"etag": '"revision-2"'})]
        self.send.side_effect = replies
        return replies

    def test_exact_conditional_variables_only_request_and_independent_readback(self):
        replies = self.successful_replies()
        observed = self.store.persist(self.record)
        self.assertEqual(observed, self.record.receipt)
        self.assertTrue(may_purge(self.record, observed))
        calls = self.send.call_args_list
        self.assertEqual([c.args[0].method for c in calls], ["GET", "PUT", "GET"])
        for call in calls:
            request = call.args[0]
            self.assertEqual(request.url,
                             "https://resourcemanager.us-ashburn-1.oraclecloud.com/20180917/stacks/" + STACK_ID)
            self.assertEqual(request.headers["authorization"], "synthetic-signature")
            self.assertTrue(call.kwargs["verify"])
            self.assertEqual(call.kwargs["timeout"], (5, 5))
        request = calls[1].args[0]
        self.assertEqual(request.headers["if-match"], '"revision-1"')
        self.assertEqual(json.loads(request.body), {"variables": self.variables})
        self.assertTrue(all(reply.raw.closed for reply in replies))

    def test_read_absence_requires_successful_known_stack_and_repeated_persist_does_not_put(self):
        self.send.side_effect = [response(stack(self.original)), response(stack(self.variables)),
                                 response(stack(self.variables))]
        self.assertIsNone(self.store.read())
        self.assertEqual(self.store.read(), self.record.receipt)
        self.assertEqual(self.store.persist(self.record), self.record.receipt)
        self.assertTrue(all(c.args[0].method == "GET" for c in self.send.call_args_list))

    def test_lost_write_is_uncertain_then_new_read_reconciles_without_second_put(self):
        self.send.side_effect = [response(stack(self.original)), OSError("synthetic-secret"),
                                 response(stack(self.variables))]
        with self.assertRaises(ReceiptUncertain) as error:
            self.store.persist(self.record)
        self.assertNotIn("synthetic-secret", str(error.exception))
        self.assertEqual(self.store.persist(self.record), self.record.receipt)
        self.assertEqual([c.args[0].method for c in self.send.call_args_list], ["GET", "PUT", "GET"])

    def test_only_put412_is_conflict_and_no_automatic_retries(self):
        for status in [401, 403, 404, 409, 412, 429, 500]:
            self.send.reset_mock()
            self.send.side_effect = [response(stack(self.original)),
                                     response({"code": "Failure", "message": "synthetic-secret"}, status=status)]
            with self.assertRaises(ReceiptConflict if status == 412 else ReceiptUncertain) as error:
                self.store.persist(self.record)
            self.assertNotIn("synthetic-secret", str(error.exception))
            self.assertEqual(self.send.call_count, 2)

    def test_readback_failure_or_mismatch_never_returns_receipt(self):
        for after in [OSError("synthetic-secret"), response(stack(self.original)),
                      response(stack(dict(self.variables, region="changed"))),
                      response(stack(self.variables, id="replacement")),
                      response({"code": "IfMatchFailed"}, status=412)]:
            self.send.reset_mock()
            self.send.side_effect = [response(stack(self.original)), response(stack(self.variables)), after]
            with self.assertRaises(ReceiptUncertain):
                self.store.persist(self.record)
            self.assertEqual(self.send.call_count, 3)

    def test_invalid_update_ack_stays_uncertain(self):
        for ack in [response(stack(self.variables), status=202), response(stack(self.original)),
                    response(stack(self.variables, compartmentId="other")),
                    response(stack(self.variables), headers={"etag": "*"})]:
            self.send.reset_mock()
            self.send.side_effect = [response(stack(self.original)), ack]
            with self.assertRaises(ReceiptUncertain):
                self.store.persist(self.record)
            self.assertEqual(self.send.call_count, 2)

    def test_missing_denied_or_inactive_stack_never_means_absent_receipt(self):
        failures = [response({"code": "NotAuthorizedOrNotFound"}, status=s) for s in [401, 403, 404, 429, 500]]
        failures += [response(stack(id="other")), response(stack(compartmentId="other")),
                     response(stack(lifecycleState="DELETED")), response(stack(lifecycleState=True)),
                     response(stack(), status=202), response([]),
                     response(stack(), headers={"etag": "*"}), response(dict(stack(), variables=None)),
                     response({k: v for k, v in stack().items() if k != "variables"})]
        for reply in failures:
            self.send.reset_mock()
            self.send.side_effect = [reply]
            with self.assertRaises(ReceiptError):
                self.store.read()
            self.assertEqual(self.send.call_count, 1)

    def test_foreign_or_conflicting_receipt_is_not_overwritten(self):
        for receipt in [replace(self.record.receipt, identity=replace(OCI_IDENTITY, epoch="9" * 32)),
                        replace(self.record.receipt, removal_id="9" * 32)]:
            self.send.reset_mock()
            self.send.side_effect = [response(stack(merge_receipt({}, receipt)))]
            with self.assertRaises(ReceiptError):
                self.store.persist(self.record)
            self.assertEqual(self.send.call_count, 1)

    def test_partial_carrier_or_capacity_failure_blocks_put(self):
        for variables in [{"troubleshooter_cleanup_receipt_0": "partial"},
                          {f"v{i}": "" for i in range(248)},
                          {f"v{i}": "a" * 4015 for i in range(65)}]:
            self.send.reset_mock()
            self.send.side_effect = [response(stack(variables))]
            with self.assertRaises(ReceiptError):
                self.store.persist(self.record)
            self.assertEqual(self.send.call_count, 1)

    def test_invalid_record_and_configuration_block_before_network(self):
        for record in [True, replace(self.record, receipt=None),
                       replace(self.record, identity=replace(OCI_IDENTITY, epoch="9" * 32))]:
            with self.assertRaises(ReceiptError):
                self.store.persist(record)
        for config, identity, identifier in [
            (dict(CONFIG, region="us-phoenix-1"), OCI_IDENTITY, STACK_ID),
            (dict(CONFIG, tenancy="other"), OCI_IDENTITY, STACK_ID),
            (dict(CONFIG, key_file="synthetic-secret"), OCI_IDENTITY, STACK_ID),
            (dict(CONFIG, user=True), OCI_IDENTITY, STACK_ID),
            (CONFIG, OCI_IDENTITY, "../stack"), (CONFIG, OCI_IDENTITY, ""),
            (CONFIG, replace(OCI_IDENTITY, epoch=True), STACK_ID),
        ]:
            with self.assertRaises(ReceiptError) as error:
                OciReceiptStore(config, identity, identifier)
            self.assertNotIn("synthetic-secret", str(error.exception))
        self.send.assert_not_called()

    def test_header_parser_has_no_hidden_retry_and_restores_environment(self):
        self.send.side_effect = [response(stack(self.original)), HeaderParsingError(["synthetic-secret"], "synthetic")]
        with patch.dict(os.environ, {"OCI_HEADER_PARSING_ERROR_MAX_RETRIES": "2"}):
            with self.assertRaises(ReceiptUncertain):
                self.store.persist(self.record)
            self.assertEqual(os.environ["OCI_HEADER_PARSING_ERROR_MAX_RETRIES"], "2")
        self.assertEqual(self.send.call_count, 2)
        self.send.side_effect = [response(body=b"x" * 262145)]
        with self.assertRaises(ReceiptError):
            self.store.read()

    def test_transport_redirect_duplicate_json_and_raw_type_rejection(self):
        for reply in [response(stack(), status=302, headers={"Location": "https://example.invalid"}),
                      response(body=b'{"id":"synthetic-stack","id":"synthetic-stack"}'),
                      response(stack({"region": True}))]:
            self.send.reset_mock()
            self.send.side_effect = [reply]
            with self.assertRaises(ReceiptError):
                self.store.read()
            self.assertEqual(self.send.call_count, 1)

    def test_late_readback_is_uncertain_and_late_read_cannot_acknowledge(self):
        self.successful_replies()
        with patch("troubleshooter_deployment.request_budget.monotonic", side_effect=[0, 21]):
            with self.assertRaises(ReceiptUncertain):
                self.store.persist(self.record)
        self.send.side_effect = [response(stack(self.variables))]
        with patch("troubleshooter_deployment.request_budget.monotonic", side_effect=[0, 21]):
            with self.assertRaises(ReceiptError):
                self.store.read()

    def test_close_failure_is_sanitized(self):
        with patch("troubleshooter_deployment.oci_transport.BoundedOciSession.close", side_effect=OSError("synthetic-secret")):
            with self.assertRaises(ReceiptError) as error:
                self.store.close()
            self.assertNotIn("synthetic-secret", str(error.exception))
