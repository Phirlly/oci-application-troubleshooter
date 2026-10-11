"""Synthetic RM/GitHub HTTP only; no cloud ownership or consistency proof."""
import json
from unittest.mock import patch
from urllib.parse import urlsplit

from troubleshooter_deployment.runner_stack_journal import bootstrap_stack_journal
from tests.contract.oci_responses import CONFIG, response
from tests.contract.recovery_store_service import RecoveryStoreService
from tests.deployment.fixtures import ATTEMPT


class StackJournalService(RecoveryStoreService):
    def setUp(self):
        super().setUp()
        self.initial_call = patch("troubleshooter_deployment.runner_stack_journal.bootstrap_initial_stack",
                                  side_effect=lambda **kwargs: self.anchor).start()

    def run_bootstrap(self, current=ATTEMPT):
        self.current = current
        self.attempt_shas[(current.run_id, current.number)] = current.source_sha
        return bootstrap_stack_journal(scope=self.initial.scope, current=current,
            owner="synthetic-owner", repository="synthetic-repo", github_token="synthetic-token", oci_config=CONFIG)

    def send(self, request, **kwargs):
        path = urlsplit(request.url).path
        if request.method != "PUT":
            return super().send(request, **kwargs)
        self.calls.append(("oci", request.method, path, request))
        self.assertIn("/stacks/", path)
        if self.fault == "before":
            raise OSError("private-failure")
        if self.fault == "conflict" or request.headers.get("if-match") != f'"s{self.stack_version}"':
            return response({"code": "IfMatchFailed"}, status=412)
        body = json.loads(request.body)
        self.assertIn("variables", body)
        self.stack.update(body)
        if "configSource" in body:
            self.stack["configSource"] = {"configSourceType": "ZIP_UPLOAD", "workingDirectory": None}
        self.stack_version += 1
        if self.fault == "after":
            raise OSError("private-failure")
        return response(self.stack, headers={"etag": f'"s{self.stack_version}"'})
