"""Offline behavior tests for the public source-update writer and merger."""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from scripts import source_update_bot as bot


APP_SLUG = "public-source-writer"
APP_LOGIN = APP_SLUG + "[bot]"
REPOSITORY = bot.PUBLIC_REPOSITORY
REPOSITORY_ID = 202
REPOSITORY_NODE_ID = "R_kgDOExample"
WORKFLOW_ID = 8801
BASE_SHA = "a" * 40
HEAD_SHA = "d" * 40
MERGE_SHA = "e" * 40
SOURCE_LOCK_METADATA = {
    "name": "example-source",
    "repository": "Example/source",
    "repository_id": 7,
    "ref": "main",
    "workflow": {
        "id": 123,
        "path": ".github/workflows/ci.yml",
        "blob": "c" * 40,
    },
}
LOCK_DOCUMENT = {
    "version": 1,
    "sources": [{**SOURCE_LOCK_METADATA, "sha": "d" * 40, "tree": "e" * 40}],
}
BASE_LOCK_DOCUMENT = {
    "version": 1,
    "sources": [{**SOURCE_LOCK_METADATA, "sha": "1" * 40, "tree": "2" * 40}],
}
POLICY_DOCUMENT = {
    "version": 1,
    "sources": {
        "example-source": {
            "repository": "Example/source",
            "repository_id": 7,
            "ref": "main",
            "workflow_id": 123,
            "workflow_path": ".github/workflows/ci.yml",
            "workflow_blob": "c" * 40,
            "required_jobs": ["test"],
            "allowed_skipped_jobs": [],
        }
    },
}
LOCK_BYTES = (json.dumps(LOCK_DOCUMENT, separators=(",", ":")) + "\n").encode()
BASE_LOCK_BYTES = (
    json.dumps(BASE_LOCK_DOCUMENT, separators=(",", ":")) + "\n"
).encode()
POLICY_BYTES = (json.dumps(POLICY_DOCUMENT, separators=(",", ":")) + "\n").encode()
LOCK_DIGEST = hashlib.sha256(LOCK_BYTES).hexdigest()
BRANCH = f"codex/source-pins-{BASE_SHA[:12]}-{LOCK_DIGEST[:12]}"


def make_pr(
    *,
    branch: str = BRANCH,
    head: str = HEAD_SHA,
    base: str = BASE_SHA,
    author: str = APP_LOGIN,
    repository: str = REPOSITORY,
    body: str | None = None,
) -> dict[str, Any]:
    if body is None:
        body = bot._pr_body(base, LOCK_DIGEST)
    return {
        "number": 17,
        "state": "open",
        "merged": False,
        "html_url": "https://github.com/Joey-Tools/codex-public-distribution/pull/17",
        "body": body,
        "user": {"login": author},
        "mergeable": True,
        "mergeable_state": "clean",
        "head": {
            "ref": branch,
            "sha": head,
            "repo": {"full_name": repository, "id": REPOSITORY_ID},
        },
        "base": {
            "ref": bot.BASE_BRANCH,
            "sha": base,
            "repo": {"full_name": repository, "id": REPOSITORY_ID},
        },
    }


def run_record(
    *,
    run_id: int = 71,
    updated: str = "2026-11-12T10:10:00Z",
    status: str = "completed",
    conclusion: str | None = "success",
    head: str = HEAD_SHA,
    branch: str = BRANCH,
    attempt: int = 1,
    repository: str = REPOSITORY,
    head_repository: str = REPOSITORY,
) -> dict[str, Any]:
    return {
        "id": run_id,
        "workflow_id": WORKFLOW_ID,
        "head_sha": head,
        "head_branch": branch,
        "event": "pull_request",
        "status": status,
        "conclusion": conclusion,
        "run_attempt": attempt,
        "created_at": "2026-11-12T10:00:00Z",
        "updated_at": updated,
        "repository": {"id": REPOSITORY_ID, "full_name": repository},
        "head_repository": {"id": REPOSITORY_ID, "full_name": head_repository},
    }


def jobs_for(run: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": index,
            "run_id": run["id"],
            "run_attempt": run["run_attempt"],
            "head_sha": run["head_sha"],
            "name": name,
            "status": "completed",
            "conclusion": conclusion,
        }
        for index, (name, conclusion) in enumerate(bot.WORKFLOW_JOBS.items(), start=1)
    ]


def status_record(
    identity: int, context: str, state: str, created_at: str
) -> dict[str, Any]:
    return {
        "url": f"https://api.github.com/repos/{REPOSITORY}/statuses/{HEAD_SHA}",
        "avatar_url": "https://github.com/images/error/hubot_happy.gif",
        "id": identity,
        "node_id": f"MDY6U3RhdHVz{identity}==",
        "state": state,
        "description": f"{context} status {state}",
        "target_url": "https://ci.example.invalid/status",
        "context": context,
        "created_at": created_at,
        "updated_at": created_at,
    }


