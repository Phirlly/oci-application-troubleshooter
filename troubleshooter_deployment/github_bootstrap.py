"""GitHub bootstrap commitments, without OCI dispatch or automatic write retries.

History is public and deletable. Discovery is not an atomic snapshot, proof of
absence, workflow attestation, cloud ownership or authorization to create.
"""
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime
from http.client import HTTPSConnection
import json
import re
from urllib.parse import parse_qs, urlsplit

from .bootstrap_commitment import StackBootstrapCommitment, commitment_payload, parse_commitment, scope_digest
from .codec import MAX_BYTES
from .request_budget import operation_budget
from .stack_ownership import StackOwnershipCommitment, ownership_payload, parse_ownership

BOOTSTRAP_TASK = "oci-troubleshooter-bootstrap-v1"
OWNERSHIP_TASK = "oci-troubleshooter-stack-ownership-v1"


class BootstrapError(RuntimeError):
    def __init__(self):
        super().__init__("github_bootstrap_unavailable_or_mismatched")


class BootstrapWriteUncertain(BootstrapError):
    def __init__(self):
        RuntimeError.__init__(self, "github_bootstrap_write_uncertain_reconcile_before_dispatch")


@dataclass(frozen=True)
class SavedCommitment:
    deployment_id: int
    created_at: str
    commitment: StackBootstrapCommitment | StackOwnershipCommitment


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BootstrapError()
        result[key] = value
    return result


def _constant(value):
    raise BootstrapError()


