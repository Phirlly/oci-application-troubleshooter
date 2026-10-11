from dataclasses import replace
from datetime import datetime, timezone
import json
import unittest

from troubleshooter_deployment.bucket_bootstrap import (
    PREFIX, BootstrapError, acknowledge, enroll, merge_bootstrap, read_bootstrap,
    require_dispatch_window, window,
)
from troubleshooter_deployment.recovery_bucket import BucketAcknowledgement
from troubleshooter_deployment.records import Identity
from troubleshooter_deployment.stack_bootstrap import build_initial_stack
from tests.deployment.fixtures import ATTEMPT, NEXT_ATTEMPT, IDENTITY

NOW = datetime(2026, 10, 11, 12, 0, 35, tzinfo=timezone.utc).timestamp()


class BucketBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.initial = build_initial_stack(IDENTITY.scope, ATTEMPT, "synthetic-user")
        self.tags = json.loads(self.initial.body)["freeformTags"]
        self.state = enroll(self.initial, "synthetic-stack", "synthetic-ns", ATTEMPT, NOW)
        self.ack = BucketAcknowledgement(self.state.request,
            Identity(IDENTITY.scope, "synthetic-bucket", self.state.request.epoch))

    def merge(self, tags, state, defined=None):
        return merge_bootstrap(self.initial, "synthetic-stack", tags, {} if defined is None else defined, state)

    def read(self, tags, defined=None):
        return read_bootstrap(self.initial, "synthetic-stack", tags, {} if defined is None else defined)

    def test_roundtrip_all_phases_preserves_other_tags_and_frozen_request(self):
        tags = {**self.tags, "Department": "Public demo"}
        self.assertIsNone(self.read(tags))
        for state in [self.state, acknowledge(self.state, self.ack),
                      replace(acknowledge(self.state, self.ack), phase="ready")]:
            tags = self.merge(tags, state)
            self.assertEqual(self.read(tags), state)
            self.assertEqual(tags["Department"], "Public demo")
            self.assertTrue(all(len(k) <= 100 and len(v) <= 256 for k, v in tags.items()))
        self.assertEqual(len(tags), 7)
        self.assertEqual(self.state.request, enroll(self.initial, "synthetic-stack", "synthetic-ns",
                                                  NEXT_ATTEMPT, NOW + 600).request)
        self.assertNotEqual(self.state.request, enroll(self.initial, "another-stack", "synthetic-ns",
                                                     ATTEMPT, NOW).request)

    def test_canonical_closed_schema_and_request_binding_reject_corruption(self):
        tags = self.merge(self.tags, self.state)
        variants = [{**tags, PREFIX + "extra": "x"}, {**tags, PREFIX + "id": "unexpected"},
                    {k: v for k, v in tags.items() if k != PREFIX + "namespace"},
                    {**tags, PREFIX + "namespace": "other"}, {**tags, PREFIX + "attempt": "[]"}]
        intent = json.loads(tags[PREFIX + "intent"])
        for delta in [{"schema": True}, {"state": "unknown"}, {"started": "2026-10-11T12:00:01Z"},
                      {"request_sha256": "wrong"}, {"extra": 1}]:
            variants.append({**tags, PREFIX + "intent": json.dumps({**intent, **delta},
                             sort_keys=True, separators=(",", ":"))})
        variants.extend([{**tags, PREFIX + "intent": " " + tags[PREFIX + "intent"]},
                         {**tags, PREFIX + "intent": '{"schema":1,' + tags[PREFIX + "intent"][1:]},
                         {**self.tags, "troubleshooter_bucket_v2_intent": "future"}])
        for bad in variants:
            with self.subTest(bad=bad), self.assertRaises(BootstrapError):
                self.read(bad)
        with self.assertRaises(BootstrapError):
            read_bootstrap(self.initial, "other-stack", tags, {})

    def test_only_forward_steps_and_matching_ack_are_accepted(self):
        initializing = acknowledge(self.state, self.ack)
        for state in [initializing, replace(initializing, phase="ready"),
                      replace(self.state, phase=True), replace(self.state, started="invalid")]:
            with self.assertRaises(BootstrapError):
                self.merge(self.tags, state)
        tags = self.merge(self.tags, self.state)
        for state in [self.state, replace(initializing, phase="ready"),
                      replace(initializing, attempt=NEXT_ATTEMPT), replace(initializing, bucket_id=None)]:
            with self.assertRaises(BootstrapError):
                self.merge(tags, state)
        for ack in [None, replace(self.ack, request=replace(self.ack.request, name="other")),
                    replace(self.ack, identity=replace(self.ack.identity, epoch="f" * 32))]:
            with self.assertRaises(BootstrapError):
                acknowledge(self.state, ack)

    def test_tag_limits_reserve_full_future_id_and_include_defined_data(self):
        full = {**self.tags, **{f"extra{i}": "x" for i in range(4)}}
        tags = self.merge(full, self.state)
        initializing = acknowledge(self.state, replace(self.ack,
            identity=replace(self.ack.identity, bucket_id="x" * 256)))
        self.assertEqual(len(self.merge(tags, initializing)), 10)
        for bad in [{**full, "overflow": "x"}, {**self.tags, "bad.key": "x"},
                    {**self.tags, "k": "x" * 257}, {**self.tags, "k": 1}, [],
                    {**self.tags, "x" * 101: "v"}]:
            with self.assertRaises(BootstrapError):
                self.merge(bad, self.state)
        for defined in [[], {"ns": []}, {"ns": {str(i): "x" for i in range(65)}},
                        {"ns": {str(i): "é" * 256 for i in range(10)}}]:
            with self.assertRaises(BootstrapError):
                self.merge(self.tags, self.state, defined)
        self.assertEqual(self.read(self.merge(self.tags, self.state, {"ns": {"k": ["a", "b"]}}),
                                   {"ns": {"k": ["a", "b"]}}), self.state)

    def test_future_identity_capacity_is_reserved_before_create(self):
        tags = self.merge(self.tags, self.state)
        defined = {"ns": {str(i): "x" * 200 for i in range(20)}}
        # Actual current JSON fits; the later 256-character ID must fit too.
        for padding in range(257):
            defined["ns"]["padding"] = "x" * padding
            size = len(json.dumps({"freeformTags": tags, "definedTags": defined},
                sort_keys=True, separators=(",", ":")).encode())
            if 4780 <= size <= 5000:
                break
        self.assertTrue(4780 <= size <= 5000)
        with self.assertRaises(BootstrapError):
            self.merge(self.tags, self.state, defined)

    def test_capacity_counts_sdk_unicode_escaping_and_json_spacing(self):
        tags = {**self.tags, **{str(i): "é" * 256 for i in range(3)}}
        with self.assertRaises(BootstrapError):
            self.merge(tags, self.state)
        # A small Unicode value remains supported and is preserved verbatim.
        tags = self.merge({**self.tags, "Department": "Démonstration"}, self.state)
        self.assertEqual(tags["Department"], "Démonstration")
        self.assertEqual(self.read(tags), self.state)

    def test_invalid_enrollment_and_expired_dispatch_do_not_change_window(self):
        for initial, stack, ns, attempt, now in [
            (None, "stack", "ns", ATTEMPT, NOW), (self.initial, "bad/id", "ns", ATTEMPT, NOW),
            (self.initial, "stack", "bad/ns", ATTEMPT, NOW),
            (self.initial, "stack", "ns", replace(ATTEMPT, source_sha="f" * 40), NOW),
            (self.initial, "stack", "ns", ATTEMPT, float("nan")),
        ]:
            with self.assertRaises(BootstrapError):
                enroll(initial, stack, ns, attempt, now)
        start, end = window(self.state)
        self.assertEqual((start.second, end.timestamp() - start.timestamp()), (0, 3600))
        require_dispatch_window(self.state, NOW)
        for now in [start.timestamp() - 1, end.timestamp(), NOW + 7200]:
            with self.assertRaises(BootstrapError):
                require_dispatch_window(self.state, now)
