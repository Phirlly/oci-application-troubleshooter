"""One fixed-origin GitHub attempt observation; never an execution or cloud fence."""
from contextlib import ExitStack
from http.client import HTTPSConnection
import json
import re

from .codec import MAX_BYTES, encode, validate_attempt
from .records import Attempt, Record, RunObservation
from .request_budget import operation_budget


class GitHubError(RuntimeError):
    def __init__(self):
        super().__init__("github_attempt_unavailable_or_mismatched")


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise GitHubError()
        result[key] = value
    return result


def _constant(value):
    raise GitHubError()


class GitHubAttempts:
    def __init__(self, owner: str, repository: str, token: str):
        if (type(owner) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", owner)
                or type(repository) is not str or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", repository)
                or repository in {".", ".."} or type(token) is not str
                or not re.fullmatch(r"[!-~]{1,1024}", token)):
            raise GitHubError()
        self._owner = owner
        self._repository = repository
        self._token = token

    def observe(self, record: Record) -> RunObservation:
        encode(record)
        return self.observe_attempt(record.identity.scope.repository_id, record.owner)

    def observe_attempt(self, repository_id: int, attempt: Attempt) -> RunObservation:
        """source_sha is the triggering commit, not an alternate checkout ref.

        GitHub does not return the caller workflow_sha. That field remains
        journal provenance; it is deliberately absent from this observation.
        """
        try:
            validate_attempt(attempt)
            if type(repository_id) is not int or not 0 < repository_id < 2**63:
                raise GitHubError()
            path = (f"/repos/{self._owner}/{self._repository}/actions/runs/"
                    f"{attempt.run_id}/attempts/{attempt.number}?exclude_pull_requests=true")
            with operation_budget(), ExitStack() as cleanup:
                connection = HTTPSConnection("api.github.com", timeout=5)
                cleanup.callback(connection.close)
                connection.request("GET", path, headers={
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2026-03-10",
                    "User-Agent": "oci-application-troubleshooter",
                    "Accept-Encoding": "identity",
                })
                response = connection.getresponse()
                cleanup.callback(response.close)
                if response.status != 200 or response.getheader("Content-Encoding") not in (None, "identity"):
                    raise GitHubError()
                length = response.getheader("Content-Length")
                if length is not None and (not re.fullmatch(r"[0-9]{1,9}", length) or int(length) > MAX_BYTES):
                    raise GitHubError()
                body = response.read(MAX_BYTES + 1)
                if len(body) > MAX_BYTES or (length is not None and len(body) != int(length)):
                    raise GitHubError()
                data = json.loads(body, object_pairs_hook=_unique_pairs, parse_constant=_constant)
                expected = (repository_id, attempt.run_id, attempt.number, attempt.source_sha)
                actual = (data["repository"]["id"], data["id"], data["run_attempt"], data["head_sha"])
                if (any(type(value) is not int for value in actual[:3]) or type(actual[3]) is not str
                        or actual != expected or type(data["status"]) is not str
                        or not re.fullmatch(r"[a-z_]{1,32}", data["status"])):
                    raise GitHubError()
                observation = RunObservation(*actual, data["status"])
            return observation
        except Exception:
            raise GitHubError() from None
