from dataclasses import replace
import json
import unittest

from troubleshooter_deployment.github_bootstrap import OWNERSHIP_TASK, BootstrapError, BootstrapWriteUncertain
from troubleshooter_deployment.stack_ownership import freeze_stack_ownership, ownership_payload
from tests.contract import test_github_bootstrap as bootstrap_tests
from tests.contract.test_github_bootstrap import BASE, deployment


def owned_deployment(ownership, identifier=12, **changes):
    return deployment(ownership.bootstrap, identifier, task=OWNERSHIP_TASK,
                      payload=ownership_payload(ownership), **changes)


class GitHubStackOwnershipTests(unittest.TestCase):
    connection = bootstrap_tests.GitHubBootstrapTests.connection
    make_response = bootstrap_tests.GitHubBootstrapTests.make_response
    reply = bootstrap_tests.GitHubBootstrapTests.reply
    calls = bootstrap_tests.GitHubBootstrapTests.calls
    link = bootstrap_tests.GitHubBootstrapTests.link

    def setUp(self):
        bootstrap_tests.GitHubBootstrapTests.setUp(self)
        self.ownership = freeze_stack_ownership(self.commitment, "test.ormstack.synthetic")

    def test_ownership_publication_and_independent_readback_use_separate_task(self):
        item = owned_deployment(self.ownership)
        self.reply(item, status=201)
        self.reply(item)
        result = self.client.publish_ownership(self.ownership)
        self.assertEqual(result.commitment, self.ownership)
        post, get = self.calls()
        self.assertEqual(post.args, ("POST", BASE))
        self.assertEqual(get.args, ("GET", BASE + "/12"))
        body = json.loads(post.kwargs["body"])
        self.assertEqual(body["task"], OWNERSHIP_TASK)
        self.assertEqual(body["payload"], ownership_payload(self.ownership))
        self.assertNotIn("test.ormstack.synthetic", post.kwargs["body"].decode())
        self.assertFalse(body["auto_merge"])
        self.assertEqual(body["required_contexts"], [])

    def test_ownership_discovery_retains_identical_duplicates_and_checks_its_own_links(self):
        link = self.link(2).replace("oci-troubleshooter-bootstrap-v1", OWNERSHIP_TASK)
        self.reply([owned_deployment(self.ownership)], headers={"Link": link})
        self.reply([owned_deployment(self.ownership, 13)])
        self.assertEqual([x.commitment for x in self.client.discover_ownership(self.scope)], [self.ownership] * 2)
        self.assertTrue(all(OWNERSHIP_TASK in call.args[1] for call in self.calls()))
        self.reply([owned_deployment(self.ownership)], headers={"Link": self.link(2)})
        with self.assertRaises(BootstrapError):
            self.client.discover_ownership(self.scope)

    def test_conflicting_id_binding_or_wrong_task_cannot_be_recovered(self):
        other = freeze_stack_ownership(self.commitment, "different-stack")
        for rows in [[owned_deployment(self.ownership), owned_deployment(other, 13)],
                     [deployment(self.commitment)],
                     [owned_deployment(replace(self.ownership, bootstrap=replace(self.commitment, scope_sha256="f" * 64)))]]:
            self.reply(rows)
            with self.assertRaises(BootstrapError):
                self.client.discover_ownership(self.scope)

    def test_ownership_post_or_readback_loss_stays_uncertain_and_is_not_retried(self):
        item = owned_deployment(self.ownership)
        self.post_error = OSError("private")
        with self.assertRaises(BootstrapWriteUncertain):
            self.client.publish_ownership(self.ownership)
        self.post_error = None
        self.reply(item, status=201)
        self.reply(status=403)
        with self.assertRaises(BootstrapWriteUncertain):
            self.client.publish_ownership(self.ownership)
        self.reply([item])
        self.assertEqual(self.client.discover_ownership(self.scope)[0].commitment, self.ownership)
        self.assertEqual([call.args[0] for call in self.calls()], ["POST", "POST", "GET", "GET"])

    def test_invalid_ownership_fails_before_transport(self):
        with self.assertRaises(BootstrapError) as error:
            self.client.publish_ownership(replace(self.ownership, schema=True))
        self.assertNotIsInstance(error.exception, BootstrapWriteUncertain)
        self.factory.assert_not_called()
