from dataclasses import replace
from io import BytesIO
import json
import unittest
from unittest.mock import Mock, patch

from troubleshooter_deployment.bootstrap_commitment import commitment_payload, freeze_stack_request
from troubleshooter_deployment.github_bootstrap import (
    BOOTSTRAP_TASK, BootstrapError, BootstrapWriteUncertain, GitHubBootstrap,
)
from tests.deployment.fixtures import ATTEMPT, IDENTITY

BASE = "/repos/synthetic-owner/synthetic-repo/deployments"


def deployment(commitment, identifier=11, **changes):
    return {"id": identifier, "sha": commitment.attempt.source_sha, "ref": commitment.attempt.source_sha,
            "task": BOOTSTRAP_TASK, "environment": commitment.environment,
            "payload": commitment_payload(commitment), "created_at": "2026-10-10T01:00:00Z",
            "repository_url": "https://api.github.com/repos/synthetic-owner/synthetic-repo", **changes}


class GitHubBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.scope = IDENTITY.scope
        self.commitment = freeze_stack_request(self.scope, ATTEMPT, b'{"synthetic":"private"}')
        self.connections, self.responses, self.queue = [], [], []
        self.repository_data, self.post_error = {"id": 42}, None
        self.factory = patch("troubleshooter_deployment.github_bootstrap.HTTPSConnection",
                             side_effect=self.connection).start()
        self.addCleanup(patch.stopall)
        self.client = GitHubBootstrap("synthetic-owner", "synthetic-repo", "synthetic-token")

    def connection(self, *args, **kwargs):
        connection = Mock()
        def request(method, path, **kwargs):
            if method == "POST" and self.post_error is not None:
                raise self.post_error
        connection.request.side_effect = request
        connection.getresponse.side_effect = lambda: (
            self.make_response(self.repository_data)
            if connection.request.call_args.args[1] == BASE.rsplit("/", 1)[0] else self.queue.pop(0))
        self.connections.append(connection)
        return connection

    def make_response(self, data=None, *, status=200, body=None, headers=None):
        content = json.dumps(data).encode() if body is None else body
        result = Mock(status=status)
        result.read.side_effect = BytesIO(content).read
        values = {"Content-Length": str(len(content)), **(headers or {})}
        result.getheader.side_effect = lambda key, default=None: values.get(key, default)
        self.responses.append(result)
        return result

    def reply(self, *args, **kwargs):
        result = self.make_response(*args, **kwargs)
        self.queue.append(result)
        return result

    def calls(self):
        return [connection.request.call_args for connection in self.connections
                if connection.request.call_args.args[1] != BASE.rsplit("/", 1)[0]]

    def test_publish_posts_only_public_payload_then_independently_gets_same_record(self):
        item = deployment(self.commitment)
        self.reply(item, status=201)
        self.reply(item)
        saved = self.client.publish(self.commitment)
        self.assertEqual((saved.deployment_id, saved.created_at, saved.commitment),
                         (11, item["created_at"], self.commitment))
        first, second = self.calls()
        self.assertEqual(first.args, ("POST", BASE))
        self.assertEqual(second.args, ("GET", BASE + "/11"))
        request = json.loads(first.kwargs["body"])
        self.assertEqual(request, {"ref": ATTEMPT.source_sha, "task": BOOTSTRAP_TASK,
            "environment": "demo", "auto_merge": False, "required_contexts": [],
            "transient_environment": False, "production_environment": False,
            "payload": commitment_payload(self.commitment)})
        self.assertNotIn("synthetic-private", str(first))
        self.assertNotIn(self.scope.tenancy_id, str(first))
        headers = first.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "Bearer synthetic-token")
        self.assertEqual(headers["User-Agent"], "oci-application-troubleshooter")
        self.assertEqual(headers["X-GitHub-Api-Version"], "2026-03-10")
        self.assertEqual(headers["Accept-Encoding"], "identity")
        self.assertEqual(headers["Content-Type"], "application/json")
        for call in self.factory.call_args_list:
            self.assertEqual(call.args, ("api.github.com",))
            self.assertEqual(call.kwargs, {"timeout": 5})
        for resource in self.connections + self.responses:
            resource.close.assert_called_once()

    def test_lost_post_reply_is_uncertain_and_fresh_client_finds_original_without_post(self):
        self.post_error = OSError("synthetic-token")
        with self.assertRaises(BootstrapWriteUncertain) as error:
            self.client.publish(self.commitment)
        self.assertNotIn("synthetic-token", str(error.exception))
        self.post_error = None
        self.reply([deployment(self.commitment)])
        fresh = GitHubBootstrap("synthetic-owner", "synthetic-repo", "replacement-token")
        found = fresh.discover(self.scope)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].commitment, self.commitment)
        self.assertEqual([call.args[0] for call in self.calls()], ["POST", "GET"])

    def test_failed_post_and_mismatched_readback_never_return_saved_checkpoint(self):
        for status in [202, 301, 401, 403, 409, 422, 429, 500]:
            self.queue.clear()
            response = self.reply(status=status, body=b"synthetic-token")
            before = len(self.connections)
            with self.assertRaises(BootstrapWriteUncertain):
                self.client.publish(self.commitment)
            self.assertEqual(len(self.connections) - before, 2)
            response.read.assert_not_called()
        for changed in [dict(deployment(self.commitment), id=12),
                        deployment(self.commitment, created_at="2026-10-10T02:00:00Z"),
                        deployment(replace(self.commitment, request_sha256="f" * 64))]:
            self.reply(deployment(self.commitment), status=201)
            self.reply(changed)
            with self.assertRaises(BootstrapWriteUncertain):
                self.client.publish(self.commitment)

    def test_discovery_preserves_history_and_duplicate_intents_without_selecting_winner(self):
        old = freeze_stack_request(self.scope, replace(ATTEMPT, run_id=90, source_sha="b" * 40), b"old")
        self.reply([deployment(self.commitment, 12), deployment(old, 9), deployment(self.commitment, 11)])
        found = self.client.discover(self.scope)
        self.assertEqual([item.deployment_id for item in found], [12, 9, 11])
        self.assertEqual(found[1].commitment, old)
        self.assertEqual(self.calls()[0].args, ("GET", BASE +
            "?task=oci-troubleshooter-bootstrap-v1&per_page=10&page=1"))

    def test_empty_discovery_never_publishes(self):
        self.reply([])
        self.assertEqual(self.client.discover(self.scope), ())
        self.assertEqual([call.args[0] for call in self.calls()], ["GET"])

    def test_conflicting_run_duplicate_ids_or_changed_target_fail_closed(self):
        variants = [
            [deployment(self.commitment), deployment(self.commitment)],
            [deployment(self.commitment), deployment(replace(self.commitment, request_sha256="f" * 64), 12)],
            [deployment(freeze_stack_request(replace(self.scope, region="us-phoenix-1"), ATTEMPT, b"old"))],
        ]
        for rows in variants:
            self.reply(rows)
            with self.assertRaises(BootstrapError):
                self.client.discover(self.scope)

    def test_bad_envelopes_and_payloads_cannot_be_discovered(self):
        changes = [{"id": True}, {"id": 0}, {"id": 2**63}, {"sha": "b" * 40}, {"ref": "dev"},
                   {"task": "deploy"}, {"environment": "other"}, {"created_at": "bad"},
                   {"created_at": "2026-02-30T00:00:00Z"}, {"created_at": None},
                   {"repository_url": "https://api.github.com/repos/foreign/repo"},
                   {"payload": {}}, {"payload": {**commitment_payload(self.commitment), "repository_id": 999}},
                   {"payload": {**commitment_payload(self.commitment), "environment": "other"}}]
        for change in changes:
            with self.subTest(change=change):
                self.reply([deployment(self.commitment, **change)])
                with self.assertRaises(BootstrapError):
                    self.client.discover(self.scope)
        for data in [{}, [None], [deployment(self.commitment)] * 11]:
            self.reply(data)
            with self.assertRaises(BootstrapError):
                self.client.discover(self.scope)

    def link(self, page, relation="next", route=BASE):
        return (f'<https://api.github.com{route}?task={BOOTSTRAP_TASK}'
                f'&per_page=10&page={page}>; rel="{relation}"')

    def test_bounded_pagination_including_empty_intermediate_page(self):
        self.reply([], headers={"Link": self.link(2) + ", " + self.link(2, "last")})
        self.reply([deployment(self.commitment)], headers={"Link": self.link(1, "prev")})
        self.assertEqual(len(self.client.discover(self.scope)), 1)
        self.assertTrue(self.calls()[1].args[1].endswith("page=2"))

    def test_malformed_foreign_looping_or_overflow_pagination_discards_partial_results(self):
        for link in ["", "invalid", self.link(1), self.link(3), self.link(2, route="/foreign"),
                     self.link(2).replace("api.github.com", "elsewhere.invalid"),
                     self.link(2).replace("per_page=10", "environment=other&per_page=10"),
                     self.link(2).replace("page=2", "page=2&page=3"),
                     self.link(2) + ", " + self.link(2), self.link(2, "last"), "x" * 4097]:
            self.reply([deployment(self.commitment)], headers={"Link": link})
            with self.assertRaises(BootstrapError):
                self.client.discover(self.scope)
        before = len(self.connections)
        for page in range(1, 6):
            self.reply([], headers={"Link": self.link(page + 1)})
        with self.assertRaises(BootstrapError):
            self.client.discover(self.scope)
        self.assertEqual(len(self.connections) - before, 6)
        self.reply([deployment(self.commitment)], headers={"Link": self.link(2)})
        self.reply(status=403)
        with self.assertRaises(BootstrapError):
            self.client.discover(self.scope)

    def test_transport_rejects_redirect_encoding_truncation_and_malformed_json(self):
        for status, body, headers in [
            (302, b"private", {}), (404, b"private", {}), (200, b"[]", {"Content-Encoding": "gzip"}),
            (200, b"[]", {"Content-Length": "3"}), (200, b"[]", {"Content-Length": "bad"}),
            (200, b"[]", {"Content-Length": "999999999"}), (200, b"x" * 262145, {"Content-Length": None}),
            (200, b"{", {}), (200, b"NaN", {}), (200, b'{"a":1,"a":2}', {}),
        ]:
            response = self.reply(status=status, body=body, headers=headers)
            with self.assertRaises(BootstrapError):
                self.client.discover(self.scope)
            response.close.assert_called_once()
        self.reply([], headers={"Content-Length": None})
        self.assertEqual(self.client.discover(self.scope), ())

    def test_invalid_route_scope_or_commitment_fails_without_request(self):
        for owner, repo, token in [("bad/owner", "repo", "token"), ("owner", "..", "token"),
                                   ("owner", "repo", "token\n"), ("owner", "repo", "")]:
            with self.assertRaises(BootstrapError):
                GitHubBootstrap(owner, repo, token)
        with self.assertRaises(BootstrapError):
            self.client.discover(replace(self.scope, repository_id=True))
        with self.assertRaises(BootstrapError) as error:
            self.client.publish(replace(self.commitment, schema=True))
        self.assertNotIsInstance(error.exception, BootstrapWriteUncertain)
        self.factory.assert_not_called()

    def test_deadlines_and_cleanup_failures_are_sanitized(self):
        self.reply([])
        with patch("troubleshooter_deployment.request_budget.monotonic", side_effect=[0, 21]):
            with self.assertRaises(BootstrapError):
                self.client.discover(self.scope)
        for _ in range(2):
            self.reply(deployment(self.commitment), status=201 if _ == 0 else 200)
        with patch("troubleshooter_deployment.request_budget.monotonic", side_effect=[0, 21]):
            with self.assertRaises(BootstrapWriteUncertain):
                self.client.publish(self.commitment)
        response = self.reply([])
        response.close.side_effect = OSError("synthetic-token")
        with self.assertRaises(BootstrapError) as error:
            self.client.discover(self.scope)
        self.assertNotIn("synthetic-token", str(error.exception))
        self.connections[-1].close.assert_called_once()

    def test_repository_identity_mismatch_stops_before_publish_or_discovery(self):
        for data in [{"id": 999}, {"id": True}, {}, []]:
            self.repository_data = data
            for operation in [lambda: self.client.publish(self.commitment),
                              lambda: self.client.discover(self.scope)]:
                with self.assertRaises(BootstrapError) as error:
                    operation()
                self.assertNotIsInstance(error.exception, BootstrapWriteUncertain)
        self.assertEqual(self.calls(), [])

    def test_numeric_pagination_alias_is_verified_and_named_path_is_reconstructed(self):
        self.reply([], headers={"Link": self.link(2, route="/repositories/42/deployments")})
        self.reply([deployment(self.commitment)])
        self.assertEqual(len(self.client.discover(self.scope)), 1)
        self.assertTrue(self.calls()[-1].args[1].startswith(BASE + "?"))
        self.reply([], headers={"Link": self.link(2, route="/repositories/999/deployments")})
        with self.assertRaises(BootstrapError):
            self.client.discover(self.scope)

    def test_valid_other_environment_is_omitted_but_moved_record_is_not_hidden(self):
        other = freeze_stack_request(replace(self.scope, environment="other"), ATTEMPT, b"other")
        self.reply([deployment(other), deployment(self.commitment, 12)])
        self.assertEqual([row.deployment_id for row in self.client.discover(self.scope)], [12])
        self.reply([deployment(self.commitment, environment="moved")])
        with self.assertRaises(BootstrapError):
            self.client.discover(self.scope)
        self.assertNotIn("environment=", self.calls()[-1].args[1])
