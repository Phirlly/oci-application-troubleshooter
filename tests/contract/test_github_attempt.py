from dataclasses import replace
from io import BytesIO
import json
import unittest
from unittest.mock import Mock, patch

from troubleshooter_deployment.github_attempt import GitHubAttempts, GitHubError
from troubleshooter_deployment.policy import PolicyError, bootstrap, claim
from troubleshooter_deployment.records import RunObservation
from tests.deployment.fixtures import ATTEMPT, NEXT_ATTEMPT, IDENTITY


class GitHubAttemptTests(unittest.TestCase):
    def setUp(self):
        self.record = bootstrap(IDENTITY, IDENTITY, ATTEMPT)
        self.data = {"repository": {"id": 42}, "id": 100, "run_attempt": 1,
                     "head_sha": ATTEMPT.source_sha, "status": "completed"}
        self.client = GitHubAttempts("synthetic-owner", "synthetic-repo", "synthetic-token")
        self.connection = Mock()
        self.factory = patch("troubleshooter_deployment.github_attempt.HTTPSConnection", return_value=self.connection).start()
        self.addCleanup(patch.stopall)

    def response(self, data=None, status=200, body=None, headers=None):
        content = json.dumps(self.data if data is None else data).encode() if body is None else body
        response = Mock(status=status)
        stream = BytesIO(content)
        response.read.side_effect = stream.read
        values = {"Content-Length": str(len(content)), **(headers or {})}
        response.getheader.side_effect = lambda name, default=None: values.get(name, default)
        self.connection.getresponse.return_value = response
        return response

    def test_exact_response_and_required_headers_support_takeover(self):
        response = self.response()
        observation = self.client.observe(self.record)
        self.assertEqual(observation, RunObservation(42, 100, 1, ATTEMPT.source_sha, "completed"))
        self.assertEqual(claim(self.record, NEXT_ATTEMPT, observation).owner, NEXT_ATTEMPT)
        self.assertNotIn("workflow_sha", observation.__dict__)
        self.factory.assert_called_once_with("api.github.com", timeout=5)
        args, kwargs = self.connection.request.call_args
        self.assertEqual(args, ("GET", "/repos/synthetic-owner/synthetic-repo/actions/runs/100/attempts/1?exclude_pull_requests=true"))
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer synthetic-token")
        self.assertEqual(kwargs["headers"]["User-Agent"], "oci-application-troubleshooter")
        self.assertEqual(kwargs["headers"]["X-GitHub-Api-Version"], "2026-03-10")
        self.assertEqual(kwargs["headers"]["Accept"], "application/vnd.github+json")
        response.close.assert_called_once()
        self.connection.close.assert_called_once()

    def test_wrong_or_missing_identity_and_malformed_status_are_rejected(self):
        variants = [dict(self.data, **{name: value}) for name, value in [
            ("id", 101), ("run_attempt", 2), ("run_attempt", True),
            ("head_sha", "b" * 40), ("repository", {"id": 99}),
            ("status", None), ("status", []), ("repository", []),
        ]]
        missing = dict(self.data)
        del missing["run_attempt"]
        for data in variants + [missing]:
            with self.subTest(data=data):
                self.response(data)
                with self.assertRaises(GitHubError):
                    self.client.observe(self.record)

    def test_nonterminal_evidence_never_claims_and_additive_fields_are_safe(self):
        for status in ["in_progress", "queued", "unknown"]:
            self.response(dict(self.data, status=status, future_field="ignored"))
            observation = self.client.observe(self.record)
            with self.assertRaises(PolicyError):
                claim(self.record, NEXT_ATTEMPT, observation)
        self.response(dict(self.data, conclusion="failure"))
        self.assertEqual(self.client.observe(self.record).status, "completed")

    def test_non200_and_redirect_do_not_retry_or_read_raw_failure_body(self):
        for status in [301, 302, 401, 403, 404, 429, 500]:
            response = self.response(status=status, body=b"synthetic-token private provider response")
            self.connection.request.reset_mock()
            with self.assertRaises(GitHubError) as error:
                self.client.observe(self.record)
            self.assertNotIn("synthetic-token", str(error.exception))
            response.read.assert_not_called()
            self.connection.request.assert_called_once()
            response.close.assert_called_once()

    def test_bounded_strict_json_encoding_and_truncation(self):
        for body, headers in [
            (b"x" * 262145, {}), (b"{}", {"Content-Length": "900000"}),
            (b"{}", {"Content-Length": "3"}), (b"{}", {"Content-Length": "invalid"}),
            (b"{}", {"Content-Encoding": "gzip"}), (b"{", {}),
            (b'{"status":"completed","status":"queued"}', {}),
            (b"NaN", {}), (b"[]", {}), (b"[" * 2000, {}),
        ]:
            response = self.response(body=body, headers=headers)
            with self.assertRaises(GitHubError):
                self.client.observe(self.record)
            response.close.assert_called_once()

    def test_network_and_late_result_are_sanitized_and_closed(self):
        self.connection.request.side_effect = OSError("synthetic-token")
        with self.assertRaises(GitHubError) as error:
            self.client.observe(self.record)
        self.assertNotIn("synthetic-token", str(error.exception))
        self.connection.close.assert_called_once()
        self.connection.request.side_effect = None
        self.response()
        with patch("troubleshooter_deployment.request_budget.monotonic", side_effect=[0, 21]):
            with self.assertRaises(GitHubError):
                self.client.observe(self.record)

    def test_cleanup_failure_closes_connection_and_remains_sanitized(self):
        response = self.response()
        response.close.side_effect = OSError("synthetic-token")
        with self.assertRaises(GitHubError) as error:
            self.client.observe(self.record)
        self.assertNotIn("synthetic-token", str(error.exception))
        self.connection.close.assert_called_once()

    def test_nonfinite_additive_json_is_not_valid_provider_evidence(self):
        self.response(dict(self.data, additional=float("nan")))
        with self.assertRaises(GitHubError):
            self.client.observe(self.record)

    def test_invalid_route_or_token_is_refused(self):
        for owner, repo, token in [("bad/owner", "repo", "x"), ("owner", "../repo", "x"),
                                    ("owner", "repo", "secret\r\n"), ("owner", "repo", "")]:
            with self.assertRaises(GitHubError):
                GitHubAttempts(owner, repo, token)
        self.factory.assert_not_called()

    def test_observation_cannot_use_boolean_as_attempt_or_foreign_scope(self):
        for observation in [RunObservation(99, 100, 1, ATTEMPT.source_sha, "completed"),
                            RunObservation(42, 100, True, ATTEMPT.source_sha, "completed")]:
            with self.assertRaises(PolicyError):
                claim(self.record, NEXT_ATTEMPT, observation)
