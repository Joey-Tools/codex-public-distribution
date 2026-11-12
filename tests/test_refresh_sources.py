"""Offline behavior tests for the bounded source-pin refresh CLI."""

from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from scripts import refresh_sources


EXPECTED_SOURCES = refresh_sources.EXPECTED_SOURCES


def fixture_documents() -> tuple[dict[str, Any], dict[str, Any]]:
    lock_sources: list[dict[str, Any]] = []
    policies: dict[str, dict[str, Any]] = {}
    for index, (name, identity) in enumerate(EXPECTED_SOURCES.items(), start=1):
        repository, ref = identity
        workflow_id = 1200 + index
        repository_id = 7000 + index
        old_sha = f"a{index:039x}"
        old_tree = f"b{index:039x}"
        workflow_blob = "c" * 40
        lock_sources.append(
            {
                "name": name,
                "repository": repository,
                "repository_id": repository_id,
                "ref": ref,
                "sha": old_sha,
                "tree": old_tree,
                "workflow": {
                    "id": workflow_id,
                    "path": ".github/workflows/ci.yml",
                    "blob": workflow_blob,
                },
            }
        )
        required_jobs = (
            ["test", "platform-safety"] if name == "codex-toolbox" else ["test"]
        )
        policies[name] = {
            "repository": repository,
            "repository_id": repository_id,
            "ref": ref,
            "workflow_id": workflow_id,
            "workflow_path": ".github/workflows/ci.yml",
            "workflow_blob": workflow_blob,
            "required_jobs": required_jobs,
            "allowed_skipped_jobs": ["deploy-pages"] if name == "archify" else [],
        }
        if name == "codex-toolbox":
            policies[name]["required_steps"] = {"test": ["Run tests"]}
    return (
        {
            "version": 1,
            "metadata": {"owner": "public-distribution"},
            "sources": lock_sources,
        },
        {"version": 1, "sources": policies},
    )


