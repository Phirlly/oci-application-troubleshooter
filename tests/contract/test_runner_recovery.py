"""Known-journal handoff, not first-anchor discovery or a hosted-runner proof."""
from dataclasses import replace
from io import BytesIO
import json
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from troubleshooter_deployment.codec import decode, encode
from troubleshooter_deployment.github_attempt import GitHubAttempts, GitHubError
from troubleshooter_deployment.journal import Journal, WriteConflict, WriteUncertain
from troubleshooter_deployment.oci_journal import OciJournalStore, StoreError
from troubleshooter_deployment.policy import PolicyError, begin_generation, bootstrap, reserve
from tests.contract.oci_responses import BUCKET, CONFIG, OCI_IDENTITY, response, signing_stub
from tests.deployment.fixtures import ATTEMPT, NEXT_ATTEMPT, GENERATION, OPERATION, SHA


class RunnerRecoveryTests(unittest.TestCase):
    def setUp(self):
        record = begin_generation(bootstrap(OCI_IDENTITY, OCI_IDENTITY, ATTEMPT), GENERATION, SHA)
        self.record, _ = reserve(record, GENERATION, OPERATION, "provision")
        self.body, self.version = encode(self.record), 1
        self.bucket, self.fail_write, self.race = dict(BUCKET), None, None
        self.calls, self.puts, self.replies = [], [], []
        self.github_status = 200
        self.github_data = {"repository": {"id": OCI_IDENTITY.scope.repository_id},
                            "id": ATTEMPT.run_id, "run_attempt": ATTEMPT.number,
                            "head_sha": ATTEMPT.source_sha, "status": "completed"}
        signing_stub().start()
        patch("oci.base_client.OCIHTTPAdapter.send", side_effect=self.send).start()
        self.connection = Mock()
        self.connection.request.side_effect = lambda method, path, **kw: self.calls.append(("github", method, path))
        self.connection.getresponse.side_effect = self.github_response
        patch("troubleshooter_deployment.github_attempt.HTTPSConnection", return_value=self.connection).start()
        self.addCleanup(patch.stopall)

    def reply(self, *args, **kwargs):
        result = response(*args, **kwargs)
        self.replies.append(result)
        return result

    def send(self, request, **kwargs):
        path = urlsplit(request.url).path
        self.calls.append(("oci", request.method, path))
        if request.method == "GET" and "/o/" not in path:
            return self.reply(self.bucket)
        if request.method == "GET":
            return self.reply(body=self.body, headers={"etag": f'"v{self.version}"'})
        self.assertEqual(request.method, "PUT")
        self.puts.append(request)
        self.assertNotIn("if-none-match", request.headers)
        if self.race is not None:
            self.body, self.race = self.race, None
            self.version += 1
        if request.headers.get("if-match") != f'"v{self.version}"':
            return self.reply({"code": "IfMatchFailed"}, status=412)
        if self.fail_write == "before":
            raise OSError("synthetic-private-failure")
        self.body = request.body
        self.version += 1
        if self.fail_write == "after":
            raise OSError("synthetic-private-failure")
        return self.reply(body=b"", headers={"etag": f'"v{self.version}"'})

    def github_response(self):
        body = json.dumps(self.github_data).encode()
        result = Mock(status=self.github_status)
        result.read.side_effect = BytesIO(body).read
        result.getheader.side_effect = lambda name: {"Content-Length": str(len(body))}.get(name)
        return result

    def fresh_runner(self):
        # Identity is a trusted fixture input; no discovery guarantee is implied.
        store = OciJournalStore(CONFIG, OCI_IDENTITY, "synthetic-ns", "synthetic-bucket")
        self.addCleanup(store.close)
        return (Journal(store, OCI_IDENTITY, NEXT_ATTEMPT),
                GitHubAttempts("synthetic-owner", "synthetic-repo", "synthetic-token"))

    def handoff(self, journal, attempts):
        # Compose existing APIs explicitly. There is no production entrypoint yet.
        snapshot = journal.load()
        if snapshot.record.owner != journal.owner:
            snapshot = journal.takeover(snapshot, attempts.observe(snapshot.record))
        return snapshot

    def test_completed_attempt_transfers_only_journal_ownership_and_preserves_operation(self):
        journal, attempts = self.fresh_runner()
        snapshot = self.handoff(journal, attempts)
        self.assertEqual(snapshot.record.owner, NEXT_ATTEMPT)
        self.assertEqual(snapshot.record.operations, self.record.operations)
        self.assertEqual(snapshot.record.operations[0].state, "uncertain")
        self.assertEqual(decode(self.body), snapshot.record)
        self.assertEqual(snapshot.revision, '"v2"')
        self.assertEqual(len(self.puts), 1)
        self.assertEqual(self.puts[0].headers["if-match"], '"v1"')
        self.assertEqual(self.calls[3], ("github", "GET",
            "/repos/synthetic-owner/synthetic-repo/actions/runs/100/attempts/1?exclude_pull_requests=true"))
        before = list(self.calls)
        self.assertEqual(journal.reserve(snapshot, GENERATION, OPERATION, "provision")[1], "reconcile")
        with self.assertRaises(PolicyError):
            journal.reserve(snapshot, GENERATION, "6" * 32, "provision")
        self.assertEqual(self.calls, before)
        self.assertTrue(all(reply.raw.closed for reply in self.replies))

    def test_lost_takeover_reply_reloads_with_fresh_adapters_without_second_put(self):
        journal, attempts = self.fresh_runner()
        self.fail_write = "after"
        with self.assertRaises(WriteUncertain) as error:
            self.handoff(journal, attempts)
        self.assertNotIn("synthetic-private", str(error.exception))
        self.assertEqual(decode(self.body).owner, NEXT_ATTEMPT)
        self.fail_write = None
        self.calls.clear()
        restarted, fresh_attempts = self.fresh_runner()
        snapshot = self.handoff(restarted, fresh_attempts)
        self.assertEqual(snapshot.record.operations, self.record.operations)
        self.assertEqual(restarted.reserve(snapshot, GENERATION, OPERATION, "provision")[1], "reconcile")
        self.assertEqual(len(self.puts), 1)
        self.assertEqual([call[:2] for call in self.calls], [("oci", "GET")] * 3)

    def test_unaccepted_takeover_write_requires_fresh_load_and_new_observation(self):
        journal, attempts = self.fresh_runner()
        self.fail_write = "before"
        with self.assertRaises(WriteUncertain):
            self.handoff(journal, attempts)
        self.assertEqual(decode(self.body), self.record)
        self.fail_write = None
        self.calls.clear()
        snapshot = self.handoff(*self.fresh_runner())
        self.assertEqual(snapshot.record.owner, NEXT_ATTEMPT)
        self.assertEqual(len(self.puts), 2)
        self.assertEqual(self.puts[-1].headers["if-match"], '"v1"')
        self.assertEqual(self.calls[3][:2], ("github", "GET"))

    def test_competing_takeover_conflicts_and_does_not_overwrite_new_owner(self):
        competing = replace(self.record, owner=replace(NEXT_ATTEMPT, run_id=200))
        self.race = encode(competing)
        with self.assertRaises(WriteConflict):
            self.handoff(*self.fresh_runner())
        self.assertEqual(decode(self.body), competing)
        self.assertEqual(len(self.puts), 1)
        # Fresh execution must verify the now-current owner, not reuse old evidence.
        self.calls.clear()
        with self.assertRaises(GitHubError):
            self.handoff(*self.fresh_runner())
        self.assertEqual(self.calls[-1], ("github", "GET",
            "/repos/synthetic-owner/synthetic-repo/actions/runs/200/attempts/2?exclude_pull_requests=true"))
        self.assertEqual(len(self.puts), 1)
        self.assertEqual(decode(self.body), competing)

    def test_nonterminal_denied_or_mismatched_github_evidence_never_writes(self):
        valid = dict(self.github_data)
        variants = [(200, dict(valid, status="in_progress")), (403, valid), (404, valid),
                    (200, dict(valid, run_attempt=2)), (200, dict(valid, head_sha="f" * 40)),
                    (200, dict(valid, repository={"id": 999}))]
        for status, data in variants:
            with self.subTest(status=status, data=data):
                self.github_status, self.github_data = status, data
                with self.assertRaises((GitHubError, PolicyError)):
                    self.handoff(*self.fresh_runner())
                self.assertEqual(self.puts, [])
                self.assertEqual(decode(self.body), self.record)

    def test_replaced_bucket_or_foreign_record_stops_before_github_or_initialization(self):
        for bucket, body in [(dict(BUCKET, id="replacement"), encode(self.record)),
                             (dict(BUCKET), encode(replace(self.record, identity=replace(OCI_IDENTITY, epoch="9" * 32))))]:
            self.bucket, self.body = bucket, body
            self.calls.clear()
            with self.assertRaises(StoreError):
                self.handoff(*self.fresh_runner())
            self.assertEqual(self.puts, [])
            self.assertTrue(all(call[:2] == ("oci", "GET") for call in self.calls))

    def test_unknown_object_read_cannot_trigger_initialization_or_takeover(self):
        with patch("oci.base_client.OCIHTTPAdapter.send", side_effect=[
                response(), response({"code": "NotAuthorizedOrNotFound"}, status=404)]) as send:
            with self.assertRaises(StoreError):
                self.handoff(*self.fresh_runner())
        self.assertEqual([call.args[0].method for call in send.call_args_list], ["GET", "GET"])
        self.connection.request.assert_not_called()
