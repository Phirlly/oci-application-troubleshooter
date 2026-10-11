from dataclasses import replace
from io import BytesIO
import json
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from troubleshooter_deployment.bootstrap_commitment import freeze_stack_request
from troubleshooter_deployment.runner_bootstrap import BootstrapPending, bootstrap_initial_stack
from troubleshooter_deployment.stack_bootstrap import build_initial_stack
from tests.contract.oci_responses import CONFIG, OCI_IDENTITY, SyntheticSigner, response
from tests.contract.test_github_bootstrap import BASE, deployment
from tests.contract.test_oci_stack_bootstrap import CREATED, NOW, STACK_ID, stack_data
from tests.deployment.fixtures import ATTEMPT, NEXT_ATTEMPT


class RunnerBootstrapTests(unittest.TestCase):
    """Actual coordinator/adapters/SDK; only the two HTTP boundaries are synthetic."""

    def setUp(self):
        self.history, self.events, self.creates = [], [], []
        self.current_status, self.original_status = "in_progress", "completed"
        self.lose_publish, self.lose_create, self.conflict = False, False, False
        self.gh_denied, self.bad_get = False, False
        self.scope = OCI_IDENTITY.scope
        self.request = build_initial_stack(self.scope, ATTEMPT, CONFIG["user"])
        self.clock = patch("troubleshooter_deployment.stack_bootstrap.time", return_value=NOW).start()
        patch("troubleshooter_deployment.github_bootstrap.HTTPSConnection", side_effect=self.github).start()
        patch("troubleshooter_deployment.github_attempt.HTTPSConnection", side_effect=self.github).start()
        patch("oci.resource_manager.resource_manager_client.Signer", return_value=SyntheticSigner()).start()
        patch("oci.base_client.OCIHTTPAdapter.send", side_effect=self.oci).start()
        self.addCleanup(patch.stopall)

    def github(self, *args, **kwargs):
        connection = Mock()
        def send(method, path, body=None, **kwargs):
            self.events.append(("github", method, path))
            status = 200
            if "/actions/runs/" in path:
                parts = path.split("/")
                run_id, number = int(parts[-3]), int(parts[-1].split("?")[0])
                active = (run_id, number) == (self.current.run_id, self.current.number)
                data = {"repository": {"id": 42}, "id": run_id, "run_attempt": number,
                        "head_sha": ATTEMPT.source_sha,
                        "status": self.current_status if active else self.original_status}
            elif path == BASE.rsplit("/", 1)[0]:
                data = {"id": 42}
            elif method == "POST":
                public = json.loads(body)
                self.assertNotIn(CONFIG["user"], body.decode())
                self.assertNotIn(CONFIG["tenancy"], body.decode())
                self.assertNotIn(CONFIG["key_content"], body.decode())
                data = {**public, "id": 11, "sha": public["ref"], "created_at": CREATED,
                        "repository_url": "https://api.github.com/repos/synthetic-owner/synthetic-repo"}
                self.history.append(data)
                if self.lose_publish:
                    raise OSError("private-gh-token")
                status = 201
            elif "?task=" in path:
                data = self.history
            else:
                self.assertEqual(path, BASE + "/11")
                data = self.history[-1]
            content = json.dumps(data).encode()
            result = Mock(status=403 if self.gh_denied else status)
            result.read.side_effect = BytesIO(content).read
            result.getheader.side_effect = lambda name, default=None: str(len(content)) if name == "Content-Length" else default
            connection.getresponse.return_value = result
        connection.request.side_effect = send
        return connection

    def oci(self, request, **kwargs):
        self.events.append(("oci", request.method, request.url))
        path = urlsplit(request.url).path
        if request.method == "POST":
            self.assertEqual(path, "/20180917/stacks")
            self.creates.append((request.body, request.headers["opc-retry-token"]))
            if self.lose_create:
                raise OSError("private-create-reply")
            return response(stack_data(self.request))
        self.assertEqual(request.method, "GET")
        if path == "/20180917/stacks":
            return response([stack_data(self.request)] if self.conflict else [])
        if path == "/20180917/stacks/" + STACK_ID:
            return response(stack_data(self.request), status=403 if self.bad_get else 200)
        self.assertEqual(path, "/20180917/jobs")
        return response([])

    def run_controller(self, current=ATTEMPT, config=CONFIG, scope=None):
        self.current = current
        return bootstrap_initial_stack(scope=self.scope if scope is None else scope, current=current,
            owner="synthetic-owner", repository="synthetic-repo", github_token="synthetic-token", oci_config=config)

    def saved(self, *, attempt=ATTEMPT, created_at=CREATED, identifier=11):
        request = build_initial_stack(self.scope, attempt, CONFIG["user"])
        return deployment(freeze_stack_request(self.scope, attempt, request.body), identifier, created_at=created_at)

    def assert_pending(self, result, reason=None):
        self.assertIsInstance(result, BootstrapPending)
        if reason:
            self.assertEqual(result.reason, reason)
        for private in [CONFIG["user"], CONFIG["tenancy"], "private-create-reply", "private-gh-token"]:
            self.assertNotIn(private, repr(result))

    def test_new_enrollment_publishes_and_reads_before_one_exact_create(self):
        result = self.run_controller()
        self.assertEqual(result.stack_id, STACK_ID)
        self.assertEqual(self.creates, [(self.request.body, self.request.token)])
        methods = [(service, method) for service, method, _ in self.events]
        self.assertEqual(methods, [("github", "GET")] * 3 + [("oci", "GET"), ("github", "GET"),
            ("github", "POST"), ("github", "GET"), ("oci", "POST"), ("oci", "GET"), ("oci", "GET")])

    def test_existing_same_attempt_never_redispatches(self):
        self.history = [self.saved()]
        self.assert_pending(self.run_controller(), "original_attempt_not_completed")
        self.assertEqual(self.creates, [])
        self.assertTrue(all(service == "github" for service, _, _ in self.events))

    def test_checkpoint_before_oci_is_recovered_by_fresh_attempt(self):
        self.history = [self.saved()]
        result = self.run_controller(NEXT_ATTEMPT)
        self.assertEqual(result.stack_id, STACK_ID)
        self.assertEqual(self.creates, [(self.request.body, self.request.token)])
        self.assertFalse(any(service == "github" and method == "POST" for service, method, _ in self.events))

    def test_lost_github_reply_suppresses_oci_then_fresh_attempt_recovers(self):
        self.lose_publish = True
        self.assert_pending(self.run_controller())
        self.assertEqual(self.creates, [])
        self.lose_publish = False
        self.assertEqual(self.run_controller(NEXT_ATTEMPT).stack_id, STACK_ID)
        self.assertEqual(self.creates, [(self.request.body, self.request.token)])
        self.assertEqual(len(self.history), 1)

    def test_lost_oci_reply_reuses_original_bytes_token_after_key_rotation(self):
        self.lose_create = True
        self.assert_pending(self.run_controller())
        self.lose_create = False
        rotated = {**CONFIG, "fingerprint": ":".join(["11"] * 16), "key_content": "replacement-synthetic-key"}
        self.assertEqual(self.run_controller(NEXT_ATTEMPT, rotated).stack_id, STACK_ID)
        self.assertEqual(self.creates, [(self.request.body, self.request.token)] * 2)
        self.assertEqual(len(self.history), 1)

    def test_denied_nonterminal_or_conflicting_history_has_no_mutation(self):
        self.history = [self.saved()]
        for status in ["in_progress", "queued", "unknown"]:
            self.original_status = status
            self.assert_pending(self.run_controller(NEXT_ATTEMPT), "original_attempt_not_completed")
        self.original_status = "completed"
        self.history.append(self.saved(attempt=replace(ATTEMPT, run_id=90), identifier=12))
        self.assert_pending(self.run_controller(NEXT_ATTEMPT), "competing_bootstrap_commitments")
        self.gh_denied = True
        self.assert_pending(self.run_controller(NEXT_ATTEMPT))
        self.assertEqual(self.creates, [])
        self.assertTrue(all(service == "github" and method == "GET" for service, method, _ in self.events))

    def test_changed_target_principal_source_workflow_or_body_cannot_replace_commitment(self):
        self.history = [self.saved()]
        for kwargs in [{"config": {**CONFIG, "user": "other-user"}},
                       {"scope": replace(self.scope, compartment_id="other")},
                       {"current": replace(NEXT_ATTEMPT, workflow_sha="b" * 40)},
                       {"current": replace(NEXT_ATTEMPT, source_sha="b" * 40)}]:
            self.assert_pending(self.run_controller(**{"current": NEXT_ATTEMPT, **kwargs}))
        self.history[0]["payload"]["request_sha256"] = "f" * 64
        self.assert_pending(self.run_controller(NEXT_ATTEMPT), "initial_request_mismatch")
        self.assertEqual(self.creates, [])

    def test_duplicate_checkpoints_preserve_earliest_deadline_and_original_request(self):
        self.history = [self.saved(identifier=12, created_at="2026-10-10T00:00:00Z"), self.saved()]
        self.assertEqual(self.run_controller(NEXT_ATTEMPT).stack_id, STACK_ID)
        self.creates.clear()
        self.history[0]["created_at"] = "2026-10-09T02:00:00Z"
        self.assert_pending(self.run_controller(NEXT_ATTEMPT), "initial_retry_window_closed")
        self.assertEqual(self.creates, [])
        self.history = [self.saved(), self.saved(identifier=12, created_at="2026-10-10T01:00:01Z")]
        self.assert_pending(self.run_controller(NEXT_ATTEMPT), "initial_retry_window_closed")
        self.assertEqual(self.creates, [])

    def test_another_completed_run_can_be_recovered_without_a_new_commitment(self):
        self.history = [self.saved()]
        self.assertEqual(self.run_controller(replace(ATTEMPT, run_id=101)).stack_id, STACK_ID)
        self.assertEqual(self.creates, [(self.request.body, self.request.token)])
        self.assertEqual(len(self.history), 1)

    def test_future_expired_or_expiring_during_dispatch_never_creates(self):
        for created_at in ["2026-10-10T01:00:01Z", "2026-10-09T02:00:00Z"]:
            self.history = [self.saved(created_at=created_at)]
            self.assert_pending(self.run_controller(NEXT_ATTEMPT), "initial_retry_window_closed")
        self.history = [self.saved()]
        self.clock.side_effect = [NOW, NOW + 23 * 3600]
        self.assert_pending(self.run_controller(NEXT_ATTEMPT))
        self.assertEqual(self.creates, [])

    def test_conflict_current_status_invalid_input_and_failed_readback_stay_pending(self):
        self.conflict = True
        self.assert_pending(self.run_controller())
        self.assertEqual(self.history, [])
        self.conflict = False
        self.current_status = "completed"
        self.assert_pending(self.run_controller(), "current_attempt_not_running")
        self.current_status = "in_progress"
        self.assert_pending(self.run_controller(config={}), "initial_request_mismatch")
        self.bad_get = True
        self.assert_pending(self.run_controller())
        self.assertEqual(len(self.history), 1)
        self.assertEqual(len(self.creates), 1)