class FakeGitHub:
    def __init__(
        self,
        lock_path: Path,
        lock: dict[str, Any],
        policy: dict[str, Any],
        *,
        changed: set[str] | None = None,
        failed_source: str | None = None,
        missing_run_source: str | None = None,
        repository_mismatch: str | None = None,
        branch_name_mismatch: str | None = None,
        on_first_request: Any = None,
    ) -> None:
        self.lock_path = lock_path
        self.lock = lock
        self.policy = policy
        self.changed = changed or set()
        self.failed_source = failed_source
        self.missing_run_source = missing_run_source
        self.repository_mismatch = repository_mismatch
        self.branch_name_mismatch = branch_name_mismatch
        self.on_first_request = on_first_request
        self.called_first_request = False
        self.calls: list[str] = []
        self.native_run_repositories: list[str] = []
        self.repo_rows = {row["name"]: row for row in lock["sources"]}
        self.policy_rows = policy["sources"]
        self.by_repository = {row["repository"]: row for row in lock["sources"]}
        self.by_run_id = {
            500 + index: row for index, row in enumerate(lock["sources"], start=1)
        }
        self.tips: dict[str, tuple[str, str]] = {}
        self.current_tip_by_repo: dict[str, tuple[str, str]] = {}
        for index, row in enumerate(lock["sources"], start=1):
            if row["name"] in self.changed:
                self.current_tip_by_repo[row["repository"]] = (
                    f"d{index:039x}",
                    f"e{index:039x}",
                )
            else:
                self.current_tip_by_repo[row["repository"]] = (row["sha"], row["tree"])

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append(path)
        if not self.called_first_request:
            self.called_first_request = True
            if self.on_first_request is not None:
                self.on_first_request()

        if path.startswith("/repos/"):
            rest = path[len("/repos/") :]
            parts = rest.split("/", 2)
            if len(parts) < 2:
                raise AssertionError(f"malformed repository endpoint: {path}")
            owner, repo_name = parts[:2]
            suffix = parts[2] if len(parts) == 3 else ""
            full_name = f"{owner}/{repo_name}"
            source = self.by_repository[full_name]
            name = source["name"]
            policy = self.policy_rows[name]
            if not suffix:
                repository_id = source["repository_id"]
                if name == self.repository_mismatch:
                    repository_id += 1
                return {
                    "id": repository_id,
                    "full_name": full_name,
                    "private": False,
                    "archived": False,
                    "default_branch": source["ref"],
                }
            if suffix.startswith("branches/"):
                requested_ref = suffix[len("branches/") :]
                if requested_ref != source["ref"]:
                    raise AssertionError(f"unexpected branch lookup: {path}")
                sha, tree = self.current_tip_by_repo[full_name]
                self.tips[sha] = (name, tree)
                branch_name = (
                    "wrong-branch"
                    if name == self.branch_name_mismatch
                    else source["ref"]
                )
                return {
                    "name": branch_name,
                    "commit": {
                        "sha": sha,
                        "commit": {"tree": {"sha": tree}},
                    },
                }
            if suffix.startswith("commits/"):
                requested = suffix[len("commits/") :]
                if requested == source["ref"]:
                    sha, tree = self.current_tip_by_repo[full_name]
                    self.tips[sha] = (name, tree)
                    return {"sha": sha, "commit": {"tree": {"sha": tree}}}
                sha = requested
                resolved = self.tips.get(sha)
                if resolved is not None:
                    source_name, tree = resolved
                    if source_name != name:
                        raise AssertionError(
                            "commit resolved under the wrong repository"
                        )
                    return {"sha": sha, "commit": {"tree": {"sha": tree}}}
                tip_sha, tip_tree = self.current_tip_by_repo[full_name]
                if sha == source["sha"]:
                    return {"sha": sha, "commit": {"tree": {"sha": source["tree"]}}}
                if sha == tip_sha:
                    return {"sha": sha, "commit": {"tree": {"sha": tip_tree}}}
                raise AssertionError(f"unexpected commit lookup: {path}")
            if suffix == f"actions/workflows/{policy['workflow_id']}":
                return {
                    "id": policy["workflow_id"],
                    "path": policy["workflow_path"],
                    "state": "active",
                }
            if suffix.startswith("contents/"):
                return {"sha": policy["workflow_blob"]}
            if suffix == f"actions/workflows/{policy['workflow_id']}/runs":
                self.native_run_repositories.append(name)
                if name == self.missing_run_source:
                    return {"workflow_runs": []}
                run = self._run_for(source)
                return self._page("workflow_runs", [run], params)
            if "actions/runs/" in suffix and suffix.endswith("/jobs"):
                return self._page("jobs", self._jobs_for(source), params)
            if "actions/runs/" in suffix and "/attempts/" in suffix:
                return self._run_for(source)
        raise AssertionError(f"unexpected API path: {path}")

    def _run_for(self, source: dict[str, Any]) -> dict[str, Any]:
        name = source["name"]
        conclusion = "failure" if name == self.failed_source else "success"
        index = list(self.repo_rows).index(name) + 1
        sha, _tree = self.current_tip_by_repo[source["repository"]]
        return {
            "id": 500 + index,
            "workflow_id": source["workflow"]["id"],
            "head_sha": sha,
            "head_branch": source["ref"],
            "event": "push",
            "status": "completed",
            "conclusion": conclusion,
            "run_attempt": 1,
            "created_at": "2026-11-12T10:00:00Z",
            "updated_at": "2026-11-12T10:10:00Z",
            "repository": {
                "id": source["repository_id"],
                "full_name": source["repository"],
            },
            "head_repository": {
                "id": source["repository_id"],
                "full_name": source["repository"],
            },
        }

    def _jobs_for(self, source: dict[str, Any]) -> list[dict[str, Any]]:
        policy = self.policy_rows[source["name"]]
        jobs = []
        sha, _tree = self.current_tip_by_repo[source["repository"]]
        for index, name in enumerate(
            policy["required_jobs"] + policy["allowed_skipped_jobs"], start=1
        ):
            steps = []
            if name in policy.get("required_steps", {}):
                steps = [
                    {"name": step, "status": "completed", "conclusion": "success"}
                    for step in policy["required_steps"][name]
                ]
            jobs.append(
                {
                    "id": index,
                    "run_id": self._run_for(source)["id"],
                    "run_attempt": 1,
                    "head_sha": sha,
                    "name": name,
                    "status": "completed",
                    "conclusion": (
                        "skipped"
                        if name in policy["allowed_skipped_jobs"]
                        else "success"
                    ),
                    "steps": steps,
                }
            )
        return jobs

    @staticmethod
    def _page(
        key: str, rows: list[dict[str, Any]], params: dict[str, Any] | None
    ) -> dict[str, Any]:
        params = params or {}
        page = int(params.get("page", 1))
        size = int(params.get("per_page", 100))
        start = (page - 1) * size
        return {key: rows[start : start + size]}


class RefreshSourcesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.lock_path = self.root / "sources.lock.json"
        self.policy_path = self.root / "source-ci-policy.json"
        self.lock, self.policy = fixture_documents()
        self._write_inputs()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_inputs(self) -> None:
        self.lock_path.write_text(
            json.dumps(self.lock, indent=2) + "\n", encoding="utf-8"
        )
        self.policy_path.write_text(
            json.dumps(self.policy, indent=2) + "\n", encoding="utf-8"
        )

    def test_no_change_does_not_write_or_run_native_ci(self) -> None:
        original = self.lock_path.read_bytes()
        api = FakeGitHub(self.lock_path, self.lock, self.policy)
        with patch.object(refresh_sources, "_atomic_replace") as replace:
            result = refresh_sources.refresh_sources(self.root, api)
        self.assertFalse(result["changed"])
        self.assertEqual(len(result["candidate_lock_sha256"]), 64)
        self.assertEqual(api.native_run_repositories, [])
        self.assertEqual(
            sum("/branches/" in path for path in api.calls), len(EXPECTED_SOURCES)
        )
        self.assertFalse(any("/commits/" in path for path in api.calls))
        self.assertEqual(self.lock_path.read_bytes(), original)
        replace.assert_not_called()

    def test_single_source_pin_refresh_verifies_all_sources_before_write(self) -> None:
        api = FakeGitHub(
            self.lock_path, self.lock, self.policy, changed={"codex-toolbox"}
        )
        original_metadata = copy.deepcopy(self.lock)
        result = refresh_sources.refresh_sources(self.root, api)
        saved = json.loads(self.lock_path.read_text(encoding="utf-8"))
        self.assertTrue(result["changed"])
        self.assertEqual(len(result["candidate_lock_sha256"]), 64)
        self.assertEqual(len(api.native_run_repositories), 12)
        self.assertEqual(set(api.native_run_repositories), set(EXPECTED_SOURCES))
        for before, after in zip(
            original_metadata["sources"], saved["sources"], strict=True
        ):
            if before["name"] == "codex-toolbox":
                self.assertEqual(after["sha"], "d" + "0" * 38 + "1")
                self.assertEqual(after["tree"], "e" + "0" * 38 + "1")
            else:
                self.assertEqual(after["sha"], before["sha"])
                self.assertEqual(after["tree"], before["tree"])
            before_without_pins = {
                key: value
                for key, value in before.items()
                if key not in {"sha", "tree"}
            }
            after_without_pins = {
                key: value for key, value in after.items() if key not in {"sha", "tree"}
            }
            self.assertEqual(after_without_pins, before_without_pins)
        self.assertEqual(saved["metadata"], original_metadata["metadata"])

    def test_multiple_source_pin_refresh_updates_only_sha_and_tree(self) -> None:
        api = FakeGitHub(
            self.lock_path,
            self.lock,
            self.policy,
            changed={"archify", "codex-project-journal"},
        )
        original = copy.deepcopy(self.lock)
        result = refresh_sources.refresh_sources(self.root, api)
        saved = json.loads(self.lock_path.read_text(encoding="utf-8"))
        self.assertTrue(result["changed"])
        for before, after in zip(original["sources"], saved["sources"], strict=True):
            self.assertEqual(
                {
                    key: value
                    for key, value in after.items()
                    if key not in {"sha", "tree"}
                },
                {
                    key: value
                    for key, value in before.items()
                    if key not in {"sha", "tree"}
                },
            )
            if before["name"] in {"archify", "codex-project-journal"}:
                self.assertNotEqual(after["sha"], before["sha"])
                self.assertNotEqual(after["tree"], before["tree"])
            else:
                self.assertEqual(
                    (after["sha"], after["tree"]), (before["sha"], before["tree"])
                )

    def test_native_ci_failure_does_not_write_candidate(self) -> None:
        original = self.lock_path.read_bytes()
        api = FakeGitHub(
            self.lock_path,
            self.lock,
            self.policy,
            changed={"codex-toolbox"},
            failed_source="codex-project-journal",
        )
        with patch.object(refresh_sources, "_atomic_replace") as replace:
            with self.assertRaises(refresh_sources.RefreshError):
                refresh_sources.refresh_sources(self.root, api)
        self.assertEqual(self.lock_path.read_bytes(), original)
        replace.assert_not_called()

    def test_missing_native_ci_evidence_does_not_write_candidate(self) -> None:
        original = self.lock_path.read_bytes()
        api = FakeGitHub(
            self.lock_path,
            self.lock,
            self.policy,
            changed={"codex-toolbox"},
            missing_run_source="codex-project-journal",
        )
        with patch.object(refresh_sources, "_atomic_replace") as replace:
            with self.assertRaises(refresh_sources.RefreshError):
                refresh_sources.refresh_sources(self.root, api)
        self.assertEqual(self.lock_path.read_bytes(), original)
        replace.assert_not_called()

    def test_lock_workflow_metadata_drift_fails_without_write(self) -> None:
        original = self.lock_path.read_bytes()
        self.lock["sources"][0]["workflow"]["blob"] = "f" * 40
        self._write_inputs()
        before = self.lock_path.read_bytes()
        api = FakeGitHub(self.lock_path, self.lock, self.policy, changed={"archify"})
        with patch.object(refresh_sources, "_atomic_replace") as replace:
            with self.assertRaises(refresh_sources.RefreshError):
                refresh_sources.refresh_sources(self.root, api)
        self.assertNotEqual(before, original)
        self.assertEqual(self.lock_path.read_bytes(), before)
        replace.assert_not_called()

    def test_repository_identity_drift_fails_without_write(self) -> None:
        original = self.lock_path.read_bytes()
        api = FakeGitHub(
            self.lock_path,
            self.lock,
            self.policy,
            changed={"codex-toolbox"},
            repository_mismatch="archify",
        )
        with patch.object(refresh_sources, "_atomic_replace") as replace:
            with self.assertRaises(refresh_sources.RefreshError):
                refresh_sources.refresh_sources(self.root, api)
        self.assertEqual(self.lock_path.read_bytes(), original)
        replace.assert_not_called()

    def test_branch_name_mismatch_fails_without_write(self) -> None:
        original = self.lock_path.read_bytes()
        api = FakeGitHub(
            self.lock_path,
            self.lock,
            self.policy,
            changed={"codex-toolbox"},
            branch_name_mismatch="codex-toolbox",
        )
        with patch.object(refresh_sources, "_atomic_replace") as replace:
            with self.assertRaisesRegex(refresh_sources.RefreshError, "branch name"):
                refresh_sources.refresh_sources(self.root, api)
        self.assertEqual(self.lock_path.read_bytes(), original)
        self.assertEqual(
            api.calls[-1], "/repos/Joey-Tools/codex-toolbox/branches/master"
        )
        replace.assert_not_called()

    def test_api_exception_does_not_write_candidate(self) -> None:
        class FailingAPI(FakeGitHub):
            def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
                if path.startswith("/repos/Joey-Tools/"):
                    raise TimeoutError("details are intentionally not surfaced")
                return super().get_json(path, params)

        original = self.lock_path.read_bytes()
        api = FailingAPI(self.lock_path, self.lock, self.policy, changed={"archify"})
        with patch.object(refresh_sources, "_atomic_replace") as replace:
            with self.assertRaises(refresh_sources.RefreshError):
                refresh_sources.refresh_sources(self.root, api)
        self.assertEqual(self.lock_path.read_bytes(), original)
        replace.assert_not_called()

    def test_lock_content_change_during_probe_is_rejected(self) -> None:
        original = self.lock_path.read_bytes()

        def change_lock() -> None:
            modified = copy.deepcopy(self.lock)
            modified["metadata"]["operator"] = "external-change"
            self.lock_path.write_text(
                json.dumps(modified, indent=2) + "\n", encoding="utf-8"
            )

        api = FakeGitHub(
            self.lock_path,
            self.lock,
            self.policy,
            changed={"codex-toolbox"},
            on_first_request=change_lock,
        )
        externally_changed = (
            json.dumps(
                {
                    **self.lock,
                    "metadata": {
                        "owner": "public-distribution",
                        "operator": "external-change",
                    },
                },
                indent=2,
            )
            + "\n"
        )
        with patch.object(refresh_sources, "_atomic_replace") as replace:
            with self.assertRaisesRegex(
                refresh_sources.RefreshError, "content changed"
            ):
                refresh_sources.refresh_sources(self.root, api)
        self.assertNotEqual(self.lock_path.read_bytes(), original)
        self.assertEqual(self.lock_path.read_text(encoding="utf-8"), externally_changed)
        replace.assert_not_called()

    def test_policy_contract_change_during_probe_is_rejected(self) -> None:
        original_lock = self.lock_path.read_bytes()

        def change_policy() -> None:
            modified = copy.deepcopy(self.policy)
            modified["sources"]["codex-toolbox"]["required_jobs"] = ["test"]
            self.policy_path.write_text(
                json.dumps(modified, indent=2) + "\n", encoding="utf-8"
            )

        api = FakeGitHub(
            self.lock_path,
            self.lock,
            self.policy,
            changed={"codex-toolbox"},
            on_first_request=change_policy,
        )
        with patch.object(refresh_sources, "_atomic_replace") as replace:
            with self.assertRaisesRegex(
                refresh_sources.RefreshError, "source-ci-policy.json content changed"
            ):
                refresh_sources.refresh_sources(self.root, api)
        self.assertEqual(self.lock_path.read_bytes(), original_lock)
        replace.assert_not_called()

    def test_same_content_replacement_during_probe_is_rejected(self) -> None:
        original = self.lock_path.read_bytes()
        original_inode = self.lock_path.stat().st_ino

        def replace_lock() -> None:
            replacement = self.root / "replacement.json"
            replacement.write_bytes(original)
            os.replace(replacement, self.lock_path)

        api = FakeGitHub(
            self.lock_path,
            self.lock,
            self.policy,
            changed={"codex-toolbox"},
            on_first_request=replace_lock,
        )
        with patch.object(refresh_sources, "_atomic_replace") as replace:
            with self.assertRaisesRegex(
                refresh_sources.RefreshError, "object was replaced"
            ):
                refresh_sources.refresh_sources(self.root, api)
        self.assertEqual(self.lock_path.read_bytes(), original)
        self.assertNotEqual(self.lock_path.stat().st_ino, original_inode)
        replace.assert_not_called()

    def test_benign_mtime_and_child_churn_do_not_look_like_tampering(self) -> None:
        original = self.lock_path.read_bytes()

        def benign_transition() -> None:
            metadata = self.lock_path.stat()
            os.utime(
                self.lock_path,
                ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 10_000_000),
            )
            child = self.root / "temporary-child"
            child.write_text("transient", encoding="utf-8")
            child.unlink()

        api = FakeGitHub(
            self.lock_path,
            self.lock,
            self.policy,
            on_first_request=benign_transition,
        )
        result = refresh_sources.refresh_sources(self.root, api)
        self.assertFalse(result["changed"])
        self.assertEqual(self.lock_path.read_bytes(), original)

    def test_symlink_source_lock_is_rejected(self) -> None:
        original = self.lock_path.with_suffix(".original")
        os.replace(self.lock_path, original)
        self.lock_path.symlink_to(original)
        api = FakeGitHub(self.lock_path, self.lock, self.policy)
        with self.assertRaises(refresh_sources.RefreshError):
            refresh_sources.refresh_sources(self.root, api)

    def test_symlink_repo_root_is_rejected(self) -> None:
        alias = self.root.parent / f"{self.root.name}-link"
        alias.symlink_to(self.root, target_is_directory=True)
        try:
            api = FakeGitHub(self.lock_path, self.lock, self.policy)
            with self.assertRaisesRegex(refresh_sources.RefreshError, "repo root"):
                refresh_sources.refresh_sources(alias, api)
        finally:
            alias.unlink()

    def test_github_output_contains_only_changed_and_digest_summary(self) -> None:
        api = FakeGitHub(self.lock_path, self.lock, self.policy)
        result = refresh_sources.refresh_sources(self.root, api)
        output = self.root / "github-output.txt"
        refresh_sources._write_github_output(str(output), result, str(self.root))
        lines = output.read_text(encoding="ascii").splitlines()
        self.assertEqual(lines[0], "changed=false")
        self.assertEqual(
            lines[1], f"candidate_lock_sha256={result['candidate_lock_sha256']}"
        )
        self.assertEqual(set(result), {"changed", "candidate_lock_sha256"})


if __name__ == "__main__":
    unittest.main()