class GitHubBootstrap:
    def __init__(self, owner, repository, token):
        if (type(owner) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", owner) is None
                or type(repository) is not str or re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", repository) is None
                or repository in {".", ".."} or type(token) is not str
                or re.fullmatch(r"[!-~]{1,1024}", token) is None):
            raise BootstrapError()
        self._repository = f"/repos/{owner}/{repository}"
        self._path, self._token = self._repository + "/deployments", token

    def _request(self, method, path, body=None, expected=200):
        with ExitStack() as cleanup:
            connection = HTTPSConnection("api.github.com", timeout=5)
            cleanup.callback(connection.close)
            headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/vnd.github+json",
                       "X-GitHub-Api-Version": "2026-03-10", "User-Agent": "oci-application-troubleshooter",
                       "Accept-Encoding": "identity", "Content-Type": "application/json"}
            encoded = None if body is None else json.dumps(body, separators=(",", ":"), allow_nan=False).encode()
            connection.request(method, path, body=encoded, headers=headers)
            response = connection.getresponse()
            cleanup.callback(response.close)
            if response.status != expected or response.getheader("Content-Encoding") not in (None, "identity"):
                raise BootstrapError()
            length = response.getheader("Content-Length")
            if length is not None and (re.fullmatch(r"[0-9]{1,9}", length) is None or int(length) > MAX_BYTES):
                raise BootstrapError()
            content = response.read(MAX_BYTES + 1)
            if len(content) > MAX_BYTES or (length is not None and len(content) != int(length)):
                raise BootstrapError()
            return (json.loads(content, object_pairs_hook=_pairs, parse_constant=_constant),
                    response.getheader("Link"))

    def _verify_repository(self, repository_id):
        data, _ = self._request("GET", self._repository)
        if type(data) is not dict or type(data.get("id")) is not int or data["id"] != repository_id:
            raise BootstrapError()

    def _entry(self, data, repository_id, ownership=False):
        commitment = parse_ownership(data["payload"]) if ownership else parse_commitment(data["payload"])
        original = commitment.bootstrap if ownership else commitment
        task = OWNERSHIP_TASK if ownership else BOOTSTRAP_TASK
        if (original.repository_id != repository_id or type(data["id"]) is not int
                or not 0 < data["id"] < 2**63 or data["task"] != task
                or data["sha"] != original.attempt.source_sha or data["ref"] != data["sha"]
                or data["environment"] != original.environment
                or data["repository_url"].casefold() != ("https://api.github.com" + self._repository).casefold()
                or type(data["created_at"]) is not str
                or re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", data["created_at"]) is None):
            raise BootstrapError()
        datetime.fromisoformat(data["created_at"])
        return SavedCommitment(data["id"], data["created_at"], commitment)

    def publish(self, commitment):
        """Persist once and read back. Caller must reconcile history before use.

        Even a confirmed result does not authorize OCI dispatch by itself.
        """
        return self._publish(commitment, ownership=False)

    def publish_ownership(self, commitment):
        """Retain prior positive ACK evidence; caller must validate it first."""
        return self._publish(commitment, ownership=True)

    def _publish(self, commitment, *, ownership):
        dispatched = False
        try:
            payload = ownership_payload(commitment) if ownership else commitment_payload(commitment)
            original = commitment.bootstrap if ownership else commitment
            task = OWNERSHIP_TASK if ownership else BOOTSTRAP_TASK
            with operation_budget():
                self._verify_repository(original.repository_id)
                body = {"ref": original.attempt.source_sha, "task": task,
                        "environment": original.environment, "auto_merge": False, "required_contexts": [],
                        "transient_environment": False, "production_environment": False, "payload": payload}
                dispatched = True
                data, _ = self._request("POST", self._path, body, 201)
                saved = self._entry(data, original.repository_id, ownership)
                if saved.commitment != commitment:
                    raise BootstrapError()
                readback, _ = self._request("GET", f"{self._path}/{saved.deployment_id}")
                if self._entry(readback, original.repository_id, ownership) != saved:
                    raise BootstrapError()
            return saved
        except Exception:
            if dispatched:
                raise BootstrapWriteUncertain() from None
            raise BootstrapError() from None

    def _next(self, link, repository_id, current, task):
        if link is None:
            return None
        if type(link) is not str or not 0 < len(link) <= 4096:
            raise BootstrapError()
        relations = {}
        for part in link.split(","):
            match = re.fullmatch(r'\s*<([^<>\s]+)>;\s*rel="(next|prev|first|last)"\s*', part)
            if match is None or match[2] in relations:
                raise BootstrapError()
            url = urlsplit(match[1])
            query = parse_qs(url.query, strict_parsing=True, keep_blank_values=True, max_num_fields=3)
            if (url.scheme != "https" or url.netloc != "api.github.com" or url.fragment
                    or url.path.casefold() not in (self._path.casefold(), f"/repositories/{repository_id}/deployments")
                    or set(query) != {"task", "per_page", "page"} or query["task"] != [task]
                    or query["per_page"] != ["10"] or len(query["page"]) != 1
                    or re.fullmatch(r"[1-9][0-9]{0,9}", query["page"][0]) is None):
                raise BootstrapError()
            relations[match[2]] = int(query["page"][0])
        following = relations.get("next")
        if (following is not None and following != current + 1
                or "first" in relations and relations["first"] != 1
                or "prev" in relations and relations["prev"] != current - 1):
            raise BootstrapError()
        last = relations.get("last")
        if last is not None and (last < current or (following is None and last != current)
                                 or (following is not None and last < following)):
            raise BootstrapError()
        return following

    def discover(self, scope):
        """Return bounded observed history. Empty history never grants creation."""
        return self._discover(scope, ownership=False)

    def discover_ownership(self, scope):
        """Read retained positive-ACK bindings, never infer them from names."""
        return self._discover(scope, ownership=True)

    def _discover(self, scope, *, ownership):
        try:
            target = scope_digest(scope)
            task = OWNERSHIP_TASK if ownership else BOOTSTRAP_TASK
            results, seen, runs = [], set(), {}
            with operation_budget():
                self._verify_repository(scope.repository_id)
                for page in range(1, 6):
                    data, link = self._request("GET", self._path +
                        f"?task={task}&per_page=10&page={page}")
                    if type(data) is not list or len(data) > 10:
                        raise BootstrapError()
                    for row in data:
                        saved = self._entry(row, scope.repository_id, ownership)
                        if saved.deployment_id in seen:
                            raise BootstrapError()
                        seen.add(saved.deployment_id)
                        intent = saved.commitment
                        original = intent.bootstrap if ownership else intent
                        key = (original.environment, original.attempt.run_id)
                        if key in runs and runs[key] != intent:
                            raise BootstrapError()
                        runs[key] = intent
                        if original.environment == scope.environment:
                            if original.scope_sha256 != target:
                                raise BootstrapError()
                            results.append(saved)
                    if self._next(link, scope.repository_id, page, task) is None:
                        break
                else:
                    raise BootstrapError()
            return tuple(results)
        except Exception:
            raise BootstrapError() from None
