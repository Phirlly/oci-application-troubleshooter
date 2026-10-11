from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from unittest.mock import patch
from urllib.parse import urlsplit

from tests.contract.oci_responses import CONFIG, response
from tests.contract.stack_journal_service import StackJournalService
from tests.contract.test_oci_stack_bootstrap import STACK_ID, stack_data
from tests.live.proof_state import ProofStore, ProofStateError
from tests.live.stack_journal_state import new_state, validate_state, initial_request
from tests.live.stack_journal_services import StackJournalProof


class StackJournalProofTests(StackJournalService):
    def setUp(self):
        super().setUp()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.scope = replace(self.initial.scope, repository_id=1, environment="stack-journal-proof-synthetic")
        self.state = new_state(self.scope, CONFIG["user"])
        self.initial = initial_request(self.state)
        self.stack = stack_data(self.initial)
        self.path = Path(self.temporary.name).resolve() / "fixture"
        self.local = ProofStore(self.path, validator=validate_state).__enter__()
        self.addCleanup(self.local.__exit__, None, None, None)
        self.local.write(self.state)
        self.transport.side_effect = self.cloud
        self.deleted = False

    def cloud(self, request, **kwargs):
        path = urlsplit(request.url).path
        if path.endswith("/stacks"):
            self.calls.append(("oci", request.method, path, request))
            if request.method == "GET":
                return response([])
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.body, self.initial.body)
            return response(self.stack, headers={"etag": '"s1"'})
        if request.method == "DELETE":
            self.calls.append(("oci", "DELETE", path, request))
            self.assertEqual(request.headers["if-match"], f'"s{self.stack_version}"')
            self.deleted = True
            return response(body=b"", status=204)
        return self.send(request, **kwargs)

    def act(self, action):
        with StackJournalProof(CONFIG, self.local, self.local.read()) as proof:
            return getattr(proof, action)()

    def test_pre_put_stop_fresh_resume_observe_and_positive_cleanup(self):
        self.assertTrue(self.act("run_interrupted"))
        self.assertEqual(self.local.read()["phase"], "anchor_ready")
        self.assertEqual([r.method for r in self.mutations()], ["POST"])
        self.assertEqual(len(self.calls), 6)
        self.assertTrue(self.act("run"))
        self.assertEqual(len(self.calls), 14)
        state = self.local.read()
        self.assertEqual((state["phase"], state["cas"]), ("ready", "confirmed"))
        before = (self.path / "state.json").read_bytes()
        self.calls.clear()
        self.assertTrue(self.act("observe"))
        self.assertEqual((self.path / "state.json").read_bytes(), before)
        self.assertEqual(self.mutations(), [])
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.act("cleanup"))
        self.assertEqual(len(self.calls), 9)
        self.assertTrue(self.deleted)
        self.assertEqual(self.local.read()["phase"], "complete")

    def test_uncertain_initialization_does_not_repeat_create_or_erase_state(self):
        self.act("run_interrupted")
        self.fault = "after"
        with self.assertRaises(Exception):
            self.act("run")
        self.assertEqual(self.local.read()["phase"], "journal_uncertain")
        self.fault = None
        before = len(self.calls)
        self.assertFalse(self.act("run_interrupted"))
        self.assertFalse(self.act("run"))
        self.assertEqual(len(self.calls), before)

    def test_changed_journal_or_nonempty_jobs_never_delete(self):
        self.act("run_interrupted")
        self.act("run")
        self.jobs = [{"id": "unexpected-job"}]
        with self.assertRaises(Exception):
            self.act("cleanup")
        self.assertFalse(self.deleted)
        self.assertEqual(self.local.read()["phase"], "purge_ready")

    def test_closed_state_rejects_invalid_dependents_and_old_format(self):
        for changes in ({"schema": True}, {"phase": "ready"}, {"stack_id": STACK_ID},
                        {"record": "{}"}, {"cas": "confirmed"}, {"extra": 1},
                        {"request_sha256": "0" * 64}):
            with self.assertRaises(ProofStateError):
                validate_state({**self.state, **changes})
        self.act("run_interrupted")
        with self.assertRaises(ProofStateError):
            validate_state({**self.local.read(), "stack_etag": "W/weak"})

    def test_lost_delete_remains_uncertain_and_cannot_repeat(self):
        self.act("run_interrupted")
        self.act("run")
        original = self.cloud

        def lost(request, **kwargs):
            result = original(request, **kwargs)
            if request.method == "DELETE":
                raise OSError("private")
            return result

        self.transport.side_effect = lost
        with self.assertRaises(Exception):
            self.act("cleanup")
        self.assertTrue(self.deleted)
        self.assertEqual(self.local.read()["phase"], "delete_uncertain")
        before = len(self.calls)
        self.assertFalse(self.act("cleanup"))
        self.assertEqual(len(self.calls), before)

    def test_uncertainty_persistence_failure_prevents_dispatch(self):
        with patch.object(self.local, "write", side_effect=OSError("private")):
            with self.assertRaises(OSError):
                self.act("run_interrupted")
        self.assertEqual(self.mutations(), [])
