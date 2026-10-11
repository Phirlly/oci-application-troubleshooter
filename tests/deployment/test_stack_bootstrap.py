from base64 import b64decode
from dataclasses import replace
from hashlib import sha256
from io import BytesIO
import json
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from troubleshooter_deployment.bootstrap_commitment import commitment_payload, freeze_stack_request
from troubleshooter_deployment.bucket_deletion import KEY
from troubleshooter_deployment.receipt_variables import KEYS
from troubleshooter_deployment.stack_bootstrap import (
    BOOTSTRAP_VARIABLE, InitialStackError, build_initial_stack, require_retry_window,
    validate_initial_stack,
)
from tests.deployment.fixtures import ATTEMPT, IDENTITY, NEXT_ATTEMPT


class InitialStackRequestTests(unittest.TestCase):
    def test_reconstruction_is_exact_and_zip_contains_only_declared_carriers(self):
        request = build_initial_stack(IDENTITY.scope, ATTEMPT, "synthetic-user")
        self.assertEqual(request, build_initial_stack(IDENTITY.scope, ATTEMPT, "synthetic-user"))
        # Compatibility fixture: v1 reconstruction must survive another runner/release.
        self.assertEqual(sha256(request.body).hexdigest(),
                         "43adfc677690025a6cdd0b0efe21c73901342ae008b5ff31dbe9387d6ecc0da1")
        validate_initial_stack(request)
        body = json.loads(request.body)
        self.assertEqual(body["terraformVersion"], "1.5.x")
        self.assertEqual(set(body["variables"]), {BOOTSTRAP_VARIABLE})
        metadata = json.loads(body["variables"][BOOTSTRAP_VARIABLE])
        self.assertEqual(metadata["principal"], "synthetic-user")
        self.assertEqual(metadata["attempt"]["number"], 1)
        with ZipFile(BytesIO(b64decode(body["configSource"]["zipFileBase64Encoded"]))) as archive:
            self.assertEqual(archive.namelist(), ["main.tf"])
            source = archive.read("main.tf").decode()
            self.assertEqual(source.count('variable "'), 5)
            for key in KEYS | {KEY, BOOTSTRAP_VARIABLE}:
                self.assertIn(f'variable "{key}"', source)
            self.assertNotIn("resource ", source)
            self.assertNotIn("provider ", source)
            self.assertEqual(archive.getinfo("main.tf").date_time, (2026, 1, 1, 0, 0, 0))
        public = json.dumps(commitment_payload(freeze_stack_request(IDENTITY.scope, ATTEMPT, request.body)))
        for private in [IDENTITY.scope.tenancy_id, IDENTITY.scope.compartment_id, "synthetic-user", request.token]:
            self.assertNotIn(private, public)

    def test_token_binds_scope_and_original_run_not_attempt_or_principal(self):
        original = build_initial_stack(IDENTITY.scope, ATTEMPT, "synthetic-user")
        for attempt, principal in [(NEXT_ATTEMPT, "synthetic-user"), (ATTEMPT, "other-user")]:
            other = build_initial_stack(IDENTITY.scope, attempt, principal)
            self.assertEqual(original.token, other.token)
            self.assertNotEqual(original.body, other.body)
        for scope, attempt in [(replace(IDENTITY.scope, environment="other"), ATTEMPT),
                               (IDENTITY.scope, replace(ATTEMPT, run_id=101))]:
            self.assertNotEqual(original.token, build_initial_stack(scope, attempt, "synthetic-user").token)

    def test_invalid_inputs_and_tampering_are_rejected_without_private_errors(self):
        for scope, attempt, principal in [(None, ATTEMPT, "user"), (IDENTITY.scope, None, "user"),
                                         (IDENTITY.scope, ATTEMPT, "private\nuser"),
                                         (IDENTITY.scope, ATTEMPT, None)]:
            with self.assertRaisesRegex(InitialStackError, "invalid_initial_stack_request"):
                build_initial_stack(scope, attempt, principal)
        valid = build_initial_stack(IDENTITY.scope, ATTEMPT, "user")
        for request in [None, replace(valid, body=b"private"), replace(valid, token="other"),
                        replace(valid, display_name="other")]:
            with self.assertRaises(InitialStackError):
                validate_initial_stack(request)

    def test_retry_window_is_conservative_and_rejects_future_or_invalid_time(self):
        with patch("troubleshooter_deployment.stack_bootstrap.time", return_value=1791594000):  # 2026-10-10 01:00Z
            require_retry_window("2026-10-10T01:00:00Z")
            require_retry_window("2026-10-09T02:00:01Z")
            for timestamp in ["2026-10-09T02:00:00Z", "2026-10-10T01:00:01Z", "bad", None,
                              "2026-10-10T01:00:00+00:00", "2026-02-30T01:00:00Z"]:
                with self.assertRaises(InitialStackError):
                    require_retry_window(timestamp)
