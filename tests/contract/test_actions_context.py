from dataclasses import replace
import json
import subprocess
import unittest
from unittest.mock import Mock, patch

from troubleshooter_deployment.actions_context import (
    ActionsContextError, RunnerConfigurationError, bootstrap_arguments,
    checked_out_sha, validate_context,
)
from tests.contract.oci_responses import CONFIG as OCI_CONFIG, OCI_IDENTITY
from tests.deployment.fixtures import ATTEMPT

CONFIG = {**OCI_CONFIG, "tenancy": "ocid1.tenancy.oc1..synthetic", "user": "ocid1.user.oc1..synthetic"}
TARGET = "ocid1.compartment.oc1..synthetic"


def environment():
    return {
        "GITHUB_ACTIONS": "true", "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_API_URL": "https://api.github.com", "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main", "GITHUB_REF_TYPE": "branch",
        "GITHUB_REPOSITORY": "synthetic-owner/synthetic-repo", "GITHUB_REPOSITORY_ID": "42",
        "GITHUB_RUN_ID": str(ATTEMPT.run_id), "GITHUB_RUN_ATTEMPT": str(ATTEMPT.number),
        "GITHUB_SHA": ATTEMPT.source_sha, "GITHUB_WORKFLOW_SHA": ATTEMPT.workflow_sha,
        "GITHUB_WORKFLOW_REF": "synthetic-owner/synthetic-repo/.github/workflows/demo.yml@refs/heads/main",
        "GITHUB_JOB": "bootstrap", "RUNNER_ENVIRONMENT": "github-hosted",
        "RUNNER_OS": "Linux", "RUNNER_ARCH": "X64",
        "DEPLOYMENT_TFVARS_JSON": json.dumps({"region": CONFIG["region"],
            "compartment_ocid": TARGET}),
        "OCI_TENANCY_OCID": CONFIG["tenancy"], "OCI_USER_OCID": CONFIG["user"],
        "OCI_FINGERPRINT": CONFIG["fingerprint"], "OCI_PRIVATE_KEY": CONFIG["key_content"],
        "GITHUB_TOKEN": "synthetic-token",
    }


class ActionsContextTests(unittest.TestCase):
    def test_context_and_central_target_build_exact_existing_coordinator_arguments(self):
        values = environment()
        values.update(OCI_REGION="ignored", OCI_COMPARTMENT_ID="ignored", OCI_CONFIG_FILE="ignored")
        args = bootstrap_arguments(values, ATTEMPT.source_sha)
        self.assertEqual(args, {"scope": replace(OCI_IDENTITY.scope, environment="demo",
            tenancy_id=CONFIG["tenancy"], compartment_id=TARGET),
            "current": ATTEMPT, "owner": "synthetic-owner", "repository": "synthetic-repo",
            "github_token": "synthetic-token", "oci_config": CONFIG})

    def test_each_required_context_value_fails_closed_without_secret_diagnostics(self):
        values = environment()
        for key in values.keys() - {"DEPLOYMENT_TFVARS_JSON", "OCI_TENANCY_OCID", "OCI_USER_OCID",
                                    "OCI_FINGERPRINT", "OCI_PRIVATE_KEY", "GITHUB_TOKEN"}:
            for value in (None, "", "private-secret", True):
                with self.subTest(key=key, value=value), self.assertRaises(ActionsContextError) as error:
                    validate_context({**values, key: value}, ATTEMPT.source_sha)
                self.assertNotIn("private-secret", str(error.exception))
        for updates in ({"GITHUB_REF": "refs/heads/dev"}, {"GITHUB_EVENT_NAME": "pull_request_target"},
                        {"GITHUB_WORKFLOW_SHA": "b" * 40}, {"GITHUB_REPOSITORY_ID": str(2**63)},
                        {"GITHUB_RUN_ATTEMPT": "01"}, {"GITHUB_REPOSITORY": "owner/../repo"},
                        {"RUNNER_ENVIRONMENT": "self-hosted"}):
            with self.assertRaises(ActionsContextError):
                validate_context({**values, **updates}, ATTEMPT.source_sha)
        with self.assertRaises(ActionsContextError):
            validate_context(values, "b" * 40)

    def test_configuration_is_bounded_and_errors_never_echo_values(self):
        values = environment()
        for key in ("DEPLOYMENT_TFVARS_JSON", "OCI_TENANCY_OCID", "OCI_USER_OCID", "OCI_FINGERPRINT",
                    "OCI_PRIVATE_KEY", "GITHUB_TOKEN"):
            for value in (None, "", "x" * 20000):
                with self.subTest(key=key), self.assertRaises(RunnerConfigurationError) as error:
                    bootstrap_arguments({**values, key: value}, ATTEMPT.source_sha)
                self.assertEqual(str(error.exception), "runner_configuration_invalid")
        for key, value in (("OCI_TENANCY_OCID", "wrong"), ("OCI_USER_OCID", "wrong"),
                           ("OCI_FINGERPRINT", "secret\nvalue"), ("GITHUB_TOKEN", "a\nb"),
                           ("OCI_PRIVATE_KEY", "key\x00content"), ("DEPLOYMENT_TFVARS_JSON", "private")):
            with self.assertRaises(RunnerConfigurationError):
                bootstrap_arguments({**values, key: value}, ATTEMPT.source_sha)

    def test_context_rejection_precedes_credential_reads(self):
        class ForbiddenSecrets(dict):
            def get(self, key, default=None):
                if key.startswith("OCI_") or key in {"GITHUB_TOKEN", "DEPLOYMENT_TFVARS_JSON"}:
                    raise AssertionError("credential accessed before context validation")
                return super().get(key, default)
        with self.assertRaises(ActionsContextError):
            bootstrap_arguments(ForbiddenSecrets(), ATTEMPT.source_sha)

    def test_git_checkout_is_bounded_and_never_leaks_git_errors(self):
        with patch("troubleshooter_deployment.actions_context.subprocess.run",
                   return_value=Mock(returncode=0, stdout=ATTEMPT.source_sha + "\n")) as command:
            self.assertEqual(checked_out_sha(), ATTEMPT.source_sha)
        self.assertEqual(command.call_args.args[0], ["git", "rev-parse", "--verify", "HEAD"])
        self.assertEqual(command.call_args.kwargs["timeout"], 5)
        self.assertIs(command.call_args.kwargs["stderr"], subprocess.DEVNULL)
        for failure in (OSError("private-secret"), subprocess.TimeoutExpired("git", 5)):
            with patch("troubleshooter_deployment.actions_context.subprocess.run", side_effect=failure):
                with self.assertRaises(ActionsContextError):
                    checked_out_sha()
        for code, output in ((1, ATTEMPT.source_sha), (0, "private-secret")):
            with patch("troubleshooter_deployment.actions_context.subprocess.run",
                       return_value=Mock(returncode=code, stdout=output)):
                with self.assertRaises(ActionsContextError):
                    checked_out_sha()
