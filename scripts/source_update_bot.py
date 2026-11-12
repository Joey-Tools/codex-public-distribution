#!/usr/bin/env python3
"""Open and merge exact-head public source-pin pull requests."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import selectors
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import refresh_sources, source_ci


PUBLIC_REPOSITORY = "Joey-Tools/codex-public-distribution"
BASE_BRANCH = "master"
LOCK_PATH = "sources.lock.json"
WORKFLOW_PATH = ".github/workflows/ci.yml"
WORKFLOW_JOBS = {
    "Build and validate public package": "success",
    "macOS package integration": "success",
    "Distribution CI": "success",
    "Publish immutable distribution": "skipped",
}
PR_MARKER = "<!-- codex-public-source-update:v1 -->"
BASE_MARKER_PREFIX = "<!-- source-base-sha:"
LOCK_MARKER_PREFIX = "<!-- source-lock-sha256:"
COMMIT_MARKER_PREFIX = "Codex-Source-Update: "
BRANCH_RE = re.compile(r"^codex/source-pins-([0-9a-f]{12})-([0-9a-f]{12})$")
FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class BotError(RuntimeError):
    """A safe source update or merge precondition was not established."""


class APIError(BotError):
    """A bounded GitHub API operation failed."""


class IndeterminateMutation(BotError):
    """A mutation may have reached GitHub; do not replay it in this invocation."""


@dataclass(frozen=True)
class Result:
    status: str
    reason: str | None = None
    pull_request: int | None = None
    url: str | None = None
    branch: str | None = None
    head: str | None = None


class GitHubAPI:
    """Run only core `gh api` calls with an explicit token and bounded I/O."""

    request_timeout = 10.0
    total_timeout = 180.0
    max_requests = 240
    max_response_bytes = 2 * 1024 * 1024
    max_total_bytes = 64 * 1024 * 1024
    max_request_bytes = 2 * 1024 * 1024

    def __init__(self, token: str | None = None) -> None:
        self._token = token if token is not None else os.environ.get("GH_TOKEN", "")
        if not self._token.strip():
            raise APIError("GH_TOKEN is required")
        if shutil.which("gh") is None:
            raise APIError("core gh executable is unavailable")
        self._started = time.monotonic()
        self._requests = 0
        self._response_bytes = 0

    def _endpoint(self, path: str, params: dict[str, Any] | None) -> str:
        if path == "graphql":
            if params:
                raise APIError("GraphQL parameters must be sent in the JSON body")
            return path
        if (
            not isinstance(path, str)
            or path.startswith("//")
            or "\x00" in path
            or "#" in path
            or path.split("?", 1)[0]
            not in {f"/repos/{PUBLIC_REPOSITORY}", f"/repos/{PUBLIC_REPOSITORY}/"}
            and not path.startswith(f"/repos/{PUBLIC_REPOSITORY}/")
        ):
            raise APIError("refusing a non-allowlisted GitHub API path")
        endpoint = path.lstrip("/")
        if params:
            endpoint += "?" + urlencode(params, doseq=True)
        return endpoint

    def _environment(self) -> dict[str, str]:
        denied = {
            "GH_TOKEN",
            "GITHUB_TOKEN",
            "GH_ENTERPRISE_TOKEN",
            "GITHUB_ENTERPRISE_TOKEN",
            "GH_CONFIG_DIR",
            "GH_HOST",
            "GH_PATH",
            "GH_REPO",
            "GH_DEBUG",
            "GH_TRACE",
            "GH_PROMPT_DISABLED",
        }
        env = {key: value for key, value in os.environ.items() if key not in denied}
        env.update(
            {
                "GH_HOST": "github.com",
                "GH_TOKEN": self._token,
                "GH_PROMPT_DISABLED": "1",
            }
        )
        return env

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        if method not in {"GET", "POST", "PUT"}:
            raise APIError("unsupported GitHub API method")
        endpoint = self._endpoint(path, params)
        body_bytes = None
        if body is not None:
            try:
                body_bytes = json.dumps(body, separators=(",", ":")).encode("utf-8")
            except (TypeError, ValueError):
                raise APIError(
                    "GitHub API request body is not JSON encodable"
                ) from None
            if len(body_bytes) > self.max_request_bytes:
                raise APIError("GitHub API request body exceeded its byte limit")
        self._requests += 1
        if self._requests > self.max_requests:
            raise APIError("GitHub API request limit exceeded")
        if time.monotonic() - self._started > self.total_timeout:
            raise APIError("GitHub API total time limit exceeded")

        command = [
            "gh",
            "api",
            "--hostname",
            "github.com",
            "--header",
            "Accept: application/vnd.github+json",
            "--header",
            "X-GitHub-Api-Version: 2022-11-28",
            "--method",
            method,
        ]
        if body_bytes is not None:
            command.extend(["--input", "-"])
        command.append(endpoint)
        try:
            result = self._run_bounded(command, body_bytes)
        except APIError:
            raise
        except Exception as exc:
            raise APIError(
                f"core gh api transport failed ({type(exc).__name__})"
            ) from None
        if result is None:
            raise APIError("core gh api returned no JSON response")
        self._response_bytes += len(result)
        if self._response_bytes > self.max_total_bytes:
            raise APIError("GitHub API total response-byte limit exceeded")
        try:
            return json.loads(result)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise APIError("GitHub API returned malformed JSON") from None

    def _run_bounded(self, command: list[str], body: bytes | None) -> bytes | None:
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE if body is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=self._environment(),
                shell=False,
            )
        except OSError as exc:
            raise APIError(f"cannot start core gh api ({type(exc).__name__})") from None
        assert process.stdout is not None
        selector = selectors.DefaultSelector()
        os.set_blocking(process.stdout.fileno(), False)
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        body_offset = 0
        if body is not None:
            assert process.stdin is not None
            os.set_blocking(process.stdin.fileno(), False)
            if body:
                selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
            else:
                process.stdin.close()
        output = bytearray()
        deadline = min(
            self._started + self.total_timeout, time.monotonic() + self.request_timeout
        )
        stdout_eof = False
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    process.kill()
                    process.wait()
                    raise APIError("GitHub API request exceeded its time limit")
                events = selector.select(remaining)
                if not events:
                    if time.monotonic() >= deadline and process.poll() is None:
                        process.kill()
                        process.wait()
                        raise APIError("GitHub API request exceeded its time limit")
                    if not selector.get_map() and process.poll() is None:
                        time.sleep(min(0.05, max(0, deadline - time.monotonic())))
                    continue
                for key, mask in events:
                    if key.data == "stdout" and mask & selectors.EVENT_READ:
                        chunk = os.read(
                            process.stdout.fileno(),
                            min(65536, self.max_response_bytes + 1 - len(output)),
                        )
                        if not chunk:
                            selector.unregister(process.stdout)
                            stdout_eof = True
                        else:
                            output.extend(chunk)
                            if len(output) > self.max_response_bytes:
                                process.kill()
                                process.wait()
                                raise APIError(
                                    "GitHub API response exceeded its byte limit"
                                )
                    elif key.data == "stdin" and mask & selectors.EVENT_WRITE:
                        assert process.stdin is not None and body is not None
                        try:
                            written = os.write(
                                process.stdin.fileno(),
                                body[body_offset : body_offset + 65536],
                            )
                        except BrokenPipeError:
                            written = 0
                            body_offset = len(body)
                        else:
                            body_offset += written
                        if body_offset >= len(body):
                            selector.unregister(process.stdin)
                            process.stdin.close()
                if stdout_eof and process.poll() is not None:
                    break
            return_code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
            if return_code != 0:
                raise APIError("core gh api returned a nonzero exit status")
            return bytes(output)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise APIError("GitHub API request exceeded its time limit") from None
        finally:
            selector.close()
            if process.poll() is None:
                process.kill()
                process.wait()
            if process.stdout is not None:
                process.stdout.close()
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._request("GET", path, params=params)

    def post_json(self, path: str, body: dict[str, Any]) -> Any:
        return self._request("POST", path, body=body)

    def put_json(self, path: str, body: dict[str, Any]) -> Any:
        return self._request("PUT", path, body=body)

    def graphql(
        self, query: str, variables: dict[str, Any], *, mutation: bool = False
    ) -> Any:
        response = self._request(
            "POST", "graphql", body={"query": query, "variables": variables}
        )
        if not isinstance(response, dict):
            error: Exception = APIError("malformed GraphQL response")
            if mutation:
                raise IndeterminateMutation(
                    "GraphQL mutation outcome is unknown; inspect native GitHub state"
                ) from error
            raise error
        if response.get("errors"):
            if mutation:
                raise IndeterminateMutation(
                    "GraphQL mutation outcome is unknown; inspect native GitHub state"
                )
            raise APIError("GitHub GraphQL query failed")
        return response


def _obj(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BotError(f"malformed {label}: expected object")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise BotError(f"malformed {label}: expected non-empty string")
    return value


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise BotError(f"malformed {label}: expected positive integer")
    return value


def _api_get(api: Any, path: str, params: dict[str, Any] | None = None) -> Any:
    try:
        return api.get_json(path, params)
    except BotError:
        raise
    except Exception as exc:
        raise APIError(f"GitHub API read failed ({type(exc).__name__})") from None


def _graphql(
    api: Any, query: str, variables: dict[str, Any], *, mutation: bool = False
) -> dict[str, Any]:
    try:
        response = api.graphql(query, variables, mutation=mutation)
    except IndeterminateMutation:
        raise
    except Exception as exc:
        if mutation:
            raise IndeterminateMutation(
                "GraphQL mutation outcome is unknown; inspect native GitHub state"
            ) from None
        if isinstance(exc, BotError):
            raise
        raise APIError(
            f"GitHub GraphQL request failed ({type(exc).__name__})"
        ) from None
    response = _obj(response, "GraphQL response")
    if response.get("errors"):
        if mutation:
            raise IndeterminateMutation(
                "GraphQL mutation outcome is unknown; inspect native GitHub state"
            )
        raise APIError("GitHub GraphQL query failed")
    return _obj(response.get("data"), "GraphQL data")


def _mutate(api: Any, method: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
    try:
        if method == "POST":
            response = api.post_json(path, body)
        elif method == "PUT":
            response = api.put_json(path, body)
        else:
            raise BotError("unsupported mutation method")
    except IndeterminateMutation:
        raise
    except Exception:
        raise IndeterminateMutation(
            "GitHub mutation outcome is unknown; inspect native GitHub state"
        ) from None
    try:
        return _obj(response, "GitHub mutation response")
    except BotError:
        raise IndeterminateMutation(
            "GitHub mutation response is incomplete; inspect native GitHub state"
        ) from None


def _repo_metadata(api: Any) -> dict[str, Any]:
    repo = _obj(
        _api_get(api, f"/repos/{PUBLIC_REPOSITORY}"), "public distribution repository"
    )
    if repo.get("full_name") != PUBLIC_REPOSITORY or repo.get("private") is not False:
        raise BotError("allowlisted repository identity or public visibility mismatch")
    if repo.get("archived") is True or repo.get("default_branch") != BASE_BRANCH:
        raise BotError("public distribution default branch is not active master")
    _positive_int(repo.get("id"), "public distribution repository id")
    _text(repo.get("node_id"), "public distribution GraphQL node id")
    return repo


REF_QUERY = """
query($owner: String!, $name: String!, $qualifiedName: String!) {
  repository(owner: $owner, name: $name) {
    ref(qualifiedName: $qualifiedName) {
      name
      prefix
      target {
        oid
        ... on Commit {
          signature { isValid wasSignedByGitHub }
          parents(first: 2) { nodes { oid } }
          messageBody
        }
      }
    }
  }
}
"""


def _ref(api: Any, branch: str) -> dict[str, Any] | None:
    data = _graphql(
        api,
        REF_QUERY,
        {
            "owner": "Joey-Tools",
            "name": "codex-public-distribution",
            "qualifiedName": f"refs/heads/{branch}",
        },
    )
    repository = _obj(data.get("repository"), "GraphQL repository")
    value = repository.get("ref")
    if value is None:
        return None
    ref = _obj(value, "Git reference")
    if ref.get("name") != branch or ref.get("prefix") != "refs/heads/":
        raise BotError("Git reference name mismatch")
    target = _obj(ref.get("target"), "Git reference target")
    oid = _text(target.get("oid"), "Git reference target oid")
    if not FULL_SHA_RE.fullmatch(oid):
        raise BotError("malformed Git reference target oid")
    return {
        "name": ref["name"],
        "prefix": ref["prefix"],
        "oid": oid,
        "target": target,
    }


def _master_sha(api: Any) -> str:
    value = _ref(api, BASE_BRANCH)
    if value is None:
        raise BotError("public distribution master ref is missing")
    return value["oid"]


def _bot_login(app_slug: str) -> str:
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", app_slug):
        raise BotError("invalid GitHub App slug")
    return app_slug + "[bot]"


def _parse_markers(body: Any) -> tuple[str, str] | None:
    if not isinstance(body, str) or PR_MARKER not in body:
        return None
    base_match = re.search(r"(?m)^<!-- source-base-sha:([0-9a-f]{40}) -->$", body)
    digest_match = re.search(r"(?m)^<!-- source-lock-sha256:([0-9a-f]{64}) -->$", body)
    if base_match is None or digest_match is None:
        raise BotError("managed source PR marker metadata is malformed")
    return base_match.group(1), digest_match.group(1)


def _source_pr_identity(pr: Any, app_slug: str) -> bool:
    row = _obj(pr, "pull request")
    head = row.get("head")
    base = row.get("base")
    author = row.get("user")
    if (
        not isinstance(head, dict)
        or not isinstance(base, dict)
        or not isinstance(author, dict)
    ):
        return False
    head_repo = head.get("repo")
    base_repo = base.get("repo")
    if not isinstance(head_repo, dict) or not isinstance(base_repo, dict):
        return False
    branch = head.get("ref")
    _parse_markers(row.get("body"))
    branch_ok = isinstance(branch, str) and bool(BRANCH_RE.fullmatch(branch))
    return (
        PR_MARKER in (row.get("body") if isinstance(row.get("body"), str) else "")
        and author.get("login") == _bot_login(app_slug)
        and branch_ok
        and head_repo.get("full_name") == PUBLIC_REPOSITORY
        and base_repo.get("full_name") == PUBLIC_REPOSITORY
        and base.get("ref") == BASE_BRANCH
    )


def _source_pr_shape(pr: Any, app_slug: str) -> bool:
    row = _obj(pr, "pull request")
    return (
        _source_pr_identity(row, app_slug)
        and row.get("state") == "open"
        and row.get("merged") is not True
    )


def _list_array_pages(
    api: Any, path: str, query: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    base_query = dict(query or {})
    for page in range(1, source_ci.MAX_PAGES + 1):
        params = {**base_query, "per_page": source_ci.PAGE_SIZE, "page": page}
        result = _api_get(api, path, params)
        if not isinstance(result, list):
            raise BotError("malformed paginated GitHub list response")
        rows.extend(_obj(row, "GitHub list item") for row in result)
        if len(result) < source_ci.PAGE_SIZE:
            return rows
    params = {
        **base_query,
        "per_page": source_ci.PAGE_SIZE,
        "page": source_ci.MAX_PAGES + 1,
    }
    result = _api_get(api, path, params)
    if not isinstance(result, list):
        raise BotError("malformed paginated GitHub list response")
    if result:
        raise BotError("GitHub list pagination exceeded the configured limit")
    return rows


def _pulls(api: Any, *, state: str = "open") -> list[dict[str, Any]]:
    return _list_array_pages(
        api,
        f"/repos/{PUBLIC_REPOSITORY}/pulls",
        {"state": state, "base": BASE_BRANCH},
    )


def _find_managed_prs(prs: list[dict[str, Any]], app_slug: str) -> list[dict[str, Any]]:
    return [row for row in prs if _source_pr_shape(row, app_slug)]


def _pr_number(pr: dict[str, Any]) -> int:
    return _positive_int(pr.get("number"), "pull request number")


def _git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BotError(f"local Git inspection failed ({type(exc).__name__})") from None
    if result.returncode != 0:
        raise BotError("local Git inspection failed")
    try:
        return result.stdout.decode("utf-8")
    except UnicodeDecodeError:
        raise BotError("local Git inspection returned malformed text") from None


def _stable_lock_read(path: Path) -> bytes:
    # Protect the selected file object and bytes; ignore timestamps so benign metadata changes do not fail.
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    if not getattr(os, "O_NOFOLLOW", 0):
        raise BotError("safe sources.lock.json open is unavailable")
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise BotError(
            f"cannot safely open sources.lock.json ({type(exc).__name__})"
        ) from None
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise BotError("sources.lock.json must be a regular file")
        identity = (before.st_dev, before.st_ino, stat.S_IFMT(before.st_mode))

        def read_once() -> bytes:
            os.lseek(fd, 0, os.SEEK_SET)
            result = bytearray()
            while len(result) <= 1024 * 1024:
                chunk = os.read(fd, min(65536, 1024 * 1024 + 1 - len(result)))
                if not chunk:
                    break
                result.extend(chunk)
            if len(result) > 1024 * 1024:
                raise BotError("sources.lock.json exceeds its size limit")
            return bytes(result)

        first = read_once()
        middle = os.fstat(fd)
        second = read_once()
        after = os.fstat(fd)
        try:
            path_metadata = os.stat(path, follow_symlinks=False)
        except OSError as exc:
            raise BotError(
                f"cannot recheck sources.lock.json ({type(exc).__name__})"
            ) from None
        path_identity = (
            path_metadata.st_dev,
            path_metadata.st_ino,
            stat.S_IFMT(path_metadata.st_mode),
        )
        if (
            (middle.st_dev, middle.st_ino, stat.S_IFMT(middle.st_mode)) != identity
            or (after.st_dev, after.st_ino, stat.S_IFMT(after.st_mode)) != identity
            or path_identity != identity
            or not stat.S_ISREG(path_metadata.st_mode)
            or first != second
        ):
            raise BotError(
                "sources.lock.json object or content changed while being read"
            )
        if not first:
            raise BotError("sources.lock.json is empty")
        return first
    finally:
        os.close(fd)


def _parse_source_json(payload: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=refresh_sources._json_object,
            parse_constant=refresh_sources._reject_json_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise BotError(f"{label} is not strict UTF-8 JSON") from None
    return _obj(value, label)


def _validate_lock_candidate(
    original: dict[str, Any], candidate: dict[str, Any], policy: dict[str, Any]
) -> None:
    if (
        any(
            type(document.get("version")) is not int or document["version"] != 1
            for document in (original, candidate)
        )
        or type(policy.get("version")) is not int
        or policy["version"] != 1
    ):
        raise BotError("source lock version is unsupported")
    original_sources = original.get("sources")
    candidate_sources = candidate.get("sources")
    policies = policy.get("sources")
    if (
        not isinstance(original_sources, list)
        or not isinstance(candidate_sources, list)
        or not isinstance(policies, dict)
        or not policies
    ):
        raise BotError("source lock or CI policy has an unexpected document shape")
    for value in [*original_sources, *candidate_sources]:
        _obj(value, "source lock entry")
    try:
        refresh_sources._validate_candidate_metadata(original, candidate)
        original_metadata = refresh_sources._without_mutable_pins(original)
        candidate_metadata = refresh_sources._without_mutable_pins(candidate)
        original_canonical = json.dumps(
            original_metadata, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        candidate_canonical = json.dumps(
            candidate_metadata, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except refresh_sources.RefreshError as exc:
        raise BotError(str(exc)) from None
    except (TypeError, ValueError, RecursionError):
        raise BotError("source lock metadata is not canonical JSON data") from None
    if original_canonical != candidate_canonical:
        raise BotError("candidate changed source metadata outside sha/tree")

    names: list[str] = []
    rows: list[dict[str, Any]] = []
    for value in candidate_sources:
        row = _obj(value, "source lock entry")
        name = _text(row.get("name"), "source name")
        if name in names:
            raise BotError("source lock contains duplicate source names")
        names.append(name)
        rows.append(row)
    if len(names) != len(policies) or set(names) != set(policies):
        raise BotError("source lock source inventory differs from its CI policy")
    for row in rows:
        try:
            source_ci._validate_source_pair(
                row, _obj(policies[row["name"]], f"source CI policy for {row['name']}")
            )
        except source_ci.VerificationError:
            raise BotError(f"invalid source pin metadata ({row['name']})") from None


def _local_source_lock(
    repo_root: str | os.PathLike[str], base_sha: str | None
) -> tuple[str, bytes, str]:
    root = Path(repo_root).expanduser()
    if root.is_symlink():
        raise BotError("repository root must not be a symlink")
    try:
        root = root.resolve(strict=True)
    except OSError as exc:
        raise BotError(
            f"repository root is unavailable ({type(exc).__name__})"
        ) from None
    if not root.is_dir():
        raise BotError("repository root must be a directory")
    lock_path = root / LOCK_PATH
    lock_bytes = _stable_lock_read(lock_path)
    lock_document = _parse_source_json(lock_bytes, "candidate sources.lock.json")
    head = _git(root, "rev-parse", "HEAD").strip()
    if not FULL_SHA_RE.fullmatch(head):
        raise BotError("local HEAD is not a full Git commit SHA")
    selected_base = base_sha if base_sha is not None else head
    if not FULL_SHA_RE.fullmatch(selected_base) or selected_base != head:
        raise BotError("local HEAD must equal the requested base SHA")
    original_document = _parse_source_json(
        _git(root, "show", f"{head}:{LOCK_PATH}").encode("utf-8"),
        "base sources.lock.json",
    )
    policy_document = _parse_source_json(
        _git(root, "show", f"{head}:{refresh_sources.POLICY_NAME}").encode("utf-8"),
        "base source CI policy",
    )
    _validate_lock_candidate(original_document, lock_document, policy_document)
    changed = _git(
        root,
        "diff",
        "--no-ext-diff",
        "--no-renames",
        "--name-only",
        "-z",
        selected_base,
        "--",
    )
    paths = [path for path in changed.split("\x00") if path]
    if paths != [LOCK_PATH]:
        raise BotError("only sources.lock.json may differ from the local base")
    digest = hashlib.sha256(lock_bytes).hexdigest()
    return head, lock_bytes, digest


def _base_lock_branch(head: str, digest: str) -> str:
    return f"codex/source-pins-{head[:12]}-{digest[:12]}"


def _commit_fields(
    ref: dict[str, Any], expected_parent: str | None = None
) -> dict[str, Any]:
    target = _obj(ref.get("target"), "commit target")
    signature = _obj(target.get("signature"), "commit signature")
    if (
        signature.get("isValid") is not True
        or signature.get("wasSignedByGitHub") is not True
    ):
        raise BotError("source update head lacks a valid native GitHub signature")
    parents = _obj(target.get("parents"), "commit parents").get("nodes")
    if not isinstance(parents, list) or len(parents) != 1:
        raise BotError("source update head must have exactly one known parent")
    parent = _text(_obj(parents[0], "commit parent").get("oid"), "commit parent oid")
    if not FULL_SHA_RE.fullmatch(parent):
        raise BotError("malformed source update parent SHA")
    if expected_parent is not None and parent != expected_parent:
        raise BotError("source update head does not have the expected parent")
    return {
        "oid": _text(target.get("oid"), "commit oid"),
        "parent": parent,
        "message_body": _text(target.get("messageBody"), "commit message body"),
    }


def _commit_marker(digest: str) -> str:
    return COMMIT_MARKER_PREFIX + digest


def _has_commit_marker(commit: dict[str, Any], digest: str) -> bool:
    return _commit_marker(digest) in commit["message_body"].splitlines()


def _compare(api: Any, base: str, head: str) -> dict[str, Any]:
    return _obj(
        _api_get(api, f"/repos/{PUBLIC_REPOSITORY}/compare/{base}...{head}"),
        "commit comparison",
    )


def _remote_lock_bytes(api: Any, head: str) -> bytes:
    content = _obj(
        _api_get(
            api,
            f"/repos/{PUBLIC_REPOSITORY}/contents/{LOCK_PATH}",
            {"ref": head},
        ),
        "remote sources.lock.json",
    )
    if content.get("encoding") != "base64":
        raise BotError("remote sources.lock.json is not base64 encoded")
    encoded = "".join(
        _text(content.get("content"), "remote source lock content").split()
    )
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (ValueError, base64.binascii.Error):
        raise BotError("remote sources.lock.json has malformed base64") from None
    if len(decoded) > 1024 * 1024:
        raise BotError("remote sources.lock.json exceeds its size limit")
    return decoded


def _verify_source_commit(
    api: Any,
    branch: str,
    head: str,
    parent: str,
    digest: str,
    expected_bytes: bytes | None,
) -> dict[str, Any]:
    branch_ref = _ref(api, branch)
    if branch_ref is None or branch_ref["oid"] != head:
        raise BotError("source branch changed while being verified")
    commit = _commit_fields(branch_ref, parent)
    branch_match = BRANCH_RE.fullmatch(branch)
    if (
        branch_match is None
        or branch_match.group(1) != parent[:12]
        or branch_match.group(2) != digest[:12]
        or not _has_commit_marker(commit, digest)
    ):
        raise BotError("source branch does not match its native update identity")
    comparison = _compare(api, parent, head)
    files = comparison.get("files")
    if (
        comparison.get("status") != "ahead"
        or comparison.get("ahead_by") != 1
        or comparison.get("behind_by") != 0
        or comparison.get("total_commits") != 1
        or not isinstance(files, list)
        or len(files) != 1
        or _obj(files[0], "compared file").get("filename") != LOCK_PATH
    ):
        raise BotError("source branch must contain one source-lock-only commit")
    lock_bytes = _remote_lock_bytes(api, head)
    if hashlib.sha256(lock_bytes).hexdigest() != digest:
        raise BotError("source branch lock content does not match its identity")
    if expected_bytes is not None and lock_bytes != expected_bytes:
        raise BotError(
            "source branch lock content differs from the local verified input"
        )
    return commit


CREATE_REF_MUTATION = """
mutation($input: CreateRefInput!) {
  createRef(input: $input) {
    ref { name prefix target { oid } }
  }
}
"""

CREATE_COMMIT_MUTATION = """
mutation($input: CreateCommitOnBranchInput!) {
  createCommitOnBranch(input: $input) {
    commit {
      oid
      signature { isValid wasSignedByGitHub }
      parents(first: 2) { nodes { oid } }
      messageBody
    }
  }
}
"""


def _create_source_ref(
    api: Any, branch: str, base: str, repository_node_id: str
) -> None:
    data = _graphql(
        api,
        CREATE_REF_MUTATION,
        {
            "input": {
                "repositoryId": repository_node_id,
                "name": f"refs/heads/{branch}",
                "oid": base,
            }
        },
        mutation=True,
    )
    try:
        result = _obj(data.get("createRef"), "createRef result")
        ref = _obj(result.get("ref"), "created source ref")
        target = _obj(ref.get("target"), "created source ref target")
    except BotError:
        raise IndeterminateMutation(
            "created source ref response is incomplete; inspect native GitHub state"
        ) from None
    if (
        ref.get("name") != branch
        or ref.get("prefix") != "refs/heads/"
        or target.get("oid") != base
    ):
        raise IndeterminateMutation(
            "created source ref could not be verified; inspect native GitHub state"
        )


def _create_source_commit(
    api: Any, branch: str, base: str, lock: bytes, digest: str
) -> str:
    contents = base64.b64encode(lock).decode("ascii")
    data = _graphql(
        api,
        CREATE_COMMIT_MUTATION,
        {
            "input": {
                "branch": {
                    "repositoryNameWithOwner": PUBLIC_REPOSITORY,
                    "branchName": branch,
                },
                "expectedHeadOid": base,
                "message": {
                    "headline": "Update verified public source pins",
                    "body": _commit_marker(digest),
                },
                "fileChanges": {
                    "additions": [{"path": LOCK_PATH, "contents": contents}]
                },
            }
        },
        mutation=True,
    )
    try:
        result = _obj(data.get("createCommitOnBranch"), "createCommitOnBranch result")
        commit = _obj(result.get("commit"), "created source commit")
        oid = _text(commit.get("oid"), "created commit oid")
    except BotError:
        raise IndeterminateMutation(
            "created source commit response is incomplete; inspect native GitHub state"
        ) from None
    if not FULL_SHA_RE.fullmatch(oid):
        raise IndeterminateMutation(
            "created commit identity is incomplete; inspect native GitHub state"
        )
    return oid


def _pr_by_branch(prs: list[dict[str, Any]], branch: str) -> list[dict[str, Any]]:
    selected = []
    for pr in prs:
        head = pr.get("head")
        base = pr.get("base")
        if not isinstance(head, dict) or not isinstance(base, dict):
            continue
        head_repo = head.get("repo")
        base_repo = base.get("repo")
        if (
            head.get("ref") == branch
            and isinstance(head_repo, dict)
            and head_repo.get("full_name") == PUBLIC_REPOSITORY
            and isinstance(base_repo, dict)
            and base_repo.get("full_name") == PUBLIC_REPOSITORY
            and base.get("ref") == BASE_BRANCH
        ):
            selected.append(pr)
    return selected


def _known_pr_for_branch(
    prs: list[dict[str, Any]], branch: str, app_slug: str
) -> Result | None:
    rows = _pr_by_branch(prs, branch)
    if len(rows) > 1:
        raise BotError("multiple pull requests use the deterministic source branch")
    if not rows:
        return None
    row = rows[0]
    if not _source_pr_identity(row, app_slug):
        raise BotError("the deterministic source branch already has an unmanaged PR")
    if row.get("merged") is True:
        return Result(
            "already_merged",
            pull_request=_pr_number(row),
            url=_text(row.get("html_url"), "pull request URL"),
            branch=branch,
        )
    if row.get("state") == "closed":
        return Result(
            "already_closed",
            pull_request=_pr_number(row),
            url=_text(row.get("html_url"), "pull request URL"),
            branch=branch,
        )
    if row.get("state") != "open":
        raise BotError("deterministic source pull request has an unknown state")
    return Result(
        "coalesced",
        pull_request=_pr_number(row),
        url=_text(row.get("html_url"), "pull request URL"),
        branch=branch,
    )


def _pr_body(base: str, digest: str) -> str:
    return "\n".join(
        [
            PR_MARKER,
            f"{BASE_MARKER_PREFIX}{base} -->",
            f"{LOCK_MARKER_PREFIX}{digest} -->",
            "Automated update from locally verified native source CI evidence.",
        ]
    )


def open_source_update(
    repo_root: str | os.PathLike[str],
    app_slug: str,
    api: Any,
    base_sha: str | None = None,
) -> Result:
    _bot_login(app_slug)
    base, lock_bytes, digest = _local_source_lock(repo_root, base_sha)
    branch = _base_lock_branch(base, digest)
    repo = _repo_metadata(api)
    remote_base = _master_sha(api)
    if remote_base != base:
        raise BotError(
            "public master changed; refresh the local source base before opening"
        )

    prs = _pulls(api, state="all")
    exact_pr = _known_pr_for_branch(prs, branch, app_slug)
    if exact_pr is not None:
        return exact_pr
    managed = _find_managed_prs(prs, app_slug)
    if len(managed) > 1:
        raise BotError(
            "multiple managed source PRs are open; refusing ambiguous ownership"
        )
    if managed:
        row = managed[0]
        head = _obj(row.get("head"), "managed pull request head")
        return Result(
            "coalesced",
            pull_request=_pr_number(row),
            url=_text(row.get("html_url"), "pull request URL"),
            branch=_text(head.get("ref"), "managed pull request branch"),
        )
    branch_ref = _ref(api, branch)
    if branch_ref is None:
        _create_source_ref(api, branch, base, repo["node_id"])
        branch_ref = _ref(api, branch)
        if branch_ref is None or branch_ref["oid"] != base:
            raise IndeterminateMutation(
                "created branch ref is not visible; inspect native GitHub state"
            )
    if branch_ref["oid"] == base:
        commit_oid = _create_source_commit(api, branch, base, lock_bytes, digest)
        branch_ref = _ref(api, branch)
        if branch_ref is None or branch_ref["oid"] != commit_oid:
            raise IndeterminateMutation(
                "created source commit is not visible at the branch; inspect native GitHub state"
            )

    head = branch_ref["oid"]
    _verify_source_commit(api, branch, head, base, digest, lock_bytes)
    if _master_sha(api) != base:
        raise BotError("public master changed during source PR preparation")

    prs = _pulls(api, state="all")
    exact_pr = _known_pr_for_branch(prs, branch, app_slug)
    if exact_pr is not None:
        return exact_pr
    managed = _find_managed_prs(prs, app_slug)
    if len(managed) > 1:
        raise BotError("multiple managed source PRs appeared during preparation")
    if managed:
        row = managed[0]
        head_data = _obj(row.get("head"), "managed pull request head")
        return Result(
            "coalesced",
            pull_request=_pr_number(row),
            url=_text(row.get("html_url"), "pull request URL"),
            branch=_text(head_data.get("ref"), "managed pull request branch"),
        )
    try:
        created = _mutate(
            api,
            "POST",
            f"/repos/{PUBLIC_REPOSITORY}/pulls",
            {
                "title": "Update verified public source pins",
                "head": branch,
                "base": BASE_BRANCH,
                "body": _pr_body(base, digest),
            },
        )
    except IndeterminateMutation:
        observed = _pulls(api, state="all")
        exact_pr = _known_pr_for_branch(observed, branch, app_slug)
        if exact_pr is not None:
            return exact_pr
        raise
    if (
        not _source_pr_shape(created, app_slug)
        or _pr_number(created) < 1
        or created.get("head", {}).get("ref") != branch
        or created.get("base", {}).get("ref") != BASE_BRANCH
    ):
        raise IndeterminateMutation(
            "created pull request identity is incomplete; inspect native GitHub state"
        )
    return Result(
        "opened",
        pull_request=_pr_number(created),
        url=_text(created.get("html_url"), "pull request URL"),
        branch=branch,
        head=head,
    )


def _workflow(api: Any) -> dict[str, Any]:
    workflow = _obj(
        _api_get(
            api,
            f"/repos/{PUBLIC_REPOSITORY}/actions/workflows/ci.yml",
        ),
        "Distribution workflow",
    )
    if workflow.get("path") != WORKFLOW_PATH or workflow.get("state") != "active":
        raise BotError("native Distribution workflow path or state mismatch")
    _positive_int(workflow.get("id"), "Distribution workflow id")
    return workflow


def _pr_snapshot(api: Any, number: int) -> dict[str, Any]:
    pr = _obj(
        _api_get(api, f"/repos/{PUBLIC_REPOSITORY}/pulls/{number}"),
        "source pull request",
    )
    if _pr_number(pr) != number:
        raise BotError("pull request number changed")
    return pr


def _pr_signature(pr: dict[str, Any]) -> tuple[Any, ...]:
    base = _obj(pr.get("base"), "pull request base")
    head = _obj(pr.get("head"), "pull request head")
    base_repo = _obj(base.get("repo"), "pull request base repository")
    head_repo = _obj(head.get("repo"), "pull request head repository")
    user = _obj(pr.get("user"), "pull request author")
    return (
        pr.get("number"),
        pr.get("state"),
        pr.get("merged"),
        user.get("login"),
        base_repo.get("full_name"),
        base.get("ref"),
        base.get("sha"),
        head_repo.get("full_name"),
        head.get("ref"),
        head.get("sha"),
        pr.get("mergeable"),
        pr.get("mergeable_state"),
        pr.get("body"),
    )


def _valid_open_managed_pr(pr: dict[str, Any], app_slug: str) -> bool:
    return _source_pr_shape(pr, app_slug)


def _pr_files(api: Any, number: int) -> list[dict[str, Any]]:
    return _list_array_pages(
        api,
        f"/repos/{PUBLIC_REPOSITORY}/pulls/{number}/files",
    )


def _files_signature(files: list[dict[str, Any]]) -> tuple[Any, ...]:
    return tuple(
        (
            row.get("filename"),
            row.get("status"),
            row.get("additions"),
            row.get("deletions"),
            row.get("changes"),
            row.get("sha"),
        )
        for row in files
    )


def _review_threads(api: Any, number: int) -> tuple[tuple[str, bool], ...]:
    query = """
    query($owner: String!, $name: String!, $number: Int!, $after: String) {
      repository(owner: $owner, name: $name) {
        pullRequest(number: $number) {
          reviewThreads(first: 100, after: $after) {
            nodes { id isResolved }
            pageInfo { hasNextPage endCursor }
          }
        }
      }
    }
    """
    cursor: str | None = None
    rows: list[tuple[str, bool]] = []
    for _page in range(source_ci.MAX_PAGES):
        data = _graphql(
            api,
            query,
            {
                "owner": "Joey-Tools",
                "name": "codex-public-distribution",
                "number": number,
                "after": cursor,
            },
        )
        repository = _obj(data.get("repository"), "review-thread repository")
        pr = _obj(repository.get("pullRequest"), "review-thread pull request")
        connection = _obj(pr.get("reviewThreads"), "review-thread connection")
        nodes = connection.get("nodes")
        page_info = _obj(connection.get("pageInfo"), "review-thread page info")
        if not isinstance(nodes, list):
            raise BotError("malformed review-thread page")
        for value in nodes:
            row = _obj(value, "review thread")
            identity = _text(row.get("id"), "review-thread id")
            resolved = row.get("isResolved")
            if not isinstance(resolved, bool):
                raise BotError("malformed review-thread resolution state")
            rows.append((identity, resolved))
        has_next = page_info.get("hasNextPage")
        if not isinstance(has_next, bool):
            raise BotError("malformed review-thread page state")
        if not has_next:
            return tuple(rows)
        next_cursor = _text(page_info.get("endCursor"), "review-thread cursor")
        if next_cursor == cursor:
            raise BotError("review-thread cursor did not advance")
        cursor = next_cursor
    raise BotError("review-thread pagination exceeded the configured limit")


def _run_matches(
    run: dict[str, Any],
    workflow_id: int,
    head: str,
    branch: str,
    repository_id: int,
) -> bool:
    source_ci._run_signature(run)
    if not (
        run.get("workflow_id") == workflow_id
        and run.get("head_sha") == head
        and run.get("head_branch") == branch
        and run.get("event") == "pull_request"
    ):
        return False
    for key in ("repository", "head_repository"):
        repository = _obj(run.get(key), f"workflow run {key}")
        if (
            repository.get("full_name") != PUBLIC_REPOSITORY
            or repository.get("id") != repository_id
        ):
            return False
    return True


def _latest_matching_run(
    rows: list[dict[str, Any]],
    workflow_id: int,
    head: str,
    branch: str,
    repository_id: int,
) -> dict[str, Any] | None:
    matches = [
        row
        for row in rows
        if _run_matches(row, workflow_id, head, branch, repository_id)
    ]
    if not matches:
        return None
    return max(
        matches,
        key=lambda row: (
            source_ci._iso(row["updated_at"], "workflow run updated_at")[0],
            source_ci._iso(row["created_at"], "workflow run created_at")[0],
            row["id"],
            row["run_attempt"],
        ),
    )


class NotReady(BotError):
    def __init__(self, reason: str, *, failed: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.failed = failed


def _workflow_runs(
    api: Any, workflow_id: int, head: str, branch: str
) -> list[dict[str, Any]]:
    try:
        return source_ci._pages(
            api,
            PUBLIC_REPOSITORY,
            f"/actions/workflows/{workflow_id}/runs",
            "workflow_runs",
            {"event": "pull_request", "head_sha": head, "branch": branch},
        )
    except source_ci.VerificationError as exc:
        raise BotError(str(exc)) from None


def _workflow_jobs(api: Any, run_id: int, attempt: int) -> list[dict[str, Any]]:
    try:
        return source_ci._pages(
            api,
            PUBLIC_REPOSITORY,
            f"/actions/runs/{run_id}/attempts/{attempt}/jobs",
            "jobs",
        )
    except source_ci.VerificationError as exc:
        raise BotError(str(exc)) from None


def _validate_distribution_jobs(
    rows: list[dict[str, Any]], run: dict[str, Any]
) -> tuple[Any, ...]:
    names: dict[str, dict[str, Any]] = {}
    for job in rows:
        if (
            job.get("run_id") != run["id"]
            or job.get("run_attempt") != run["run_attempt"]
            or job.get("head_sha") != run["head_sha"]
        ):
            raise BotError(
                "job inventory does not match the selected run attempt and head"
            )
        name = _text(job.get("name"), "workflow job name")
        if name in names:
            raise BotError("duplicate Distribution workflow job name")
        if name not in WORKFLOW_JOBS:
            raise BotError(f"unexpected Distribution workflow job: {name}")
        names[name] = job
    missing = sorted(set(WORKFLOW_JOBS) - set(names))
    if missing:
        raise BotError("Distribution workflow job inventory is incomplete")
    for name, expected in WORKFLOW_JOBS.items():
        row = names[name]
        if row.get("status") != "completed":
            raise NotReady("ci_job_pending")
        if row.get("conclusion") != expected:
            raise NotReady("ci_job_failed_or_unexpected", failed=True)
    return tuple(source_ci._job_signature(row) for row in rows)


@dataclass(frozen=True)
class CISnapshot:
    run_signature: tuple[Any, ...]
    attempt_signature: tuple[Any, ...]
    jobs_signature: tuple[Any, ...]
    run_id: int
    attempt: int


def _ci_snapshot(
    api: Any, workflow_id: int, head: str, branch: str, repository_id: int
) -> CISnapshot:
    rows = _workflow_runs(api, workflow_id, head, branch)
    run = _latest_matching_run(rows, workflow_id, head, branch, repository_id)
    if run is None:
        raise NotReady("ci_run_missing")
    if run.get("status") != "completed":
        raise NotReady("ci_run_pending")
    if run.get("conclusion") != "success":
        raise NotReady("ci_run_failed_or_cancelled", failed=True)
    run_signature = source_ci._run_signature(run)
    run_id = _positive_int(run.get("id"), "workflow run id")
    attempt_number = _positive_int(run.get("run_attempt"), "workflow run attempt")
    attempt = _obj(
        _api_get(
            api,
            f"/repos/{PUBLIC_REPOSITORY}/actions/runs/{run_id}/attempts/{attempt_number}",
        ),
        "current workflow attempt",
    )
    if source_ci._run_identity_status_signature(
        attempt
    ) != source_ci._run_identity_status_signature(run):
        raise NotReady("ci_attempt_changed")
    attempt_signature = source_ci._run_signature(attempt)
    jobs = _workflow_jobs(api, run_id, attempt_number)
    jobs_signature = _validate_distribution_jobs(jobs, run)
    repository_signature = tuple(
        (
            key,
            _obj(run.get(key), f"workflow run {key}").get("id"),
            _obj(run.get(key), f"workflow run {key}").get("full_name"),
        )
        for key in ("repository", "head_repository")
    )
    return CISnapshot(
        run_signature + repository_signature,
        attempt_signature,
        jobs_signature,
        run_id,
        attempt_number,
    )


def _check_signature(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        row.get("id"),
        row.get("name"),
        _obj(row.get("app"), "check-run app").get("id") if row.get("app") else None,
        row.get("head_sha"),
        row.get("status"),
        row.get("conclusion"),
        row.get("started_at"),
        row.get("completed_at"),
        row.get("check_suite", {}).get("id")
        if isinstance(row.get("check_suite"), dict)
        else None,
    )


def _status_signature(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        row.get("id"),
        row.get("url"),
        row.get("context"),
        row.get("state"),
        row.get("created_at"),
        row.get("updated_at"),
    )


def _check_status_snapshot(api: Any, head: str) -> tuple[Any, ...]:
    try:
        check_runs = source_ci._pages(
            api,
            PUBLIC_REPOSITORY,
            f"/commits/{head}/check-runs",
            "check_runs",
            {"filter": "latest"},
        )
        statuses = _list_array_pages(
            api,
            f"/repos/{PUBLIC_REPOSITORY}/commits/{head}/statuses",
        )
    except source_ci.VerificationError as exc:
        raise BotError(str(exc)) from None
    seen_check_ids: set[int] = set()
    for check in check_runs:
        check_id = _positive_int(check.get("id"), "check-run id")
        if check_id in seen_check_ids:
            raise BotError("duplicate current check-run identity")
        seen_check_ids.add(check_id)
        if check.get("head_sha") != head:
            raise BotError("check run does not match the exact source PR head")
        if check.get("status") != "completed":
            raise NotReady("check_run_pending")
        if check.get("conclusion") not in {"success", "skipped", "neutral"}:
            raise NotReady("check_run_failed_or_unknown", failed=True)
    latest_status_by_context: dict[str, dict[str, Any]] = {}
    for status in statuses:
        context = _text(status.get("context"), "commit status context")
        context_key = context.casefold()
        if context_key not in latest_status_by_context:
            latest_status_by_context[context_key] = status
    for status in latest_status_by_context.values():
        if status.get("state") != "success":
            raise NotReady(
                "commit_status_pending_or_failed",
                failed=status.get("state") not in {"pending"},
            )
    combined = _obj(
        _api_get(api, f"/repos/{PUBLIC_REPOSITORY}/commits/{head}/status"),
        "combined commit status",
    )
    if combined.get("sha") != head:
        raise BotError("combined commit status does not match the exact source PR head")
    total_count = combined.get("total_count")
    if (
        isinstance(total_count, bool)
        or not isinstance(total_count, int)
        or total_count < 0
    ):
        raise BotError("malformed combined commit status count")
    if total_count and combined.get("state") != "success":
        raise NotReady(
            "combined_commit_status_pending_or_failed",
            failed=combined.get("state") not in {"pending"},
        )
    return (
        tuple(_check_signature(row) for row in check_runs),
        tuple(_status_signature(row) for row in statuses),
        (combined.get("state"), total_count),
    )


def _compare_parent_to_base(api: Any, parent: str, base: str) -> None:
    comparison = _compare(api, parent, base)
    if comparison.get("behind_by") != 0 or comparison.get("status") not in {
        "ahead",
        "identical",
    }:
        raise BotError("source PR parent is not an ancestor of current public master")


def _validate_pr_identity(
    pr: dict[str, Any], app_slug: str
) -> tuple[str, str, str, str]:
    if not _valid_open_managed_pr(pr, app_slug):
        raise BotError("pull request is not owned by this source-update App")
    markers = _parse_markers(pr.get("body"))
    if markers is None:
        raise BotError("managed source PR markers are missing")
    source_base, digest = markers
    base = _obj(pr.get("base"), "pull request base")
    head = _obj(pr.get("head"), "pull request head")
    head_sha = _text(head.get("sha"), "pull request head SHA")
    base_sha = _text(base.get("sha"), "pull request base SHA")
    if not FULL_SHA_RE.fullmatch(head_sha) or not FULL_SHA_RE.fullmatch(base_sha):
        raise BotError("pull request contains a malformed base or head SHA")
    branch = _text(head.get("ref"), "pull request source branch")
    branch_match = BRANCH_RE.fullmatch(branch)
    if branch_match is None:
        raise BotError("source PR branch is outside the automation namespace")
    if (
        branch_match.group(1) != source_base[:12]
        or branch_match.group(2) != digest[:12]
    ):
        raise BotError("source PR branch and machine markers disagree")
    return branch, head_sha, base_sha, digest


def _check_pr_mergeable(pr: dict[str, Any]) -> None:
    if pr.get("state") != "open" or pr.get("merged") is True:
        raise BotError("source PR is no longer open")
    if pr.get("mergeable") is not True or pr.get("mergeable_state") != "clean":
        raise NotReady("pull_request_not_currently_mergeable")


def _merge_candidate(prs: list[dict[str, Any]], app_slug: str) -> dict[str, Any] | None:
    managed = _find_managed_prs(prs, app_slug)
    if len(managed) > 1:
        raise BotError(
            "multiple managed source PRs are open; refusing ambiguous ownership"
        )
    return managed[0] if managed else None


def merge_ready_source_update(app_slug: str, api: Any) -> Result:
    _bot_login(app_slug)
    repo = _repo_metadata(api)
    candidate = _merge_candidate(_pulls(api), app_slug)
    if candidate is None:
        return Result("no_candidate")
    number = _pr_number(candidate)
    initial = _pr_snapshot(api, number)
    branch, head_sha, base_sha, digest = _validate_pr_identity(initial, app_slug)
    _check_pr_mergeable(initial)
    master = _master_sha(api)
    if master != base_sha:
        raise NotReady("public_master_changed_during_source_pr")

    files_first = _pr_files(api, number)
    if len(files_first) != 1 or files_first[0].get("filename") != LOCK_PATH:
        raise BotError("source PR diff must contain only sources.lock.json")

    branch_ref = _ref(api, branch)
    if branch_ref is None or branch_ref["oid"] != head_sha:
        raise BotError("source PR head does not match its native branch ref")
    commit = _commit_fields(branch_ref)
    if commit["parent"] != _parse_markers(initial.get("body"))[0]:
        raise BotError("source PR head parent differs from its marker")
    if not _has_commit_marker(commit, digest):
        raise BotError("source PR head lacks the expected native commit identity")
    branch_match = BRANCH_RE.fullmatch(branch)
    if branch_match is None or branch_match.group(1) != commit["parent"][:12]:
        raise BotError("source PR head is not the expected single source update")
    _compare_parent_to_base(api, commit["parent"], base_sha)
    remote_lock = _remote_lock_bytes(api, head_sha)
    if hashlib.sha256(remote_lock).hexdigest() != digest:
        raise BotError("source PR lock content does not match its machine marker")

    workflow = _workflow(api)
    workflow_id = _positive_int(workflow.get("id"), "Distribution workflow id")
    ci_first = _ci_snapshot(api, workflow_id, head_sha, branch, repo["id"])
    threads_first = _review_threads(api, number)
    if any(not resolved for _, resolved in threads_first):
        raise NotReady("unresolved_review_threads")
    checks_first = _check_status_snapshot(api, head_sha)

    current = _pr_snapshot(api, number)
    _check_pr_mergeable(current)
    if _pr_signature(current) != _pr_signature(initial):
        raise NotReady("source_pr_head_or_base_changed")
    if _master_sha(api) != base_sha:
        raise NotReady("public_master_changed_during_source_pr")
    files_second = _pr_files(api, number)
    if _files_signature(files_first) != _files_signature(files_second):
        raise NotReady("source_pr_diff_changed_during_verification")
    branch_second = _ref(api, branch)
    if branch_second is None or branch_second["oid"] != head_sha:
        raise NotReady("source_pr_head_changed_during_verification")
    commit_second = _commit_fields(branch_second)
    if commit_second != commit:
        raise NotReady("source_pr_commit_changed_during_verification")
    ci_second = _ci_snapshot(api, workflow_id, head_sha, branch, repo["id"])
    if ci_second != ci_first:
        raise NotReady("native_distribution_ci_changed_during_verification")
    threads_second = _review_threads(api, number)
    if threads_second != threads_first or any(
        not resolved for _, resolved in threads_second
    ):
        raise NotReady("review_threads_changed_during_verification")
    checks_second = _check_status_snapshot(api, head_sha)
    if checks_second != checks_first:
        raise NotReady("check_or_status_changed_during_verification")
    final = _pr_snapshot(api, number)
    _check_pr_mergeable(final)
    if _pr_signature(final) != _pr_signature(initial):
        raise NotReady("source_pr_head_or_base_changed_before_merge")
    if _master_sha(api) != base_sha:
        raise NotReady("public_master_changed_before_merge")

    merged = _mutate(
        api,
        "PUT",
        f"/repos/{PUBLIC_REPOSITORY}/pulls/{number}/merge",
        {"sha": head_sha, "merge_method": "squash"},
    )
    if merged.get("merged") is not True or not FULL_SHA_RE.fullmatch(
        str(merged.get("sha", ""))
    ):
        raise IndeterminateMutation(
            "merge response is incomplete; inspect native GitHub state"
        )
    return Result(
        "merged", pull_request=number, url=initial.get("html_url"), head=head_sha
    )


def _print_result(result: Result) -> None:
    fields = [f"status={result.status}"]
    if result.reason:
        fields.append(f"reason={result.reason}")
    if result.pull_request is not None:
        fields.append(f"pull_request={result.pull_request}")
    if result.url:
        fields.append(f"pull_request_url={result.url}")
    if result.branch:
        fields.append(f"branch={result.branch}")
    if result.head:
        fields.append(f"head={result.head}")
    print(" ".join(fields))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    open_parser = subparsers.add_parser("open", help="open or reuse one source-pin PR")
    open_parser.add_argument("--repo-root", default=".")
    open_parser.add_argument("--base-sha")
    open_parser.add_argument("--app-slug", required=True)
    merge_parser = subparsers.add_parser(
        "merge-ready", help="merge one exact-head CI-ready source-pin PR"
    )
    merge_parser.add_argument("--repo-root", default=".")
    merge_parser.add_argument("--app-slug", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        api = GitHubAPI()
        if args.command == "open":
            result = open_source_update(
                args.repo_root, args.app_slug, api, base_sha=args.base_sha
            )
        else:
            result = merge_ready_source_update(args.app_slug, api)
        _print_result(result)
        return 0
    except NotReady as exc:
        _print_result(Result("blocked" if exc.failed else "pending", reason=exc.reason))
        return 0
    except (BotError, source_ci.VerificationError) as exc:
        print(
            f"error={type(exc).__name__} reason={str(exc).replace(' ', '_')}",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:
        print(f"error=unexpected reason={type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
