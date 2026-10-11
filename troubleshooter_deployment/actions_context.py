"""Inputs for the trusted main-only bootstrap workflow, not trust attestation.

Administrative enrollment and exclusive controller/history retention are external
prerequisites. Never infer them from environment variables or log these inputs.
"""
from dataclasses import dataclass
from pathlib import Path
import re
import subprocess

from .codec import validate_attempt
from .deployment_config import from_environment
from .records import Attempt

ROOT = Path(__file__).resolve().parents[1]


class ActionsContextError(ValueError):
    def __init__(self):
        super().__init__("actions_context_invalid")


class RunnerConfigurationError(ValueError):
    def __init__(self):
        super().__init__("runner_configuration_invalid")


@dataclass(frozen=True, repr=False)
class ActionsContext:
    owner: str
    repository: str
    repository_id: int
    current: Attempt


def _text(value, pattern):
    if type(value) is not str or re.fullmatch(pattern, value) is None:
        raise ValueError()
    return value


def _number(value):
    number = int(_text(value, r"[1-9][0-9]{0,18}"))
    if number >= 2**63:
        raise ValueError()
    return number


def validate_context(environ, checkout_sha):
    try:
        expected = {"GITHUB_ACTIONS": "true", "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_API_URL": "https://api.github.com", "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_REF": "refs/heads/main", "GITHUB_REF_TYPE": "branch", "GITHUB_JOB": "bootstrap",
            "RUNNER_ENVIRONMENT": "github-hosted", "RUNNER_OS": "Linux", "RUNNER_ARCH": "X64"}
        if any(environ.get(key) != value for key, value in expected.items()):
            raise ValueError()
        name = _text(environ.get("GITHUB_REPOSITORY"), r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}")
        owner, repository = name.split("/")
        if repository in {".", ".."}:
            raise ValueError()
        current = Attempt(_number(environ.get("GITHUB_RUN_ID")), _number(environ.get("GITHUB_RUN_ATTEMPT")),
                          environ.get("GITHUB_WORKFLOW_SHA"), environ.get("GITHUB_SHA"))
        validate_attempt(current)
        if (checkout_sha != current.source_sha or current.workflow_sha != current.source_sha
                or environ.get("GITHUB_WORKFLOW_REF") != name + "/.github/workflows/demo.yml@refs/heads/main"):
            raise ValueError()
        return ActionsContext(owner, repository, _number(environ.get("GITHUB_REPOSITORY_ID")), current)
    except Exception:
        raise ActionsContextError() from None


def checked_out_sha():
    try:
        result = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=ROOT,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=5, check=False)
        if result.returncode != 0:
            raise ValueError()
        return _text(result.stdout.strip(), r"[0-9a-f]{40}")
    except Exception:
        raise ActionsContextError() from None


def bootstrap_arguments(environ, checkout_sha):
    context = validate_context(environ, checkout_sha)
    try:
        target = from_environment(environ)
        config = {"tenancy": _text(environ.get("OCI_TENANCY_OCID"), r"ocid1\.tenancy\.oc1\.\.[A-Za-z0-9]{1,230}"),
                  "user": _text(environ.get("OCI_USER_OCID"), r"ocid1\.user\.oc1\.\.[A-Za-z0-9]{1,230}"),
                  "fingerprint": _text(environ.get("OCI_FINGERPRINT"), r"[0-9a-f]{2}(:[0-9a-f]{2}){15}"),
                  "region": target.region,
                  "key_content": _text(environ.get("OCI_PRIVATE_KEY"), r"[\x09\x0a\x0d\x20-\x7e]{1,16384}")}
        token = _text(environ.get("GITHUB_TOKEN"), r"[!-~]{1,1024}")
        return {"scope": target.scope(context.repository_id, "demo", config["tenancy"]),
                "current": context.current, "owner": context.owner, "repository": context.repository,
                "github_token": token, "oci_config": config}
    except Exception:
        raise RunnerConfigurationError() from None