class FakeGitHub:
    """A deterministic fake for the allowlisted REST and GraphQL endpoints."""

    def __init__(self, *, repository: str = REPOSITORY) -> None:
        self.repo: dict[str, Any] = {
            "id": REPOSITORY_ID,
            "node_id": REPOSITORY_NODE_ID,
            "full_name": repository,
            "private": False,
            "archived": False,
            "default_branch": bot.BASE_BRANCH,
        }
        self.base_sha = BASE_SHA
        self.head_sha = HEAD_SHA
        self.branch = BRANCH
        self.ref_prefix = "refs/heads/"
        self.lock_bytes = LOCK_BYTES
        self.prs: list[dict[str, Any]] = []
        self.branches: dict[str, dict[str, Any]] = {
            bot.BASE_BRANCH: self._branch_state(BASE_SHA, None, "")
        }
        self.calls: list[tuple[str, str, Any]] = []
        self.mutations: list[tuple[str, str]] = []
        self.fail_mutation: str | None = None
        self.deliver_unknown = False
        self.workflow_runs = [run_record()]
        self.job_rows = jobs_for(self.workflow_runs[0])
        self.review_rows: list[tuple[str, bool]] = [("THREAD_1", True)]
        self.check_rows = [
            {
                "id": 901,
                "name": "Distribution",
                "app": {"id": 555},
                "head_sha": HEAD_SHA,
                "status": "completed",
                "conclusion": "success",
                "started_at": "2026-11-12T10:01:00Z",
                "completed_at": "2026-11-12T10:10:00Z",
                "check_suite": {"id": 902},
            }
        ]
        self.status_rows: list[dict[str, Any]] = []
        self.combined_state = "pending"
        self.pull_detail_calls = 0
        self.change_head_on_detail_call: int | None = None
        self.attempt_reads = 0
        self.changed_attempt_updated_at: list[str] | None = None
        self.jobs_reads = 0
        self.change_jobs_on_second_read = False

    def _branch_state(
        self, oid: str, parent: str | None, digest: str
    ) -> dict[str, Any]:
        return {
            "oid": oid,
            "signature": {"isValid": True, "wasSignedByGitHub": True},
            "parents": {"nodes": [] if parent is None else [{"oid": parent}]},
            "messageBody": "" if not digest else bot._commit_marker(digest),
        }

    def _page(
        self, rows: list[dict[str, Any]], params: dict[str, Any] | None
    ) -> list[dict[str, Any]]:
        params = params or {}
        page = int(params.get("page", 1))
        size = int(params.get("per_page", 100))
        start = (page - 1) * size
        return rows[start : start + size]

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}
        self.calls.append(("GET", path, dict(params)))
        if path == f"/repos/{REPOSITORY}":
            return dict(self.repo)
        if path == f"/repos/{REPOSITORY}/pulls":
            return self._page(self.prs, params)
        if path.startswith(f"/repos/{REPOSITORY}/pulls/") and path.endswith("/files"):
            return self._page(
                [
                    {
                        "filename": bot.LOCK_PATH,
                        "status": "modified",
                        "additions": 1,
                        "deletions": 1,
                        "changes": 2,
                        "sha": HEAD_SHA,
                    }
                ],
                params,
            )
        if path == f"/repos/{REPOSITORY}/pulls/17":
            self.pull_detail_calls += 1
            pr = dict(self.prs[0])
            if self.change_head_on_detail_call == self.pull_detail_calls:
                pr["head"] = dict(pr["head"], sha="f" * 40)
            return pr
        if path.startswith(f"/repos/{REPOSITORY}/compare/"):
            selected = path.removeprefix(f"/repos/{REPOSITORY}/compare/")
            left, right = selected.split("...", 1)
            return self._compare(left, right)
        if path == f"/repos/{REPOSITORY}/contents/{bot.LOCK_PATH}":
            return {
                "encoding": "base64",
                "content": base64.b64encode(self.lock_bytes).decode("ascii"),
                "sha": "c" * 40,
            }
        if path == f"/repos/{REPOSITORY}/actions/workflows/ci.yml":
            return {"id": WORKFLOW_ID, "path": bot.WORKFLOW_PATH, "state": "active"}
        if path == f"/repos/{REPOSITORY}/actions/workflows/{WORKFLOW_ID}/runs":
            rows = [
                row
                for row in self.workflow_runs
                if row["head_sha"] == params.get("head_sha")
                and row["head_branch"] == params.get("branch")
                and row["event"] == params.get("event")
            ]
            return {"workflow_runs": self._page(rows, params)}
        if "/attempts/" in path and path.endswith("/jobs"):
            self.jobs_reads += 1
            rows = list(self.job_rows)
            if self.change_jobs_on_second_read and self.jobs_reads == 2:
                rows[0] = dict(rows[0], conclusion="failure")
            return {"jobs": self._page(rows, params)}
        if (
            path.startswith(f"/repos/{REPOSITORY}/actions/runs/")
            and "/attempts/" in path
        ):
            self.attempt_reads += 1
            selected_path = path.removeprefix(f"/repos/{REPOSITORY}/actions/runs/")
            run_id, attempt_segment = selected_path.split("/attempts/", 1)
            attempt_text = attempt_segment
            selected = next(
                row for row in self.workflow_runs if row["id"] == int(run_id)
            )
            if selected["run_attempt"] != int(attempt_text):
                raise AssertionError("unexpected workflow run attempt")
            result = dict(selected)
            if self.changed_attempt_updated_at:
                index = min(
                    self.attempt_reads - 1, len(self.changed_attempt_updated_at) - 1
                )
                result["updated_at"] = self.changed_attempt_updated_at[index]
            return result
        if path == f"/repos/{REPOSITORY}/commits/{HEAD_SHA}/check-runs":
            return {"check_runs": self._page(self.check_rows, params)}
        if path == f"/repos/{REPOSITORY}/commits/{HEAD_SHA}/statuses":
            return self._page(self.status_rows, params)
        if path == f"/repos/{REPOSITORY}/commits/{HEAD_SHA}/status":
            return {
                "sha": HEAD_SHA,
                "state": self.combined_state,
                "total_count": len(
                    {
                        str(status.get("context", "default")).casefold()
                        for status in self.status_rows
                    }
                ),
            }
        raise AssertionError(f"unexpected API read: {path}")

    def graphql(
        self, query: str, variables: dict[str, Any], *, mutation: bool = False
    ) -> dict[str, Any]:
        operation = "mutation" if mutation else "query"
        self.calls.append(("GRAPHQL", operation, (query, variables)))
        if "createRef" in query and mutation:
            if "prefix" not in query:
                raise AssertionError("createRef query must request the Ref prefix")
            self.mutations.append(("graphql", "createRef"))
            name = variables["input"]["name"]
            branch = name.removeprefix("refs/heads/")
            self.branches[branch] = self._branch_state(self.base_sha, None, "")
            if self.fail_mutation == "createRef":
                if self.deliver_unknown:
                    self.branches[branch] = self._branch_state(self.base_sha, None, "")
                raise bot.IndeterminateMutation("fake uncertain createRef")
            return {
                "data": {
                    "createRef": {
                        "ref": {
                            "name": branch,
                            "prefix": self.ref_prefix,
                            "target": {"oid": self.base_sha},
                        }
                    }
                }
            }
        if "createCommitOnBranch" in query and mutation:
            self.mutations.append(("graphql", "createCommitOnBranch"))
            value = variables["input"]
            branch = value["branch"]["branchName"]
            self.lock_bytes = base64.b64decode(
                value["fileChanges"]["additions"][0]["contents"], validate=True
            )
            digest = hashlib.sha256(self.lock_bytes).hexdigest()
            self.branches[branch] = self._branch_state(HEAD_SHA, self.base_sha, digest)
            if self.fail_mutation == "createCommitOnBranch":
                if not self.deliver_unknown:
                    self.branches[branch] = self._branch_state(self.base_sha, None, "")
                raise bot.IndeterminateMutation("fake uncertain commit")
            return {
                "data": {
                    "createCommitOnBranch": {
                        "commit": {
                            "oid": HEAD_SHA,
                            "signature": {"isValid": True, "wasSignedByGitHub": True},
                            "parents": {"nodes": [{"oid": self.base_sha}]},
                            "messageBody": bot._commit_marker(digest),
                        }
                    }
                }
            }
        if "reviewThreads" in query:
            end = variables.get("after")
            if end is not None:
                return {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {
                                    "nodes": [],
                                    "pageInfo": {
                                        "hasNextPage": False,
                                        "endCursor": None,
                                    },
                                }
                            }
                        }
                    }
                }
            return {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "nodes": [
                                    {"id": identity, "isResolved": resolved}
                                    for identity, resolved in self.review_rows
                                ],
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                            }
                        }
                    }
                }
            }
        if "repository(owner:" in query and "ref(qualifiedName:" in query:
            if "prefix" not in query:
                raise AssertionError("Ref query must request the namespace prefix")
            name = variables["qualifiedName"].removeprefix("refs/heads/")
            state = self.branches.get(name)
            ref = None
            if state is not None:
                ref = {
                    "name": name,
                    "prefix": self.ref_prefix,
                    "target": {
                        "oid": state["oid"],
                        "signature": state["signature"],
                        "parents": state["parents"],
                        "messageBody": state["messageBody"],
                    },
                }
            return {"data": {"repository": {"ref": ref}}}
        raise AssertionError(f"unexpected GraphQL query: {query[:60]!r}")

    def post_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("POST", path, body))
        self.mutations.append(("POST", path))
        if path == f"/repos/{REPOSITORY}/pulls":
            branch = body["head"]
            pr = make_pr(
                branch=branch, head=HEAD_SHA, base=self.base_sha, body=body["body"]
            )
            pr["title"] = body["title"]
            self.prs = [pr]
            if self.fail_mutation == "pulls":
                if not self.deliver_unknown:
                    self.prs = []
                raise bot.IndeterminateMutation("fake uncertain pull request")
            return pr
        raise AssertionError(f"unexpected API mutation: {path}")

    def put_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("PUT", path, body))
        self.mutations.append(("PUT", path))
        if self.fail_mutation == "merge":
            raise bot.IndeterminateMutation("fake uncertain merge")
        if path == f"/repos/{REPOSITORY}/pulls/17/merge":
            if body != {"sha": HEAD_SHA, "merge_method": "squash"}:
                raise AssertionError("merge must bind the exact head and use squash")
            return {
                "merged": True,
                "sha": MERGE_SHA,
                "message": "Pull Request successfully merged",
            }
        raise AssertionError(f"unexpected API mutation: {path}")

    @staticmethod
    def _compare(left: str, right: str) -> dict[str, Any]:
        if left == right:
            return {
                "status": "identical",
                "ahead_by": 0,
                "behind_by": 0,
                "total_commits": 0,
                "files": [],
            }
        return {
            "status": "ahead",
            "ahead_by": 1,
            "behind_by": 0,
            "total_commits": 1,
            "files": [{"filename": bot.LOCK_PATH, "status": "modified"}],
        }


class SourceUpdateBotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self._git("init", "--initial-branch=master")
        self._git("config", "user.name", "Local Test")
        self._git("config", "user.email", "local@example.invalid")
        (self.root / bot.LOCK_PATH).write_bytes(BASE_LOCK_BYTES)
        (self.root / "source-ci-policy.json").write_bytes(POLICY_BYTES)
        (self.root / "README.md").write_text("public distribution\n", encoding="utf-8")
        self._git("add", bot.LOCK_PATH, "source-ci-policy.json", "README.md")
        self._git("commit", "-m", "base")
        self.base_sha = self._git("rev-parse", "HEAD").strip()
        (self.root / bot.LOCK_PATH).write_bytes(LOCK_BYTES)
        self.api = FakeGitHub()
        self.api.base_sha = self.base_sha
        self.api.branches = {
            bot.BASE_BRANCH: self.api._branch_state(self.base_sha, None, "")
        }
        self.api.branch = bot._base_lock_branch(self.base_sha, LOCK_DIGEST)
        self.api.prs = []

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.root), *args],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
        return result.stdout.decode().strip()

    def test_core_api_client_rejects_non_allowlisted_repository_routes(self) -> None:
        client = object.__new__(bot.GitHubAPI)
        self.assertEqual(
            client._endpoint(f"/repos/{REPOSITORY}/pulls", {"state": "open"}),
            f"repos/{REPOSITORY}/pulls?state=open",
        )
        for path in (
            "/repos/attacker/other/pulls",
            "https://api.github.com/repos/Joey-Tools/codex-public-distribution/pulls",
        ):
            with self.subTest(path=path), self.assertRaises(bot.APIError):
                client._endpoint(path, None)

    def test_core_api_environment_uses_only_explicit_token_without_config_pool(
        self,
    ) -> None:
        client = object.__new__(bot.GitHubAPI)
        client._token = "selected"
        with patch.dict(
            "os.environ",
            {
                "GH_TOKEN": "inherited",
                "GITHUB_TOKEN": "inherited",
                "GH_ENTERPRISE_TOKEN": "inherited",
                "GH_CONFIG_DIR": "/inherited/config",
            },
            clear=True,
        ):
            env = client._environment()
        self.assertEqual(env["GH_TOKEN"], "selected")
        self.assertEqual(env["GH_HOST"], "github.com")
        self.assertEqual(env["GH_PROMPT_DISABLED"], "1")
        for key in ("GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GH_CONFIG_DIR"):
            self.assertNotIn(key, env)

    def test_open_creates_native_signed_source_only_pr(self) -> None:
        result = bot.open_source_update(self.root, APP_SLUG, self.api)
        self.assertEqual(result.status, "opened")
        self.assertEqual(result.branch, self.api.branch)
        self.assertEqual(
            self.api.mutations,
            [
                ("graphql", "createRef"),
                ("graphql", "createCommitOnBranch"),
                ("POST", f"/repos/{REPOSITORY}/pulls"),
            ],
        )
        self.assertEqual(self.api.prs[0]["user"]["login"], APP_LOGIN)
        self.assertIn(bot.PR_MARKER, self.api.prs[0]["body"])
        commit_call = next(
            call
            for call in self.api.calls
            if call[0:2] == ("GRAPHQL", "mutation")
            and "createCommitOnBranch" in call[2][0]
        )
        self.assertEqual(commit_call[2][1]["input"]["expectedHeadOid"], self.base_sha)
        additions = commit_call[2][1]["input"]["fileChanges"]["additions"]
        self.assertEqual([item["path"] for item in additions], [bot.LOCK_PATH])
        self.assertEqual(
            additions[0]["contents"], base64.b64encode(LOCK_BYTES).decode("ascii")
        )
        self.assertEqual(self.api.lock_bytes, LOCK_BYTES)

    def test_second_open_coalesces_without_mutating(self) -> None:
        first = bot.open_source_update(self.root, APP_SLUG, self.api)
        mutation_count = len(self.api.mutations)
        second = bot.open_source_update(self.root, APP_SLUG, self.api)
        self.assertEqual(first.status, "opened")
        self.assertEqual(second.status, "coalesced")
        self.assertEqual(len(self.api.mutations), mutation_count)

    def test_base_only_native_ref_recovers_with_expected_head_cas(self) -> None:
        self.api.branches[self.api.branch] = self.api._branch_state(
            self.base_sha, None, ""
        )
        result = bot.open_source_update(self.root, APP_SLUG, self.api)
        self.assertEqual(result.status, "opened")
        self.assertEqual(
            self.api.mutations,
            [
                ("graphql", "createCommitOnBranch"),
                ("POST", f"/repos/{REPOSITORY}/pulls"),
            ],
        )

    def test_closed_or_merged_exact_source_pr_is_not_reopened(self) -> None:
        for state, merged, expected in (
            ("closed", False, "already_closed"),
            ("closed", True, "already_merged"),
        ):
            with self.subTest(state=state, merged=merged):
                pr = make_pr(
                    branch=self.api.branch,
                    head=HEAD_SHA,
                    base=self.base_sha,
                    body=bot._pr_body(self.base_sha, LOCK_DIGEST),
                )
                pr.update(state=state, merged=merged)
                self.api.prs = [pr]
                result = bot.open_source_update(self.root, APP_SLUG, self.api)
                self.assertEqual(result.status, expected)
                self.assertEqual(self.api.mutations, [])
                self.api.prs = []

    def test_unknown_delivery_is_not_rewritten_in_the_same_invocation(self) -> None:
        self.api.fail_mutation = "createRef"
        self.api.deliver_unknown = True
        with self.assertRaises(bot.IndeterminateMutation):
            bot.open_source_update(self.root, APP_SLUG, self.api)
        self.assertEqual(self.api.mutations, [("graphql", "createRef")])
        self.assertEqual(self.api.prs, [])

    def test_unknown_commit_delivery_recovers_from_native_branch_state(self) -> None:
        self.api.fail_mutation = "createCommitOnBranch"
        self.api.deliver_unknown = True
        with self.assertRaises(bot.IndeterminateMutation):
            bot.open_source_update(self.root, APP_SLUG, self.api)
        mutation_count = len(self.api.mutations)
        self.api.fail_mutation = None
        result = bot.open_source_update(self.root, APP_SLUG, self.api)
        self.assertEqual(result.status, "opened")
        self.assertEqual(
            self.api.mutations[mutation_count:],
            [("POST", f"/repos/{REPOSITORY}/pulls")],
        )

    def test_unknown_pr_delivery_recovers_by_observing_the_native_open_pr(self) -> None:
        self.api.fail_mutation = "pulls"
        self.api.deliver_unknown = True
        result = bot.open_source_update(self.root, APP_SLUG, self.api)
        self.assertEqual(result.status, "coalesced")
        self.assertEqual(len(self.api.prs), 1)
        mutation_count = len(self.api.mutations)
        self.api.fail_mutation = None
        second = bot.open_source_update(self.root, APP_SLUG, self.api)
        self.assertEqual(second.status, "coalesced")
        self.assertEqual(len(self.api.mutations), mutation_count)

    def test_open_rejects_other_tracked_changes_before_any_api_call(self) -> None:
        (self.root / "README.md").write_text("changed\n", encoding="utf-8")
        with self.assertRaisesRegex(bot.BotError, "only sources.lock.json"):
            bot.open_source_update(self.root, APP_SLUG, self.api)
        self.assertEqual(self.api.calls, [])

    def test_open_rejects_lock_metadata_pollution_before_any_api_call(self) -> None:
        polluted = dict(LOCK_DOCUMENT)
        polluted["review_policy"] = "waived"
        (self.root / bot.LOCK_PATH).write_text(
            json.dumps(polluted) + "\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(bot.BotError, "outside sha/tree"):
            bot.open_source_update(self.root, APP_SLUG, self.api)
        self.assertEqual(self.api.calls, [])

    def test_open_rejects_source_count_drift_before_any_api_call(self) -> None:
        changed = {**LOCK_DOCUMENT, "sources": []}
        (self.root / bot.LOCK_PATH).write_text(
            json.dumps(changed) + "\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(bot.BotError, "outside sha/tree"):
            bot.open_source_update(self.root, APP_SLUG, self.api)
        self.assertEqual(self.api.calls, [])

    def test_open_rejects_immutable_json_type_changes_before_any_api_call(self) -> None:
        changed = {**LOCK_DOCUMENT, "version": 1.0}
        (self.root / bot.LOCK_PATH).write_text(
            json.dumps(changed) + "\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(bot.BotError, "version is unsupported"):
            bot.open_source_update(self.root, APP_SLUG, self.api)
        self.assertEqual(self.api.calls, [])

    def test_open_rejects_duplicate_json_keys_before_any_api_call(self) -> None:
        (self.root / bot.LOCK_PATH).write_bytes(
            b'{"version":1,"version":1,"sources":[]}\n'
        )
        with self.assertRaisesRegex(bot.BotError, "strict UTF-8 JSON"):
            bot.open_source_update(self.root, APP_SLUG, self.api)
        self.assertEqual(self.api.calls, [])

    def test_open_rejects_wrong_repository_identity(self) -> None:
        self.api.repo["full_name"] = "someone-else/codex-public-distribution"
        with self.assertRaisesRegex(bot.BotError, "identity"):
            bot.open_source_update(self.root, APP_SLUG, self.api)
        self.assertEqual(self.api.mutations, [])

    def test_open_refuses_existing_same_branch_pr_from_another_actor(self) -> None:
        foreign = make_pr(
            branch=self.api.branch, head=HEAD_SHA, base=self.base_sha, author="human"
        )
        self.api.prs = [foreign]
        with self.assertRaisesRegex(bot.BotError, "unmanaged PR"):
            bot.open_source_update(self.root, APP_SLUG, self.api)
        self.assertEqual(self.api.mutations, [])

    def test_open_refuses_a_github_master_head_race(self) -> None:
        new_master = "b" * 40
        self.api.branches[bot.BASE_BRANCH] = self.api._branch_state(
            new_master, None, ""
        )
        with self.assertRaisesRegex(bot.BotError, "public master changed"):
            bot.open_source_update(self.root, APP_SLUG, self.api)
        self.assertEqual(self.api.mutations, [])

    def test_open_rejects_a_ref_with_the_wrong_namespace_prefix(self) -> None:
        self.api.ref_prefix = "refs/tags/"
        with self.assertRaisesRegex(bot.BotError, "Git reference name mismatch"):
            bot.open_source_update(self.root, APP_SLUG, self.api)
        self.assertEqual(self.api.mutations, [])

    def test_merge_ready_uses_latest_exact_head_ci_and_squash_sha_binding(self) -> None:
        self._seed_merge_pr()
        result = bot.merge_ready_source_update(APP_SLUG, self.api)
        self.assertEqual(result.status, "merged")
        merge_calls = [call for call in self.api.calls if call[0] == "PUT"]
        self.assertEqual(len(merge_calls), 1)
        self.assertEqual(merge_calls[0][2], {"sha": HEAD_SHA, "merge_method": "squash"})

    def test_merge_ignores_foreign_actor_and_foreign_branch_prs(self) -> None:
        self._seed_merge_pr()
        self.api.prs = [
            make_pr(author="human"),
            make_pr(branch="codex/manual-update", author=APP_LOGIN),
        ]
        result = bot.merge_ready_source_update(APP_SLUG, self.api)
        self.assertEqual(result.status, "no_candidate")
        self.assertEqual(self.api.mutations, [])

    def test_latest_failed_run_wins_over_old_success(self) -> None:
        self._seed_merge_pr()
        self.api.workflow_runs = [
            run_record(
                run_id=71,
                updated="2026-11-12T10:10:00Z",
                branch=self.api.branch,
            ),
            run_record(
                run_id=72,
                updated="2026-11-12T10:20:00Z",
                conclusion="failure",
                branch=self.api.branch,
            ),
        ]
        with self.assertRaisesRegex(bot.NotReady, "failed"):
            bot.merge_ready_source_update(APP_SLUG, self.api)
        self.assertEqual(self.api.mutations, [])

    def test_latest_pending_attempt_wins_over_old_green_run(self) -> None:
        self._seed_merge_pr()
        self.api.workflow_runs = [
            run_record(
                run_id=71,
                updated="2026-11-12T10:10:00Z",
                branch=self.api.branch,
            ),
            run_record(
                run_id=72,
                updated="2026-11-12T10:20:00Z",
                status="in_progress",
                conclusion=None,
                branch=self.api.branch,
            ),
        ]
        with self.assertRaisesRegex(bot.NotReady, "ci_run_pending"):
            bot.merge_ready_source_update(APP_SLUG, self.api)
        self.assertEqual(self.api.mutations, [])

    def test_exact_head_run_is_required(self) -> None:
        self._seed_merge_pr()
        self.api.workflow_runs = [run_record(head="f" * 40, branch=self.api.branch)]
        with self.assertRaisesRegex(bot.NotReady, "ci_run_missing"):
            bot.merge_ready_source_update(APP_SLUG, self.api)
        self.assertEqual(self.api.mutations, [])

    def test_workflow_head_repository_must_match_exact_public_repository(self) -> None:
        self._seed_merge_pr()
        self.api.workflow_runs = [
            run_record(branch=self.api.branch, head_repository="other/fork")
        ]
        with self.assertRaisesRegex(bot.NotReady, "ci_run_missing"):
            bot.merge_ready_source_update(APP_SLUG, self.api)
        self.assertEqual(self.api.mutations, [])

    def test_missing_or_unknown_job_blocks_merge(self) -> None:
        for rows in (
            jobs_for(run_record())[:-1],
            jobs_for(run_record())
            + [
                {
                    "id": 99,
                    "run_id": 71,
                    "run_attempt": 1,
                    "head_sha": HEAD_SHA,
                    "name": "unreviewed extra job",
                    "status": "completed",
                    "conclusion": "success",
                }
            ],
        ):
            with self.subTest(count=len(rows)):
                self._seed_merge_pr()
                self.api.job_rows = rows
                with self.assertRaises(bot.BotError):
                    bot.merge_ready_source_update(APP_SLUG, self.api)
                self.assertEqual(self.api.mutations, [])

    def test_publish_job_must_be_skipped_for_pull_request_ci(self) -> None:
        self._seed_merge_pr()
        self.api.job_rows[-1]["conclusion"] = "success"
        with self.assertRaisesRegex(bot.NotReady, "ci_job_failed_or_unexpected"):
            bot.merge_ready_source_update(APP_SLUG, self.api)
        self.assertEqual(self.api.mutations, [])

    def test_unresolved_review_thread_blocks_merge(self) -> None:
        self._seed_merge_pr()
        self.api.review_rows = [("THREAD_1", False)]
        with self.assertRaisesRegex(bot.NotReady, "unresolved_review_threads"):
            bot.merge_ready_source_update(APP_SLUG, self.api)
        self.assertEqual(self.api.mutations, [])

    def test_changed_head_during_stable_reread_blocks_merge(self) -> None:
        self._seed_merge_pr()
        self.api.change_head_on_detail_call = 2
        with self.assertRaisesRegex(bot.NotReady, "head_or_base_changed"):
            bot.merge_ready_source_update(APP_SLUG, self.api)
        self.assertEqual(self.api.mutations, [])

    def test_changed_attempt_jobs_during_reread_blocks_merge(self) -> None:
        self._seed_merge_pr()
        self.api.change_jobs_on_second_read = True
        with self.assertRaises(bot.BotError):
            bot.merge_ready_source_update(APP_SLUG, self.api)
        self.assertEqual(self.api.mutations, [])

    def test_changed_attempt_record_during_reread_blocks_merge(self) -> None:
        self._seed_merge_pr()
        self.api.changed_attempt_updated_at = [
            "2026-11-12T10:10:01Z",
            "2026-11-12T10:10:02Z",
        ]
        with self.assertRaisesRegex(bot.NotReady, "ci_changed_during_verification"):
            bot.merge_ready_source_update(APP_SLUG, self.api)
        self.assertEqual(self.api.mutations, [])

    def test_pending_check_or_status_blocks_merge(self) -> None:
        self._seed_merge_pr()
        self.api.check_rows[0]["status"] = "in_progress"
        with self.assertRaisesRegex(bot.NotReady, "check_run_pending"):
            bot.merge_ready_source_update(APP_SLUG, self.api)
        self.assertEqual(self.api.mutations, [])

        self._seed_merge_pr()
        self.api.status_rows = [
            status_record(
                7,
                "external/required",
                "pending",
                "2026-11-12T10:00:00Z",
            )
        ]
        with self.assertRaisesRegex(bot.NotReady, "commit_status_pending"):
            bot.merge_ready_source_update(APP_SLUG, self.api)
        self.assertEqual(self.api.mutations, [])

    def test_successful_commit_status_uses_exact_sha_endpoint_without_sha_field(
        self,
    ) -> None:
        self._seed_merge_pr()
        self.api.status_rows = [
            status_record(
                8,
                "external/required",
                "success",
                "2026-11-12T10:00:00Z",
            )
        ]
        self.api.combined_state = "success"

        result = bot.merge_ready_source_update(APP_SLUG, self.api)

        self.assertEqual(result.status, "merged")
        self.assertNotIn("sha", self.api.status_rows[0])
        status_endpoint = f"/repos/{REPOSITORY}/commits/{HEAD_SHA}/statuses"
        status_reads = [
            call
            for call in self.api.calls
            if call[0] == "GET" and call[1] == status_endpoint
        ]
        self.assertEqual(len(status_reads), 2)

    def test_latest_success_supersedes_older_statuses_for_same_context(self) -> None:
        self._seed_merge_pr()
        self.api.status_rows = [
            status_record(10, "External/Required", "success", "2026-11-12T10:03:00Z"),
            status_record(9, "external/required", "pending", "2026-11-12T10:02:00Z"),
            status_record(8, "EXTERNAL/REQUIRED", "failure", "2026-11-12T10:01:00Z"),
        ]
        self.api.combined_state = "success"

        result = bot.merge_ready_source_update(APP_SLUG, self.api)

        self.assertEqual(result.status, "merged")

    def test_latest_pending_or_failure_status_blocks_merge(self) -> None:
        for state in ("pending", "failure"):
            with self.subTest(state=state):
                self._seed_merge_pr()
                self.api.status_rows = [
                    status_record(
                        11, "external/required", state, "2026-11-12T10:03:00Z"
                    ),
                    status_record(
                        10, "external/required", "success", "2026-11-12T10:02:00Z"
                    ),
                ]
                self.api.combined_state = state
                with self.assertRaisesRegex(bot.NotReady, "commit_status_pending"):
                    bot.merge_ready_source_update(APP_SLUG, self.api)
                self.assertEqual(self.api.mutations, [])

    def test_failure_in_another_status_context_still_blocks_merge(self) -> None:
        self._seed_merge_pr()
        self.api.status_rows = [
            status_record(12, "external/required", "success", "2026-11-12T10:03:00Z"),
            status_record(11, "security/audit", "failure", "2026-11-12T10:02:00Z"),
        ]
        self.api.combined_state = "failure"

        with self.assertRaisesRegex(bot.NotReady, "commit_status_pending"):
            bot.merge_ready_source_update(APP_SLUG, self.api)
        self.assertEqual(self.api.mutations, [])

    def test_invalid_native_signature_blocks_merge(self) -> None:
        self._seed_merge_pr()
        self.api.branches[self.api.branch]["signature"]["wasSignedByGitHub"] = False
        with self.assertRaisesRegex(bot.BotError, "native GitHub signature"):
            bot.merge_ready_source_update(APP_SLUG, self.api)
        self.assertEqual(self.api.mutations, [])

    def test_non_source_pr_file_blocks_merge(self) -> None:
        self._seed_merge_pr()
        original_get = self.api.get_json

        def get_with_extra_file(path: str, params: dict[str, Any] | None = None) -> Any:
            if path.endswith("/pulls/17/files"):
                return [
                    {"filename": bot.LOCK_PATH, "status": "modified"},
                    {"filename": "README.md", "status": "modified"},
                ]
            return original_get(path, params)

        self.api.get_json = get_with_extra_file  # type: ignore[method-assign]
        with self.assertRaisesRegex(bot.BotError, "only sources.lock.json"):
            bot.merge_ready_source_update(APP_SLUG, self.api)
        self.assertEqual(self.api.mutations, [])

    def _seed_merge_pr(self) -> None:
        self.api.prs = [make_pr(base=self.base_sha)]
        self.api.base_sha = self.base_sha
        self.api.head_sha = HEAD_SHA
        self.api.branch = bot._base_lock_branch(self.base_sha, LOCK_DIGEST)
        self.api.branches = {
            bot.BASE_BRANCH: self.api._branch_state(self.base_sha, None, ""),
            self.api.branch: self.api._branch_state(
                HEAD_SHA, self.base_sha, LOCK_DIGEST
            ),
        }
        self.api.prs[0]["head"]["ref"] = self.api.branch
        self.api.prs[0]["head"]["sha"] = HEAD_SHA
        self.api.prs[0]["base"]["sha"] = self.base_sha
        self.api.prs[0]["body"] = bot._pr_body(self.base_sha, LOCK_DIGEST)
        self.api.workflow_runs = [run_record(branch=self.api.branch)]
        self.api.job_rows = jobs_for(self.api.workflow_runs[0])
        self.api.review_rows = [("THREAD_1", True)]
        self.api.check_rows = [
            {
                "id": 901,
                "name": "Distribution",
                "app": {"id": 555},
                "head_sha": HEAD_SHA,
                "status": "completed",
                "conclusion": "success",
                "started_at": "2026-11-12T10:01:00Z",
                "completed_at": "2026-11-12T10:10:00Z",
                "check_suite": {"id": 902},
            }
        ]
        self.api.status_rows = []
        self.api.combined_state = "pending"
        self.api.pull_detail_calls = 0
        self.api.attempt_reads = 0
        self.api.jobs_reads = 0
        self.api.change_head_on_detail_call = None
        self.api.changed_attempt_updated_at = None
        self.api.change_jobs_on_second_read = False
        self.api.mutations.clear()
        self.api.calls.clear()


if __name__ == "__main__":
    unittest.main()
