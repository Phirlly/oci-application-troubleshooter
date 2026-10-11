"""Stateful synthetic HTTP for recovery-store bootstrap; never a live fixture."""
from copy import deepcopy
from io import BytesIO
import json
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from troubleshooter_deployment.bucket_bootstrap import PREFIX, read_bootstrap
from troubleshooter_deployment.oci_stack_bootstrap import InitialStackAcknowledgement
from troubleshooter_deployment.recovery_bucket import creation_digest_v1
from troubleshooter_deployment.runner_recovery_store import bootstrap_recovery_store
from troubleshooter_deployment.stack_bootstrap import build_initial_stack
from tests.contract.oci_responses import CONFIG, OCI_IDENTITY, SyntheticSigner, response
from tests.contract.test_oci_audit import event, page
from tests.contract.test_oci_stack_bootstrap import STACK_ID, stack_data
from tests.deployment.fixtures import ATTEMPT
from tests.deployment.test_bucket_bootstrap import NOW


class RecoveryStoreService(unittest.TestCase):
    def setUp(self):
        self.initial = build_initial_stack(OCI_IDENTITY.scope, ATTEMPT, CONFIG["user"])
        self.stack, self.stack_version = stack_data(self.initial), 1
        self.stack["definedTags"] = {"Department": {"Name": "Demo"}}
        self.stack["variables"]["unrelated"] = "preserve"
        self.stack["freeformTags"]["Department"] = "Demo"
        self.anchor = InitialStackAcknowledgement(self.initial, STACK_ID, '"old-anchor"')
        self.calls, self.bucket, self.body, self.object_version = [], None, None, 0
        self.current, self.statuses, self.jobs, self.job_headers = ATTEMPT, {}, [], {}
        self.attempt_shas = {(ATTEMPT.run_id, ATTEMPT.number): ATTEMPT.source_sha}
        self.fault, self.audit_count = None, 1
        self.now = NOW
        for module in ("resource_manager.resource_manager_client", "object_storage.object_storage_client",
                       "loggingsearch.log_search_client"):
            patch("oci." + module + ".Signer", return_value=SyntheticSigner()).start()
        self.transport = patch("oci.base_client.OCIHTTPAdapter.send", side_effect=self.send).start()
        self.initial_call = patch("troubleshooter_deployment.runner_recovery_store.bootstrap_initial_stack",
                                  side_effect=lambda **kwargs: self.anchor).start()
        patch("troubleshooter_deployment.runner_recovery_store.time", side_effect=lambda: self.now).start()
        self.connection = Mock()
        self.connection.request.side_effect = self.github_request
        self.connection.getresponse.side_effect = self.github_response
        patch("troubleshooter_deployment.github_attempt.HTTPSConnection", return_value=self.connection).start()
        self.addCleanup(patch.stopall)

    def run_bootstrap(self, current=ATTEMPT):
        self.current = current
        self.attempt_shas[(current.run_id, current.number)] = current.source_sha
        return bootstrap_recovery_store(scope=self.initial.scope, current=current,
            owner="synthetic-owner", repository="synthetic-repo", github_token="synthetic-token", oci_config=CONFIG)

    def state(self):
        return read_bootstrap(self.initial, STACK_ID, self.stack["freeformTags"], self.stack["definedTags"])

    def github_request(self, method, path, **kwargs):
        self.calls.append(("github", method, path, None))
        parts = path.split("?")[0].split("/")
        self.requested_attempt = int(parts[-3]), int(parts[-1])

    def github_response(self):
        run_id, number = self.requested_attempt
        status = self.statuses.get((run_id, number),
            "in_progress" if (run_id, number) == (self.current.run_id, self.current.number) else "completed")
        data = {"repository": {"id": self.initial.scope.repository_id}, "id": run_id, "run_attempt": number,
                "head_sha": self.attempt_shas[(run_id, number)], "status": status}
        body = json.dumps(data).encode()
        result = Mock(status=200)
        result.read.side_effect = BytesIO(body).read
        result.getheader.side_effect = lambda name: {"Content-Length": str(len(body))}.get(name)
        return result

    def send(self, request, **kwargs):
        path = urlsplit(request.url).path
        self.calls.append(("oci", request.method, path, request))
        if path.endswith("/search"):
            state = self.state()
            item = event()
            item["data"]["additionalDetails"]["bucketName"] = state.request.name
            item["data"]["request"]["headers"] = {
                "opc-client-request-id": [state.request.correlation],
                "x-content-sha256": [creation_digest_v1(state.request)]}
            return page([deepcopy(item) for _ in range(self.audit_count)])
        if path.endswith("/jobs"):
            return response(self.jobs, headers=self.job_headers)
        if "/stacks/" in path:
            if request.method == "GET":
                return response(self.stack, headers={"etag": f'"s{self.stack_version}"'})
            self.assertEqual(request.method, "PUT")
            self.assertEqual(set(json.loads(request.body)), {"freeformTags"})
            state = json.loads(json.loads(request.body)["freeformTags"][PREFIX + "intent"])["state"]
            if self.fault == state + "_before":
                raise OSError("synthetic-private-failure")
            if self.fault == "cas_conflict" or request.headers.get("if-match") != f'"s{self.stack_version}"':
                return response({"code": "IfMatchFailed"}, status=412)
            self.stack["freeformTags"] = json.loads(request.body)["freeformTags"]
            self.stack_version += 1
            if self.fault == state + "_after":
                raise OSError("synthetic-private-failure")
            return response(self.stack)
        if path == "/n":
            return response("synthetic-ns")
        if request.method == "POST":
            self.assertEqual(path, "/n/synthetic-ns/b")
            if self.fault == "bucket_before":
                raise OSError("synthetic-private-failure")
            self.bucket = {**json.loads(request.body), "id": "synthetic-created-bucket",
                           "namespace": "synthetic-ns", "createdBy": CONFIG["user"], "isReadOnly": False}
            if self.fault == "bucket_after":
                raise OSError("synthetic-private-failure")
            return response(self.bucket)
        if "/o/" not in path:
            return response(self.bucket or {"code": "NotAuthorizedOrNotFound"}, status=200 if self.bucket else 404)
        if request.method == "GET":
            return response(body=self.body or b'{}', status=200 if self.body else 404,
                            headers={"etag": f'"o{self.object_version}"'})
        self.assertEqual(request.method, "PUT")
        if self.fault == "object_before":
            raise OSError("synthetic-private-failure")
        if (("if-none-match" in request.headers and self.body is not None)
                or ("if-match" in request.headers and request.headers["if-match"] != f'"o{self.object_version}"')):
            return response({"code": "IfMatchFailed"}, status=412)
        self.body = request.body
        self.object_version += 1
        if self.fault == "object_after":
            raise OSError("synthetic-private-failure")
        return response(body=b"", headers={"etag": f'"o{self.object_version}"'})

    def mutations(self):
        return [call[3] for call in self.calls if call[:2] in (("oci", "POST"), ("oci", "PUT"))
                and not call[2].endswith("/search")]
