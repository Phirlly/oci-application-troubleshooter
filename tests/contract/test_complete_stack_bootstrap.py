from dataclasses import replace
import json
import os
from unittest.mock import patch
from urllib.parse import urlsplit

from troubleshooter_deployment import github_bootstrap_worker
from troubleshooter_deployment.codec import decode
from troubleshooter_deployment.runner_stack_journal import bootstrap_stack_journal, StackJournalReady
from troubleshooter_deployment.runner_bootstrap import BootstrapPending
from troubleshooter_deployment.stack_bootstrap import build_initial_stack
from troubleshooter_deployment.stack_journal_variables import read_journal
from tests.contract import test_runner_bootstrap as initial_tests
from tests.contract.oci_responses import response
from tests.contract.test_actions_context import environment, CONFIG, TARGET
from tests.contract.test_oci_stack_bootstrap import stack_data, STACK_ID, NOW
from tests.deployment.fixtures import ATTEMPT, NEXT_ATTEMPT


class CompleteStackBootstrapTests(initial_tests.BootstrapHttpService):
    """Real worker/coordinators/SDK; only HTTP, signer and checkout are synthetic."""

    def setUp(self):
        super().setUp()
        self.scope = replace(self.scope, tenancy_id=CONFIG["tenancy"], compartment_id=TARGET)
        self.request = build_initial_stack(self.scope, ATTEMPT, CONFIG["user"])
        self.stack, self.version, self.puts, self.fault = None, 0, [], None

    def oci(self, request, **kwargs):
        path = urlsplit(request.url).path
        if request.method == "POST":
            self.stack = stack_data(self.request)
            return super().oci(request, **kwargs)
        if path == "/20180917/stacks/" + STACK_ID:
            self.events.append(("oci", request.method, request.url))
            if request.method == "PUT":
                self.puts.append(request)
                if self.fault == "before":
                    raise OSError("private-journal-reply")
                if self.fault == "conflict" or request.headers["if-match"] != f'"s{self.version}"':
                    return response({"code": "IfMatchFailed"}, status=412)
                self.stack.update(json.loads(request.body))
                self.stack["configSource"] = {"configSourceType": "ZIP_UPLOAD", "workingDirectory": None}
                self.version += 1
                if self.fault == "after":
                    raise OSError("private-journal-reply")
            return response(self.stack, headers={"etag": f'"s{self.version}"'}, status=403 if self.bad_get else 200)
        return super().oci(request, **kwargs)

    def run_journal(self, current=ATTEMPT):
        self.current = current
        self.attempt_shas[(current.run_id, current.number)] = current.source_sha
        return bootstrap_stack_journal(scope=self.scope, current=current, owner="synthetic-owner",
            repository="synthetic-repo", github_token="synthetic-token", oci_config=CONFIG)

    def test_worker_completes_first_use_and_fresh_attempt_without_copied_ids(self):
        self.current = ATTEMPT
        with patch.dict(os.environ, environment(), clear=True), patch(
                "troubleshooter_deployment.github_bootstrap_worker.checked_out_sha", return_value=ATTEMPT.source_sha):
            self.assertEqual(github_bootstrap_worker.main(), 0)
        self.assertEqual(len(self.creates), 1)
        self.assertEqual(len(self.history), 2)
        self.assertEqual(len(self.puts), 1)
        self.assertEqual(decode(read_journal(self.stack["variables"])).owner, ATTEMPT)
        owned_get = max(i for i, (service, method, path) in enumerate(self.events)
                        if service == "github" and method == "GET" and path.endswith("/12"))
        journal_put = next(i for i, (service, method, _) in enumerate(self.events) if service == "oci" and method == "PUT")
        self.assertLess(owned_get, journal_put)
        self.assertIsInstance(self.run_journal(NEXT_ATTEMPT), StackJournalReady)
        self.assertEqual(decode(read_journal(self.stack["variables"])).owner, NEXT_ATTEMPT)
        self.assertEqual(len(self.creates), 1)
        self.assertEqual(len(self.history), 2)
        self.assertTrue(all("objectstorage" not in path and not (method == "POST" and "/jobs" in path)
                            for _, method, path in self.events))

    def test_stop_before_journal_put_recovers_from_confirmed_ownership_after_expiry(self):
        original = self.oci
        def stop(request, **kwargs):
            if request.method == "PUT":
                raise KeyboardInterrupt()
            return original(request, **kwargs)
        with patch("oci.base_client.OCIHTTPAdapter.send", side_effect=stop):
            with self.assertRaises(KeyboardInterrupt):
                self.run_journal()
        self.assertEqual(len(self.history), 2)
        self.assertIsNone(read_journal(self.stack["variables"]))
        self.clock.return_value = NOW + 48 * 3600
        self.assertIsInstance(self.run_journal(NEXT_ATTEMPT), StackJournalReady)
        self.assertEqual(len(self.creates), 1)

    def test_lost_publish_create_ownership_and_journal_replies_use_retained_evidence(self):
        # Each subcase starts a fresh synthetic service, never real resources.
        for failure in ("lose_publish", "lose_create", "lose_ownership", "before", "after"):
            with self.subTest(failure=failure):
                self.history, self.creates, self.puts, self.events = [], [], [], []
                self.existing_stack, self.stack, self.version = False, None, 0
                if failure in {"before", "after"}:
                    self.fault = failure
                else:
                    setattr(self, failure, True)
                self.assertIsInstance(self.run_journal(), BootstrapPending)
                self.fault = None
                self.lose_publish = self.lose_create = self.lose_ownership = False
                self.assertIsInstance(self.run_journal(NEXT_ATTEMPT), StackJournalReady)
                self.assertEqual(len(self.creates), 2 if failure == "lose_create" else 1)
                self.assertEqual(len(set(self.creates)), 1)
                self.assertEqual(len(self.history), 2)
                self.assertEqual(decode(read_journal(self.stack["variables"])).owner, NEXT_ATTEMPT)

    def test_lost_takeover_ack_is_not_rewritten_when_same_attempt_resumes(self):
        self.assertIsInstance(self.run_journal(), StackJournalReady)
        self.fault = "after"
        self.assertIsInstance(self.run_journal(NEXT_ATTEMPT), BootstrapPending)
        previous = len(self.puts)
        self.fault = None
        self.assertIsInstance(self.run_journal(NEXT_ATTEMPT), StackJournalReady)
        self.assertEqual(len(self.puts), previous)
        self.assertEqual(len(self.creates), 1)

    def test_denial_conflict_or_unfinished_attempt_never_replaces_stack(self):
        self.assertIsInstance(self.run_journal(), StackJournalReady)
        self.gh_denied = True
        self.assertIsInstance(self.run_journal(NEXT_ATTEMPT), BootstrapPending)
        self.gh_denied = False
        self.original_status = "in_progress"
        self.assertIsInstance(self.run_journal(NEXT_ATTEMPT), BootstrapPending)
        self.original_status = "completed"
        self.fault = "conflict"
        self.assertIsInstance(self.run_journal(NEXT_ATTEMPT), BootstrapPending)
        self.assertEqual(len(self.creates), 1)
        self.assertEqual(decode(read_journal(self.stack["variables"])).owner, ATTEMPT)

    def test_new_release_reuses_empty_journal_without_create_or_github_publication(self):
        original = self.run_journal()
        before = json.loads(json.dumps(self.stack))
        later = replace(ATTEMPT, run_id=200, source_sha="b" * 40, workflow_sha="b" * 40)
        self.events.clear()
        result = self.run_journal(later)
        self.assertIsInstance(result, StackJournalReady)
        self.assertEqual(result.snapshot.record, replace(original.snapshot.record, owner=later))
        self.assertEqual(result.anchor.request, original.anchor.request)
        self.assertEqual(len(self.creates), 1)
        self.assertEqual(len(self.history), 2)
        self.assertEqual(len(self.puts), 2)
        self.assertEqual(set(json.loads(self.puts[-1].body)), {"variables"})
        self.assertTrue(all(method == "GET" for service, method, _ in self.events if service == "github"))
        self.assertEqual({k: v for k, v in self.stack.items() if k != "variables"},
                         {k: v for k, v in before.items() if k != "variables"})

    def test_changed_release_cannot_finish_unconfirmed_creation_or_missing_journal(self):
        later = replace(ATTEMPT, run_id=200, source_sha="b" * 40, workflow_sha="b" * 40)
        self.lose_create = True
        self.assertIsInstance(self.run_journal(), BootstrapPending)
        self.lose_create = False
        self.assertIsInstance(self.run_journal(later), BootstrapPending)
        self.assertEqual(len(self.creates), 1)
        self.assertEqual(self.puts, [])
        # Same-release reconciliation confirms ownership, but initialization loses its request.
        self.fault = "before"
        self.assertIsInstance(self.run_journal(NEXT_ATTEMPT), BootstrapPending)
        self.fault = None
        count = len(self.puts)
        self.assertIsInstance(self.run_journal(later), BootstrapPending)
        self.assertEqual(len(self.puts), count)
        self.assertIsNone(read_journal(self.stack["variables"]))

    def test_unconfirmed_expired_create_stays_pending_without_new_token(self):
        self.lose_create = True
        self.assertIsInstance(self.run_journal(), BootstrapPending)
        self.lose_create = False
        self.clock.return_value = NOW + 24 * 3600
        result = self.run_journal(NEXT_ATTEMPT)
        self.assertEqual(result, BootstrapPending("initial_retry_window_closed"))
        self.assertEqual(len(self.creates), 1)
        self.assertEqual(self.puts, [])
