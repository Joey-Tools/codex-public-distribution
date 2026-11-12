#!/usr/bin/env python3
"""Verify pinned public sources against native GitHub Actions evidence."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


API_ROOT = "https://api.github.com"
PAGE_SIZE = 100
MAX_PAGES = 20
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_REQUESTS = 800
REQUEST_TIMEOUT = 10
TOTAL_TIMEOUT = 180


class VerificationError(Exception):
    """A source could not be established as having exact native CI proof."""


class APIError(VerificationError):
    """GitHub API transport or response error, kept distinct from absence."""


class _SameHostRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> Request | None:
        if urlsplit(newurl).hostname != "api.github.com":
            raise APIError("GitHub API redirect left api.github.com")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class GitHubAPI:
    """Small bounded urllib client; its get_json method is injectable in tests."""

    def __init__(self, token: str | None = None) -> None:
        self._token = token
        self._opener = build_opener(_SameHostRedirect())
        self._started = time.monotonic()
        self._requests = 0
        self._bytes = 0

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if not path.startswith("/repos/") or path.startswith("//"):
            raise APIError("refusing a non-repository API path")
        self._requests += 1
        if self._requests > MAX_REQUESTS:
            raise APIError("GitHub API request limit exceeded")
        if time.monotonic() - self._started > TOTAL_TIMEOUT:
            raise APIError("GitHub API total time limit exceeded")
        url = API_ROOT + path
        if params:
            url += "?" + urlencode(params)
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "codex-source-ci-verifier",
        }
        if self._token:
            headers["Authorization"] = "Bearer " + self._token
        request = Request(url, headers=headers)
        try:
            with self._opener.open(request, timeout=REQUEST_TIMEOUT) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            raise APIError(f"GitHub API returned HTTP {exc.code} for {path}") from None
        except (URLError, TimeoutError, OSError) as exc:
            # Do not include exception strings: urllib errors may contain request URLs.
            raise APIError(
                f"GitHub API transport failed for {path} ({type(exc).__name__})"
            ) from None
        if len(body) > MAX_RESPONSE_BYTES:
            raise APIError(f"GitHub API response exceeded {MAX_RESPONSE_BYTES} bytes")
        self._bytes += len(body)
        if self._bytes > MAX_TOTAL_BYTES:
            raise APIError("GitHub API total response-byte limit exceeded")
        try:
            return json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise APIError(f"GitHub API returned malformed JSON for {path}") from None


def _read_json(path: str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerificationError(
            f"cannot read JSON input {path}: {type(exc).__name__}"
        ) from None


def _obj(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise VerificationError(f"malformed {what}: expected object")
    return value


def _nonempty(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise VerificationError(f"malformed {what}: expected non-empty string")
    return value


def _api_path(repository: str, suffix: str) -> str:
    owner, name = repository.split("/", 1)
    return f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}{suffix}"


def _get(
    api: Any, repository: str, suffix: str, params: dict[str, Any] | None = None
) -> Any:
    try:
        return api.get_json(_api_path(repository, suffix), params)
    except VerificationError:
        raise
    except Exception as exc:
        raise APIError(
            f"injected GitHub API client failed ({type(exc).__name__})"
        ) from None


def _pages(
    api: Any,
    repository: str,
    suffix: str,
    collection: str,
    query: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for page in range(1, MAX_PAGES + 1):
        params = {"per_page": PAGE_SIZE, "page": page}
        params.update(query or {})
        result = _get(api, repository, suffix, params)
        if not isinstance(result, dict) or not isinstance(result.get(collection), list):
            raise VerificationError(f"malformed {collection} pagination response")
        batch = result[collection]
        rows.extend(
            _obj(
                row, "workflow run" if collection == "workflow_runs" else "workflow job"
            )
            for row in batch
        )
        if len(batch) < PAGE_SIZE:
            return rows
    # A full final page cannot establish completeness without probing the next page.
    params = {"per_page": PAGE_SIZE, "page": MAX_PAGES + 1}
    params.update(query or {})
    result = _get(api, repository, suffix, params)
    if not isinstance(result, dict) or not isinstance(result.get(collection), list):
        raise VerificationError(f"malformed {collection} pagination response")
    if result[collection]:
        raise VerificationError(
            f"{collection} pagination exceeded the configured limit"
        )
    return rows


def _iso(value: Any, what: str) -> tuple[datetime, str]:
    raw = _nonempty(value, what)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed, raw
    except ValueError:
        raise VerificationError(
            f"malformed {what}: expected timezone-aware timestamp"
        ) from None


def _run_signature(run: dict[str, Any]) -> tuple[Any, ...]:
    required = (
        "id",
        "workflow_id",
        "head_sha",
        "head_branch",
        "event",
        "status",
        "conclusion",
        "run_attempt",
        "created_at",
        "updated_at",
    )
    if any(key not in run for key in required):
        raise VerificationError(
            "malformed workflow run: missing identity or activity field"
        )
    created, _ = _iso(run["created_at"], "workflow run created_at")
    updated, _ = _iso(run["updated_at"], "workflow run updated_at")
    for field in ("id", "workflow_id", "run_attempt"):
        if (
            isinstance(run[field], bool)
            or not isinstance(run[field], int)
            or run[field] < 1
        ):
            raise VerificationError(f"malformed workflow run: invalid {field}")
    for field in ("head_sha", "head_branch", "event", "status"):
        _nonempty(run[field], f"workflow run {field}")
    if run["conclusion"] is not None and not isinstance(run["conclusion"], str):
        raise VerificationError("malformed workflow run: invalid conclusion")
    return (
        run["id"],
        run["workflow_id"],
        run["head_sha"],
        run["head_branch"],
        run["event"],
        run["status"],
        run["conclusion"],
        run["run_attempt"],
        created.isoformat(),
        updated.isoformat(),
    )


def _run_identity_status_signature(run: dict[str, Any]) -> tuple[Any, ...]:
    """Compare run identity/state across endpoints with independent update clocks."""
    signature = _run_signature(run)
    return signature[:-1]


def _matches(run: dict[str, Any], source: dict[str, Any]) -> bool:
    _run_signature(run)
    return (
        run["workflow_id"] == source["workflow"]["id"]
        and run["head_sha"] == source["sha"]
        and run["head_branch"] == source["ref"]
        and run["event"] == "push"
    )


def _latest_matching(
    rows: list[dict[str, Any]], source: dict[str, Any]
) -> dict[str, Any]:
    matches = [row for row in rows if _matches(row, source)]
    if not matches:
        raise VerificationError(
            "no native push workflow run exists for the locked SHA and ref"
        )
    for row in matches:
        # When supplied, the API's repository identity must agree with the source lock.
        repository = row.get("repository")
        if (
            "repository" in row
            and repository is not None
            and not isinstance(repository, dict)
        ):
            raise VerificationError("malformed workflow run repository identity")
        if isinstance(repository, dict):
            if (
                repository.get("id") != source["repository_id"]
                or repository.get("full_name") != source["repository"]
            ):
                raise VerificationError(
                    "workflow run repository identity does not match the source lock"
                )
        head_repository = row.get("head_repository")
        if (
            "head_repository" in row
            and head_repository is not None
            and not isinstance(head_repository, dict)
        ):
            raise VerificationError("malformed workflow run head repository identity")
        if isinstance(head_repository, dict):
            if (
                head_repository.get("id") != source["repository_id"]
                or head_repository.get("full_name") != source["repository"]
            ):
                raise VerificationError(
                    "workflow run head repository does not match the source lock"
                )
    return max(
        matches,
        key=lambda row: (
            _iso(row["updated_at"], "workflow run updated_at")[0],
            _iso(row["created_at"], "workflow run created_at")[0],
            row["id"],
            row["run_attempt"],
        ),
    )


def _validate_job_rows(
    rows: list[dict[str, Any]], run: dict[str, Any], policy: dict[str, Any]
) -> dict[str, Any]:
    required = policy["required_jobs"]
    allowed_skipped = policy["allowed_skipped_jobs"]
    universe = set(required) | set(allowed_skipped)
    by_name: dict[str, dict[str, Any]] = {}
    for job in rows:
        if (
            job.get("run_id") != run["id"]
            or job.get("run_attempt") != run["run_attempt"]
        ):
            raise VerificationError(
                "job run identity does not match the selected run attempt"
            )
        if job.get("head_sha") != run["head_sha"]:
            raise VerificationError(
                "job head SHA does not match the selected workflow run"
            )
        name = _nonempty(job.get("name"), "workflow job name")
        if name in by_name:
            raise VerificationError(f"duplicate workflow job name: {name}")
        if name not in universe:
            raise VerificationError(f"unexpected workflow job: {name}")
        by_name[name] = job
    missing = [name for name in required if name not in by_name]
    if missing:
        raise VerificationError(
            "missing required workflow job(s): " + ", ".join(missing)
        )
    missing_allowed = [name for name in allowed_skipped if name not in by_name]
    if missing_allowed:
        raise VerificationError(
            "missing configured optional workflow job(s): " + ", ".join(missing_allowed)
        )
    for name, job in by_name.items():
        status, conclusion = job.get("status"), job.get("conclusion")
        if status != "completed":
            raise VerificationError(f"workflow job is not completed: {name} ({status})")
        if conclusion == "success":
            continue
        if conclusion == "skipped" and name in allowed_skipped:
            continue
        raise VerificationError(f"workflow job did not succeed: {name} ({conclusion})")

    required_steps = policy.get("required_steps", {})
    if not isinstance(required_steps, dict):
        raise VerificationError("malformed policy required_steps")
    step_evidence: dict[str, list[str]] = {}
    for job_name, names in required_steps.items():
        if job_name not in required or not isinstance(names, list) or not names:
            raise VerificationError(
                "required_steps must name a required job and non-empty step list"
            )
        job = by_name[job_name]
        steps = job.get("steps")
        if not isinstance(steps, list):
            raise VerificationError(f"malformed steps for required job: {job_name}")
        step_by_name: dict[str, dict[str, Any]] = {}
        for step in steps:
            step = _obj(step, "workflow job step")
            step_name = _nonempty(step.get("name"), "workflow job step name")
            if step_name in step_by_name:
                raise VerificationError(
                    f"duplicate step name in job {job_name}: {step_name}"
                )
            step_by_name[step_name] = step
        for step_name in names:
            _nonempty(step_name, "required step name")
            step = step_by_name.get(step_name)
            if (
                step is None
                or step.get("status") != "completed"
                or step.get("conclusion") != "success"
            ):
                raise VerificationError(
                    f"required test step did not succeed: {job_name} / {step_name}"
                )
        step_evidence[job_name] = list(names)
    return {
        "required_jobs": list(required),
        "allowed_skipped_jobs": [
            name
            for name in allowed_skipped
            if name in by_name and by_name[name].get("conclusion") == "skipped"
        ],
        "required_steps": step_evidence,
    }


def _validate_source_pair(locked: dict[str, Any], policy: dict[str, Any]) -> None:
    keys = ("repository", "repository_id", "ref")
    for key in keys:
        if locked.get(key) != policy.get(key):
            raise VerificationError(f"lock and CI policy disagree on {key}")
    workflow = _obj(locked.get("workflow"), "source workflow lock")
    for lock_key, policy_key in (
        ("id", "workflow_id"),
        ("path", "workflow_path"),
        ("blob", "workflow_blob"),
    ):
        if workflow.get(lock_key) != policy.get(policy_key):
            raise VerificationError(
                f"lock and CI policy disagree on workflow {lock_key}"
            )
    if not isinstance(policy.get("required_jobs"), list) or not policy["required_jobs"]:
        raise VerificationError("policy required_jobs must be a non-empty list")
    if not isinstance(policy.get("allowed_skipped_jobs"), list):
        raise VerificationError("policy allowed_skipped_jobs must be a list")
    for key in ("required_jobs", "allowed_skipped_jobs"):
        values = policy[key]
        if any(not isinstance(value, str) or not value for value in values) or len(
            values
        ) != len(set(values)):
            raise VerificationError(f"policy {key} must contain unique non-empty names")
    if set(policy["required_jobs"]) & set(policy["allowed_skipped_jobs"]):
        raise VerificationError("required and allowed-skipped job lists overlap")
    for field in ("name", "repository", "ref", "sha", "tree"):
        _nonempty(locked.get(field), f"source lock {field}")
    for field in ("sha", "tree"):
        value = locked[field]
        if len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
            raise VerificationError(
                f"malformed source lock {field}: expected lowercase Git SHA-1"
            )
    for field in ("id",):
        value = workflow.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise VerificationError("malformed source workflow lock id")
    for field in ("path", "blob"):
        _nonempty(workflow.get(field), f"source workflow lock {field}")
    if len(workflow["blob"]) != 40 or any(
        char not in "0123456789abcdef" for char in workflow["blob"]
    ):
        raise VerificationError(
            "malformed source workflow blob: expected lowercase Git SHA-1"
        )
    for field, value in (
        ("repository_id", locked.get("repository_id")),
        ("policy repository_id", policy.get("repository_id")),
        ("workflow_id", policy.get("workflow_id")),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise VerificationError(f"malformed source CI policy {field}")
    for field in ("workflow_path", "workflow_blob"):
        _nonempty(policy.get(field), f"source CI policy {field}")
    if policy["workflow_blob"] != workflow["blob"]:
        raise VerificationError(
            "policy workflow blob does not match locked workflow blob"
        )


def _verify_source(
    api: Any, locked: dict[str, Any], policy: dict[str, Any]
) -> dict[str, Any]:
    name = _nonempty(locked.get("name"), "source name")
    _validate_source_pair(locked, policy)
    repository, ref, sha = locked["repository"], locked["ref"], locked.get("sha")
    if not isinstance(repository, str) or repository.count("/") != 1:
        raise VerificationError("malformed source repository")
    repo = _obj(_get(api, repository, ""), "repository metadata")
    if repo.get("id") != locked["repository_id"] or repo.get("full_name") != repository:
        raise VerificationError(
            "repository name or numeric identity does not match the source lock"
        )
    if repo.get("private") is not False or repo.get("archived") is True:
        raise VerificationError("source repository is not an active public repository")
    if repo.get("default_branch") != ref:
        raise VerificationError(
            "locked source ref is not the repository default branch"
        )

    commit = _obj(
        _get(api, repository, f"/commits/{quote(sha, safe='')}"), "locked commit"
    )
    commit_data = _obj(commit.get("commit"), "locked commit data")
    tree = _obj(commit_data.get("tree"), "locked commit tree")
    if commit.get("sha") != sha or tree.get("sha") != locked.get("tree"):
        raise VerificationError(
            "locked commit SHA or tree does not match the source lock"
        )

    workflow_lock = locked["workflow"]
    workflow = _obj(
        _get(api, repository, f"/actions/workflows/{workflow_lock['id']}"),
        "workflow metadata",
    )
    if (
        workflow.get("id") != workflow_lock["id"]
        or workflow.get("path") != workflow_lock["path"]
    ):
        raise VerificationError(
            "native workflow id or path does not match the source lock"
        )
    if workflow.get("state") != "active":
        raise VerificationError("locked GitHub Actions workflow is not active")
    content_path = quote(workflow_lock["path"], safe="/")
    contents = _obj(
        _get(api, repository, f"/contents/{content_path}?ref={quote(sha, safe='')}"),
        "workflow file at locked commit",
    )
    if contents.get("sha") != workflow_lock["blob"]:
        raise VerificationError(
            "workflow file blob at locked commit does not match the source lock"
        )

    runs_path = f"/actions/workflows/{workflow_lock['id']}/runs"
    run_query = {"head_sha": sha, "event": "push", "branch": ref}
    first_rows = _pages(api, repository, runs_path, "workflow_runs", run_query)
    selected = _latest_matching(first_rows, locked)
    selected_signature = _run_signature(selected)
    if selected["status"] != "completed" or selected["conclusion"] != "success":
        raise VerificationError(
            "latest matching native workflow activity is not successful"
        )
    attempt = selected["run_attempt"]
    attempt_data = _obj(
        _get(api, repository, f"/actions/runs/{selected['id']}/attempts/{attempt}"),
        "selected workflow attempt",
    )
    if _run_identity_status_signature(attempt_data) != _run_identity_status_signature(
        selected
    ):
        raise VerificationError(
            "selected workflow attempt identity changed or mismatched"
        )
    first_attempt_signature = _run_signature(attempt_data)
    jobs_path = f"/actions/runs/{selected['id']}/attempts/{attempt}/jobs"
    first_jobs = _pages(api, repository, jobs_path, "jobs")
    job_evidence = _validate_job_rows(first_jobs, selected, policy)
    second_jobs = _pages(api, repository, jobs_path, "jobs")
    if [_job_signature(job) for job in first_jobs] != [
        _job_signature(job) for job in second_jobs
    ]:
        raise VerificationError("workflow jobs changed during verification")
    second_rows = _pages(api, repository, runs_path, "workflow_runs", run_query)
    second_selected = _latest_matching(second_rows, locked)
    if [_run_signature(row) for row in first_rows if _matches(row, locked)] != [
        _run_signature(row) for row in second_rows if _matches(row, locked)
    ] or _run_signature(second_selected) != selected_signature:
        raise VerificationError(
            "latest matching workflow activity changed during verification"
        )
    second_attempt = _obj(
        _get(api, repository, f"/actions/runs/{selected['id']}/attempts/{attempt}"),
        "selected workflow attempt recheck",
    )
    if _run_signature(second_attempt) != first_attempt_signature:
        raise VerificationError("selected workflow attempt changed during verification")
    return {
        "name": name,
        "status": "verified",
        "repository": repository,
        "repository_id": locked["repository_id"],
        "ref": ref,
        "sha": sha,
        "tree": locked["tree"],
        "workflow_id": workflow_lock["id"],
        "workflow_path": workflow_lock["path"],
        "workflow_blob": workflow_lock["blob"],
        "run_id": selected["id"],
        "run_attempt": attempt,
        "run_event": selected["event"],
        "run_status": selected["status"],
        "run_conclusion": selected["conclusion"],
        "run_updated_at": selected["updated_at"],
        "jobs": job_evidence,
        "evidence_source": "github-actions-api",
    }


def _job_signature(job: dict[str, Any]) -> tuple[Any, ...]:
    return (
        job.get("id"),
        job.get("run_id"),
        job.get("run_attempt"),
        job.get("head_sha"),
        job.get("name"),
        job.get("status"),
        job.get("conclusion"),
        tuple(
            (step.get("name"), step.get("status"), step.get("conclusion"))
            for step in job.get("steps", [])
            if isinstance(step, dict)
        ),
    )


def verify(lock_path: str, policy_path: str, api: Any) -> dict[str, Any]:
    lock = _obj(_read_json(lock_path), "source lock")
    policy_doc = _obj(_read_json(policy_path), "source CI policy")
    sources, policies = lock.get("sources"), policy_doc.get("sources")
    if not isinstance(sources, list) or not isinstance(policies, dict):
        raise VerificationError("malformed source lock or source CI policy")
    names = [
        _nonempty(_obj(row, "source lock entry").get("name"), "source name")
        for row in sources
    ]
    if len(set(names)) != len(names) or set(names) != set(policies):
        raise VerificationError(
            "source lock and CI policy must contain the same unique source names"
        )
    results = []
    for locked in sources:
        name = locked["name"]
        try:
            results.append(
                _verify_source(api, locked, _obj(policies[name], f"policy for {name}"))
            )
        except VerificationError as exc:
            results.append({"name": name, "status": "failed", "reason": str(exc)})
        except Exception as exc:
            results.append(
                {
                    "name": name,
                    "status": "failed",
                    "reason": f"unexpected verifier error ({type(exc).__name__})",
                }
            )
    return {
        "version": 1,
        "evidence_source": "github-actions-api",
        "sources": results,
        "passed": all(item["status"] == "verified" for item in results),
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", default="sources.lock.json")
    parser.add_argument("--policy", default="source-ci-policy.json")
    parser.add_argument("--output", help="write the human audit record to this path")
    args = parser.parse_args(argv)
    token = os.environ.get("GITHUB_TOKEN")
    if token is not None and ("\n" in token or "\r" in token):
        print("GITHUB_TOKEN contains invalid line breaks", file=sys.stderr)
        return 2
    try:
        result = verify(args.lock, args.policy, GitHubAPI(token))
    except VerificationError as exc:
        print(
            json.dumps(
                {"version": 1, "status": "failed", "reason": str(exc)}, sort_keys=True
            )
        )
        return 2
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        try:
            Path(args.output).write_text(rendered, encoding="utf-8")
        except OSError as exc:
            print(f"cannot write audit record ({type(exc).__name__})", file=sys.stderr)
            return 2
    print(rendered, end="")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
