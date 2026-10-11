from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from urllib.parse import unquote, urlsplit
from unittest.mock import patch

from tests.contract.oci_responses import CONFIG, OCI_IDENTITY, response
from tests.contract.recovery_store_service import RecoveryStoreService
from tests.contract.test_oci_stack_bootstrap import stack_data
from tests.live.proof_state import ProofStore, ProofStateError
from tests.live.store_bootstrap_state import new_state, validate_state, initial_request
from tests.live.store_bootstrap_services import BootstrapProof


class SavedProof:
    def __init__(self, state):
        self.state, self.fail = deepcopy(state), None

    def write(self, state):
        validate_state(state)
        if state["phase"] == self.fail:
            raise OSError("synthetic-private-write-failure")
        self.state = deepcopy(state)


class StoreBootstrapProofTests(RecoveryStoreService):
    def setUp(self):
        super().setUp()
        scope = replace(OCI_IDENTITY.scope, repository_id=1, environment="store-proof-synthetic")
        self.proof = new_state(scope, CONFIG["user"], "synthetic-ns")
        self.initial = initial_request(self.proof)
        self.stack, self.stack_version = stack_data(self.initial), 1
        self.stack["definedTags"] = {}
        self.saved = SavedProof(self.proof)
        self.sent_states, self.failure = [], None
        self.delete_status = 204

    def send(self, request, **kwargs):
        path = unquote(urlsplit(request.url).path)
        method = request.method
        if hasattr(self, "saved"):
            self.sent_states.append((method, path, deepcopy(self.saved.state)))
        if self.failure == (method, path):
            self.calls.append(("oci", method, path, request))
            raise OSError("synthetic-private-wire-failure")
        handled = False
        if path == "/20180917/stacks":
            handled = True
            result = response([]) if method == "GET" else response(self.stack, headers={"etag": '"s1"'})
        elif method == "DELETE":
            handled = True
            if "/o/" in path:
                self.assertEqual(path.split("/o/")[1], "journal/" + self.state().request.epoch + ".json")
                self.assertEqual(request.headers["if-match"], f'"o{self.object_version}"')
                self.body = None
            elif "/stacks/" in path:
                self.assertEqual(request.headers["if-match"], f'"s{self.stack_version}"')
                self.assertIsNone(self.bucket)
            else:
                self.assertIsNone(self.body)
                self.bucket = None
            result = response(body=b"", status=self.delete_status)
        elif "/stacks/" in path and method == "PUT" and "variables" in json.loads(request.body):
            handled = True
            self.assertEqual(request.headers["if-match"], f'"s{self.stack_version}"')
            self.stack["variables"] = json.loads(request.body)["variables"]
            self.stack_version += 1
            result = response(self.stack, headers={"etag": f'"s{self.stack_version}"'})
        if handled:
            self.calls.append(("oci", method, path, request))
            return result
        return super().send(request, **kwargs)

    def act(self, action):
        state = deepcopy(self.saved.state)
        with BootstrapProof(CONFIG, self.saved, state) as proof:
            return getattr(proof, action)()

    def test_live_shape_setup_fresh_readonly_observation_and_exact_cleanup(self):
        self.assertTrue(self.act("run"))
        setup_calls = len(self.calls)
        self.assertEqual(self.saved.state["phase"], "ready")
        self.assertEqual(self.saved.state["cas_status"], "confirmed")
        before, mutation_count = deepcopy(self.saved.state), len(self.mutations())
        self.assertTrue(self.act("observe"))
        self.assertEqual(self.saved.state, before)
        self.assertEqual(len(self.mutations()), mutation_count)
        self.assertEqual(len(self.calls) - setup_calls, 5)
        self.assertTrue(self.act("cleanup"))
        self.assertEqual(self.saved.state["phase"], "complete")
        deletes = [(c[2], c[3]) for c in self.calls if c[1] == "DELETE"]
        self.assertEqual(len(deletes), 3)
        self.assertIn("/o/journal%2F", deletes[0][1].url)
        self.assertIn("/n/synthetic-ns/b/", deletes[1][0])
        self.assertIn("/stacks/", deletes[2][0])
        self.assertLessEqual(len(self.calls), 100)
        self.assertEqual(sum(c[1] == "POST" and c[2].endswith("/b") for c in self.calls), 1)
        self.assertEqual(sum(c[1] == "POST" and c[2].endswith("/stacks") for c in self.calls), 1)
        self.assertTrue(all(s["phase"].endswith("uncertain") for m, _, s in self.sent_states if m == "DELETE"))
        self.assertEqual([m for m, p, s in self.sent_states if "/o/" in p and m == "PUT"], ["PUT", "PUT"])
        before_calls = len(self.calls)
        self.assertTrue(self.act("cleanup"))
        self.assertEqual(len(self.calls), before_calls)

    def test_uncertain_creations_are_not_dispatched_again(self):
        for path, phase in [("/20180917/stacks", "stack_uncertain"), ("/n/synthetic-ns/b", "bucket_uncertain")]:
            with self.subTest(path=path):
                self.saved = SavedProof(self.proof)
                self.stack, self.stack_version = stack_data(self.initial), 1
                self.stack["definedTags"] = {}
                self.calls.clear()
                self.failure = "POST", path
                with self.assertRaises(Exception):
                    self.act("run")
                self.assertEqual(self.saved.state["phase"], phase)
                calls = len(self.calls)
                self.failure = None
                self.assertFalse(self.act("run"))
                self.assertFalse(self.act("cleanup"))
                self.assertEqual(len(self.calls), calls)

    def test_failed_private_write_prevents_create(self):
        self.saved.fail = "stack_uncertain"
        with self.assertRaises(OSError):
            self.act("run")
        self.assertEqual(self.mutations(), [])

    def test_lost_journal_delete_retains_receipt_and_never_repeats_or_deletes_bucket(self):
        self.act("run")
        name = self.state().request.name
        self.failure = "DELETE", f"/n/synthetic-ns/b/{name}/o/journal/{self.state().request.epoch}.json"
        with self.assertRaises(Exception):
            self.act("cleanup")
        self.assertEqual(self.saved.state["phase"], "object_delete_uncertain")
        self.assertIsNotNone(self.body)
        calls = len(self.calls)
        self.failure = None
        self.assertFalse(self.act("cleanup"))
        self.assertEqual(len(self.calls), calls)
        self.assertEqual(sum(c[1] == "DELETE" for c in self.calls), 1)

    def test_changed_journal_or_bucket_prevents_observation_and_cleanup(self):
        self.act("run")
        original = deepcopy(self.bucket)
        self.bucket["id"] = "different-bucket"
        with self.assertRaises(Exception):
            self.act("observe")
        with self.assertRaises(Exception):
            self.act("cleanup")
        self.assertFalse(any(c[1] == "DELETE" for c in self.calls))
        self.bucket = original
        self.body = b"{}"
        with self.assertRaises(Exception):
            self.act("cleanup")
        self.assertFalse(any(c[1] == "DELETE" for c in self.calls))

    def test_new_validator_rejects_corruption_and_private_store_preserves_old_default(self):
        for delta in ({"phase": "ready"}, {"schema": True}, {"unexpected": 1},
                      {"request_sha256": "0" * 64}, {"scope": {}}):
            with self.subTest(delta=delta), self.assertRaises(Exception):
                validate_state({**self.proof, **delta})
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp).resolve() / "state"
            with ProofStore(path, validator=validate_state) as store:
                store.write(self.proof)
                self.assertEqual(store.read(), self.proof)
            with ProofStore(path) as old_store:
                with self.assertRaises(ProofStateError):
                    old_store.read()

    def test_real_service_cas_probe_must_reject_stale_revision(self):
        self.act("run")
        stale = [r for r in self.mutations() if r.method == "PUT" and r.headers.get("if-match") == '"s1"']
        self.assertEqual(len(stale), 2)
        self.assertEqual(json.loads(stale[-1].body)["freeformTags"], self.stack["freeformTags"])

    def test_cleanup_never_claims_success_from_an_unexpected_delete_status(self):
        self.act("run")
        self.delete_status = 202
        with self.assertRaises(ValueError):
            self.act("cleanup")
        self.assertEqual(self.saved.state["phase"], "object_delete_uncertain")
        calls = len(self.calls)
        self.assertFalse(self.act("cleanup"))
        self.assertEqual(len(self.calls), calls)

    def test_journal_revision_change_prevents_cleanup_even_with_identical_bytes(self):
        self.act("run")
        self.object_version += 1
        with self.assertRaises(ValueError):
            self.act("cleanup")
        self.assertFalse(any(c[1] == "DELETE" for c in self.calls))
        self.assertEqual(self.saved.state["phase"], "ready")

    def test_failed_saved_delete_intent_prevents_object_delete(self):
        self.act("run")
        self.saved.fail = "object_delete_uncertain"
        with self.assertRaises(OSError):
            self.act("cleanup")
        self.assertFalse(any(c[1] == "DELETE" for c in self.calls))
        self.assertEqual(self.saved.state["phase"], "purge_ready")

    def test_nonempty_or_incomplete_jobs_block_stack_delete(self):
        for jobs, headers in [([{"id": "synthetic-job"}], {}), ([], {"opc-next-page": "next"})]:
            with self.subTest(jobs=jobs), patch.object(self, "jobs", jobs), patch.object(self, "job_headers", headers):
                self.saved = SavedProof(self.proof)
                self.stack, self.stack_version = stack_data(self.initial), 1
                self.stack["definedTags"] = {}
                # Jobs appear only after setup: no jobs may be submitted by the proof.
                with patch.object(self, "jobs", []), patch.object(self, "job_headers", {}):
                    self.act("run")
                self.calls.clear()
                with self.assertRaises(ValueError):
                    self.act("cleanup")
                self.assertFalse(any(c[1] == "DELETE" and "/stacks/" in c[2] for c in self.calls))
                self.assertEqual(self.saved.state["phase"], "bucket_deleted")

    def test_lost_bucket_delete_is_never_redispatched(self):
        self.act("run")
        self.failure = "DELETE", f"/n/synthetic-ns/b/{self.state().request.name}"
        with self.assertRaises(Exception):
            self.act("cleanup")
        self.assertEqual(self.saved.state["phase"], "bucket_delete_uncertain")
        calls = len(self.calls)
        self.assertFalse(self.act("cleanup"))
        self.assertEqual(len(self.calls), calls)

    def test_lost_stack_delete_is_never_redispatched(self):
        self.act("run")
        self.failure = "DELETE", "/20180917/stacks/" + self.saved.state["stack_id"]
        with self.assertRaises(Exception):
            self.act("cleanup")
        self.assertEqual(self.saved.state["phase"], "stack_delete_uncertain")
        calls = len(self.calls)
        self.assertFalse(self.act("cleanup"))
        self.assertEqual(len(self.calls), calls)

    def test_failed_receipt_survival_read_preserves_stack(self):
        self.act("run")
        original = self.send

        def lose_survival_read(request, **kwargs):
            if request.method == "GET" and self.saved.state["phase"] == "bucket_deleted":
                return response({"code": "NotAuthorizedOrNotFound"}, status=404)
            return original(request, **kwargs)

        self.transport.side_effect = lose_survival_read
        with self.assertRaises(Exception):
            self.act("cleanup")
        self.assertEqual(self.saved.state["phase"], "bucket_deleted")
        self.assertFalse(any(c[1] == "DELETE" and "/stacks/" in c[2] for c in self.calls))

    def test_unexpected_stale_cas_acceptance_does_not_pass_or_repeat(self):
        original = self.send

        def accept_stale(request, **kwargs):
            if request.method == "PUT" and request.headers.get("if-match") == '"s1"' and self.stack_version > 1:
                self.calls.append(("oci", "PUT", urlsplit(request.url).path, request))
                return response(self.stack)
            return original(request, **kwargs)

        self.transport.side_effect = accept_stale
        with self.assertRaisesRegex(ValueError, "proof_stale_revision_accepted"):
            self.act("run")
        self.assertEqual(self.saved.state["cas_status"], "uncertain")
        calls = len(self.calls)
        self.assertFalse(self.act("run"))
        self.assertFalse(self.act("cleanup"))
        self.assertEqual(len(self.calls), calls)
