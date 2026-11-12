"""Offline behavioral tests for native source CI verification."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from scripts.source_ci import verify


REPOSITORY = "Example/source"
SHA = "a" * 40
TREE = "b" * 40
BLOB = "c" * 40
WORKFLOW_ID = 123
RUN_ID = 900


def run_record(**changes: Any) -> dict[str, Any]:
    run = {
        "id": RUN_ID,
        "workflow_id": WORKFLOW_ID,
        "head_sha": SHA,
        "head_branch": "main",
        "event": "push",
        "status": "completed",
        "conclusion": "success",
        "run_attempt": 1,
        "created_at": "2026-11-12T10:00:00Z",
        "updated_at": "2026-11-12T10:10:00Z",
        "repository": {"id": 7, "full_name": REPOSITORY},
        "head_repository": {"id": 7, "full_name": REPOSITORY},
    }
    run.update(changes)
    return run


def job(name: str, **changes: Any) -> dict[str, Any]:
    value = {
        "id": len(name) + 1,
        "run_id": RUN_ID,
        "run_attempt": 1,
        "head_sha": SHA,
        "name": name,
        "status": "completed",
        "conclusion": "success",
        "steps": [
            {"name": "Run tests", "status": "completed", "conclusion": "success"}
        ],
    }
    value.update(changes)
    return value


class FakeGitHub:
    def __init__(self) -> None:
        self.repo: dict[str, Any] = {
            "id": 7,
            "full_name": REPOSITORY,
            "private": False,
            "archived": False,
            "default_branch": "main",
        }
        self.current_branch_tip = SHA
        self.tree_sha = TREE
        self.workflow: dict[str, Any] = {
            "id": WORKFLOW_ID,
            "path": ".github/workflows/ci.yml",
            "state": "active",
        }
        self.blob_sha = BLOB
        self.runs = [run_record()]
        self.jobs = [job("test"), job("deploy-pages", conclusion="skipped")]
        self.run_list_calls = 0
        self.run_query_params: list[dict[str, Any]] = []
        self.job_list_calls = 0
        self.attempt_calls = 0
        self.attempt_updated_at: list[str] | None = None
        self.run_list_mutation: list[dict[str, Any]] | None = None

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if path == "/repos/Example/source":
            return self.repo
        if path == f"/repos/Example/source/commits/{SHA}":
            return {"sha": SHA, "commit": {"tree": {"sha": self.tree_sha}}}
        if path == f"/repos/Example/source/actions/workflows/{WORKFLOW_ID}":
            return self.workflow
        if path == f"/repos/Example/source/contents/.github/workflows/ci.yml?ref={SHA}":
            return {"sha": self.blob_sha}
        if path == f"/repos/Example/source/actions/workflows/{WORKFLOW_ID}/runs":
            self.run_list_calls += 1
            params = params or {}
            self.run_query_params.append(dict(params))
            rows = [
                row
                for row in self.runs
                if row["head_sha"] == params.get("head_sha")
                and row["event"] == params.get("event")
                and row["head_branch"] == params.get("branch")
            ]
            if self.run_list_mutation is not None and self.run_list_calls > 1:
                rows = self.run_list_mutation
            return self._page("workflow_runs", rows, params)
        prefix = "/repos/Example/source/actions/runs/"
        if path.startswith(prefix) and "/attempts/" in path and path.endswith("/jobs"):
            self.job_list_calls += 1
            return self._page("jobs", self.jobs, params)
        if path.startswith(prefix) and "/attempts/" in path:
            run_id, attempt = path[len(prefix) :].split("/attempts/")
            selected = next(
                (row for row in self.runs if row["id"] == int(run_id)), None
            )
            if selected is None or selected["run_attempt"] != int(attempt):
                raise AssertionError("requested an unlisted workflow attempt")
            self.attempt_calls += 1
            response = dict(selected)
            if self.attempt_updated_at is not None:
                index = min(self.attempt_calls - 1, len(self.attempt_updated_at) - 1)
                response["updated_at"] = self.attempt_updated_at[index]
            return response
        raise AssertionError(f"unexpected API path: {path}")

    @staticmethod
    def _page(
        key: str, rows: list[dict[str, Any]], params: dict[str, Any] | None
    ) -> dict[str, Any]:
        params = params or {}
        page = int(params.get("page", 1))
        size = int(params.get("per_page", 100))
        start = (page - 1) * size
        return {key: rows[start : start + size]}


class SourceCiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.lock_path = self.root / "sources.lock.json"
        self.policy_path = self.root / "source-ci-policy.json"
        locked = {
            "name": "example-source",
            "repository": REPOSITORY,
            "repository_id": 7,
            "ref": "main",
            "sha": SHA,
            "tree": TREE,
            "workflow": {
                "id": WORKFLOW_ID,
                "path": ".github/workflows/ci.yml",
                "blob": BLOB,
            },
        }
        policy = {
            "repository": REPOSITORY,
            "repository_id": 7,
            "ref": "main",
            "workflow_id": WORKFLOW_ID,
            "workflow_path": ".github/workflows/ci.yml",
            "workflow_blob": BLOB,
            "required_jobs": ["test"],
            "allowed_skipped_jobs": ["deploy-pages"],
        }
        self.lock_path.write_text(
            json.dumps({"version": 1, "sources": [locked]}), encoding="utf-8"
        )
        self.policy_path.write_text(
            json.dumps({"version": 1, "sources": {"example-source": policy}}),
            encoding="utf-8",
        )
        self.api = FakeGitHub()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def check(self) -> dict[str, Any]:
        return verify(str(self.lock_path), str(self.policy_path), self.api)

    def test_exact_native_success_passes_with_explicit_deployment_skip(self) -> None:
        result = self.check()
        self.assertTrue(result["passed"])
        source = result["sources"][0]
        self.assertEqual(source["evidence_source"], "github-actions-api")
        self.assertEqual(source["jobs"]["allowed_skipped_jobs"], ["deploy-pages"])
        self.assertEqual(len(self.api.run_query_params), 2)
        for params in self.api.run_query_params:
            self.assertEqual(params["head_sha"], SHA)
            self.assertEqual(params["event"], "push")
            self.assertEqual(params["branch"], "main")

    def test_advanced_default_branch_tip_does_not_invalidate_historical_pin(
        self,
    ) -> None:
        self.api.current_branch_tip = "d" * 40
        self.assertTrue(self.check()["passed"])

    def test_attempt_updated_at_may_differ_from_run_list_timestamp(self) -> None:
        self.api.attempt_updated_at = ["2026-11-12T10:10:01Z", "2026-11-12T10:10:01Z"]
        self.assertTrue(self.check()["passed"])

    def test_attempt_updated_at_change_between_attempt_reads_invalidates_proof(
        self,
    ) -> None:
        self.api.attempt_updated_at = ["2026-11-12T10:10:01Z", "2026-11-12T10:10:02Z"]
        result = self.check()
        self.assertFalse(result["passed"])
        self.assertIn(
            "attempt changed during verification", result["sources"][0]["reason"]
        )

    def test_required_step_must_be_present_and_successful(self) -> None:
        document = json.loads(self.policy_path.read_text(encoding="utf-8"))
        document["sources"]["example-source"]["required_steps"] = {
            "test": ["Run tests"]
        }
        self.policy_path.write_text(json.dumps(document), encoding="utf-8")
        self.assertTrue(self.check()["passed"])
        self.api.jobs[0]["steps"][0]["conclusion"] = "skipped"
        result = self.check()
        self.assertFalse(result["passed"])
        self.assertIn("required test step", result["sources"][0]["reason"])

    def test_repository_tree_workflow_and_blob_mismatches_fail(self) -> None:
        for mutate in (
            lambda api: api.repo.update(id=8),
            lambda api: setattr(api, "tree_sha", "d" * 40),
            lambda api: api.workflow.update(path=".github/workflows/other.yml"),
            lambda api: setattr(api, "blob_sha", "e" * 40),
        ):
            with self.subTest(mutate=mutate):
                self.api = FakeGitHub()
                mutate(self.api)
                self.assertFalse(self.check()["passed"])

    def test_missing_or_unknown_jobs_fail_even_when_run_succeeded(self) -> None:
        self.api.jobs = [job("deploy-pages", conclusion="skipped")]
        self.assertIn("missing required", self.check()["sources"][0]["reason"])
        self.api = FakeGitHub()
        self.api.jobs.append(job("unreviewed-extra"))
        self.assertIn("unexpected workflow job", self.check()["sources"][0]["reason"])
        self.api = FakeGitHub()
        self.api.jobs = [job("test")]
        self.assertIn(
            "missing configured optional", self.check()["sources"][0]["reason"]
        )

    def test_failed_or_pending_jobs_and_run_fail(self) -> None:
        self.api.jobs[0]["conclusion"] = "failure"
        self.assertIn("did not succeed", self.check()["sources"][0]["reason"])
        self.api = FakeGitHub()
        self.api.runs = [run_record(status="in_progress", conclusion=None)]
        self.assertIn("latest matching", self.check()["sources"][0]["reason"])

    def test_newer_failed_activity_wins_over_an_older_success(self) -> None:
        self.api.runs = [
            run_record(
                id=900,
                created_at="2026-11-12T10:00:00Z",
                updated_at="2026-11-12T10:10:00Z",
            ),
            run_record(
                id=901,
                conclusion="failure",
                created_at="2026-11-12T10:20:00Z",
                updated_at="2026-11-12T10:21:00Z",
            ),
        ]
        self.assertIn("latest matching", self.check()["sources"][0]["reason"])

    def test_newer_rerun_of_older_run_is_selected_by_activity_time(self) -> None:
        older = run_record(
            id=900,
            run_attempt=2,
            created_at="2026-11-12T10:00:00Z",
            updated_at="2026-11-12T10:30:00Z",
        )
        newer = run_record(
            id=901, created_at="2026-11-12T10:20:00Z", updated_at="2026-11-12T10:25:00Z"
        )
        self.api.runs = [newer, older]
        self.api.jobs = [
            job("test", run_id=900, run_attempt=2),
            job("deploy-pages", run_id=900, run_attempt=2, conclusion="skipped"),
        ]
        result = self.check()
        self.assertTrue(result["passed"])
        self.assertEqual(result["sources"][0]["run_id"], 900)
        self.assertEqual(result["sources"][0]["run_attempt"], 2)

    def test_new_matching_run_on_final_reread_invalidates_stability(self) -> None:
        self.api.run_list_mutation = [
            run_record(),
            run_record(
                id=901,
                status="in_progress",
                conclusion=None,
                created_at="2026-11-12T10:20:00Z",
                updated_at="2026-11-12T10:30:00Z",
            ),
        ]
        self.assertIn(
            "changed during verification", self.check()["sources"][0]["reason"]
        )

    def test_run_pagination_is_complete_before_selecting(self) -> None:
        earlier_matching = [
            run_record(
                id=1000 + n,
                created_at=f"2026-11-12T09:{n // 60:02d}:{n % 60:02d}Z",
                updated_at=f"2026-11-12T09:{n // 60:02d}:{n % 60:02d}Z",
            )
            for n in range(100)
        ]
        self.api.runs = earlier_matching + [run_record(id=2000)]
        self.api.jobs = [
            job("test", run_id=2000),
            job("deploy-pages", run_id=2000, conclusion="skipped"),
        ]
        self.assertTrue(self.check()["passed"])
        self.assertGreaterEqual(self.api.run_list_calls, 4)

    def test_malformed_run_response_is_reported_as_failure(self) -> None:
        class Malformed(FakeGitHub):
            def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
                if path.endswith("/runs"):
                    return {"jobs": []}
                return super().get_json(path, params)

        self.api = Malformed()
        self.assertIn("malformed workflow_runs", self.check()["sources"][0]["reason"])


if __name__ == "__main__":
    unittest.main()
