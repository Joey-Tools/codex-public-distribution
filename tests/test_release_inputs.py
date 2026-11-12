from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import quote, urlsplit

from scripts import publish_distribution, release_inputs
from scripts.release_inputs import latest_published_sha, release_required


SHA = "a" * 40
OTHER_SHA = "b" * 40
TAG = f"personal-codex-20261112-120000-{SHA[:7]}"
LATEST_URL = publish_distribution._api(
    f"/repos/{publish_distribution.EXPECTED_REPO}/releases/latest"
)
TAG_URL = publish_distribution._api(
    f"/repos/{publish_distribution.EXPECTED_REPO}/git/ref/tags/{quote(TAG, safe='')}"
)


def valid_release() -> dict:
    return {
        "id": 17,
        "tag_name": TAG,
        "target_commitish": SHA,
        "draft": False,
        "prerelease": False,
        "immutable": True,
        "assets": [
            {
                "name": template.format(sha=SHA),
                "state": "uploaded",
                "size": 100,
                "digest": f"sha256:{str(index) * 64}",
            }
            for index, template in enumerate(publish_distribution.ASSET_NAMES, 1)
        ],
    }


class FakeAPI:
    def __init__(
        self,
        *,
        release: dict | None = None,
        latest_error: Exception | None = None,
        tag_error: Exception | None = None,
        tag_sha: str = SHA,
    ) -> None:
        self.release = valid_release() if release is None else release
        self.latest_error = latest_error
        self.tag_error = tag_error
        self.tag_sha = tag_sha
        self.calls: list[tuple[str, str]] = []

    def request(self, method: str, url: str) -> dict:
        self.calls.append((method, url))
        if url == LATEST_URL:
            if self.latest_error is not None:
                raise self.latest_error
            return self.release
        if url == TAG_URL:
            if self.tag_error is not None:
                raise self.tag_error
            return {"object": {"type": "commit", "sha": self.tag_sha}}
        raise AssertionError(f"unexpected API URL: {url}")


class ReleaseInputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Distribution Test")
        self.git("config", "user.email", "distribution-test@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        (self.root / "sources.lock.json").write_text("{}\n", encoding="utf-8")
        self.before = self.commit()

    def git(self, *arguments: str) -> str:
        return (
            subprocess.run(
                ["git", *arguments],
                cwd=self.root,
                capture_output=True,
                check=True,
                timeout=10,
            )
            .stdout.decode("ascii")
            .strip()
        )

    def commit(self, message: str = "fixture") -> str:
        self.git("add", ".")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def write_commit(self, path: str, contents: str, message: str) -> str:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents, encoding="utf-8")
        return self.commit(message)

    def assert_requires_release(self, *, baseline: str | None, after: str) -> None:
        self.assertTrue(
            release_required(
                self.root,
                event="push",
                after=after,
                published_sha=baseline,
            )
        )

    def test_docs_and_automation_edits_after_published_baseline_do_not_publish(self):
        self.write_commit("README.md", "Updated automation setup\n", "docs")
        after = self.write_commit(
            ".github/workflows/ci.yml", "updated workflow\n", "automation"
        )
        self.assertFalse(
            release_required(
                self.root,
                event="push",
                after=after,
                published_sha=self.before,
            )
        )

    def test_source_pin_change_requires_a_release(self):
        after = self.write_commit("sources.lock.json", '{"version":1}\n', "source pin")
        self.assert_requires_release(baseline=self.before, after=after)

    def test_packager_change_requires_a_release(self):
        after = self.write_commit(
            "scripts/archify_export.py", "# changed package transform\n", "builder"
        )
        self.assert_requires_release(baseline=self.before, after=after)

    def test_missing_or_non_ancestor_baseline_cannot_select_a_skip(self):
        with self.assertRaises(ValueError):
            release_required(
                self.root,
                event="push",
                after=self.before,
                published_sha="e" * 40,
            )
        tree = self.git("rev-parse", "HEAD^{tree}")
        unrelated = self.git("commit-tree", tree, "-m", "unrelated")
        with self.assertRaisesRegex(ValueError, "not an ancestor"):
            release_required(
                self.root,
                event="push",
                after=self.before,
                published_sha=unrelated,
            )
        with self.assertRaises(ValueError):
            release_required(
                self.root,
                event="push",
                after="f" * 40,
                published_sha=self.before,
            )

    def test_first_push_without_published_release_bootstraps_and_pull_request_skips(
        self,
    ):
        self.assert_requires_release(baseline=None, after=self.before)
        self.assertFalse(
            release_required(
                self.root,
                event="pull_request",
                after="",
                published_sha=None,
            )
        )

    def test_unpublished_source_change_survives_later_docs_and_ci_only_push(self):
        self.write_commit("sources.lock.json", '{"version":1}\n', "source pin")
        after = self.write_commit(
            "README.md", "CI repair notes\n", "repair docs and CI"
        )
        self.assert_requires_release(baseline=self.before, after=after)

    def test_docs_and_ci_only_after_real_release_baseline_skip(self):
        published = self.write_commit(
            "sources.lock.json", '{"version":1}\n', "source pin release"
        )
        after = self.write_commit("README.md", "CI repair notes\n", "repair docs")
        self.assertFalse(
            release_required(
                self.root,
                event="push",
                after=after,
                published_sha=published,
            )
        )

    def test_exact_latest_release_404_bootstraps_but_tag_404_is_an_error(self):
        api = FakeAPI(
            latest_error=publish_distribution.GitHubHTTPError(404, "not found")
        )
        self.assertIsNone(latest_published_sha(api))
        self.assertEqual(api.calls, [("GET", LATEST_URL)])
        with self.assertRaises(publish_distribution.GitHubHTTPError):
            latest_published_sha(
                FakeAPI(tag_error=publish_distribution.GitHubHTTPError(404, "missing"))
            )

    def test_latest_release_requires_stable_immutable_tag_and_exact_assets(self):
        cases = []
        for key, value in (
            ("draft", True),
            ("prerelease", True),
            ("immutable", False),
            ("target_commitish", OTHER_SHA),
            ("tag_name", "mutable-tag"),
        ):
            release = valid_release()
            release[key] = value
            cases.append((key, release, SHA))
        release = valid_release()
        release.pop("tag_name")
        cases.append(("missing tag", release, SHA))
        release = valid_release()
        release["assets"].pop()
        cases.append(("missing asset", release, SHA))
        release = valid_release()
        release["assets"].append(dict(release["assets"][0]))
        cases.append(("duplicate asset", release, SHA))
        release = valid_release()
        release["assets"][0]["state"] = "new"
        cases.append(("mutable asset", release, SHA))
        release = valid_release()
        release["assets"][0]["size"] = 0
        cases.append(("invalid size", release, SHA))
        release = valid_release()
        release["assets"][0]["digest"] = "sha256:broken"
        cases.append(("invalid digest", release, SHA))
        cases.append(("tag points elsewhere", valid_release(), OTHER_SHA))

        for label, release, tag_sha in cases:
            with (
                self.subTest(case=label),
                self.assertRaises(publish_distribution.PublishError),
            ):
                latest_published_sha(FakeAPI(release=release, tag_sha=tag_sha))
        with self.assertRaisesRegex(publish_distribution.PublishError, "malformed"):
            latest_published_sha(FakeAPI(release=[]))

    def test_api_failures_cannot_be_treated_as_missing_release(self):
        for status in (403, 500):
            with (
                self.subTest(status=status),
                self.assertRaises(publish_distribution.GitHubHTTPError),
            ):
                latest_published_sha(
                    FakeAPI(
                        latest_error=publish_distribution.GitHubHTTPError(
                            status, "injected"
                        )
                    )
                )
        with self.assertRaises(publish_distribution.PublishError):
            latest_published_sha(
                FakeAPI(latest_error=publish_distribution.PublishError("transport"))
            )

    def test_api_gets_are_limited_to_fixed_public_repo(self):
        api = FakeAPI()
        self.assertEqual(latest_published_sha(api), SHA)
        self.assertGreaterEqual(len(api.calls), 2)
        for method, url in api.calls:
            parsed = urlsplit(url)
            self.assertEqual(method, "GET")
            self.assertEqual(parsed.scheme, "https")
            self.assertEqual(parsed.hostname, "api.github.com")
            self.assertTrue(
                parsed.path.startswith(f"/repos/{publish_distribution.EXPECTED_REPO}/")
            )

    def test_non_push_cli_needs_no_token_or_api(self):
        class EnvironmentWithoutToken(dict):
            def get(self, key, default=None):
                if key == "GITHUB_TOKEN":
                    raise AssertionError("non-push CLI read the API token")
                return super().get(key, default)

        output = StringIO()
        with (
            patch.object(
                release_inputs.os,
                "environ",
                EnvironmentWithoutToken({"GITHUB_EVENT_NAME": "pull_request"}),
            ),
            patch.object(
                release_inputs,
                "latest_published_sha",
                side_effect=AssertionError("non-push CLI called the API"),
            ),
            redirect_stdout(output),
        ):
            self.assertEqual(release_inputs.main(["--repo-root", str(self.root)]), 0)
        self.assertEqual(output.getvalue(), "release_required=false\n")

    def test_push_cli_uses_explicit_token_and_bootstraps_from_exact_404(self):
        api = FakeAPI(
            latest_error=publish_distribution.GitHubHTTPError(404, "not found")
        )
        output = StringIO()
        with (
            patch.dict(
                release_inputs.os.environ,
                {
                    "GITHUB_EVENT_NAME": "push",
                    "GITHUB_SHA": self.before,
                    "GITHUB_TOKEN": "synthetic-test-token",
                },
                clear=True,
            ),
            patch.object(
                release_inputs.publisher, "GitHubTransport", return_value=api
            ) as factory,
            redirect_stdout(output),
        ):
            self.assertEqual(release_inputs.main(["--repo-root", str(self.root)]), 0)
        factory.assert_called_once_with("synthetic-test-token")
        self.assertEqual(output.getvalue(), "release_required=true\n")

    def test_push_cli_api_failure_exits_without_selecting_skip(self):
        api = FakeAPI(
            latest_error=publish_distribution.GitHubHTTPError(500, "injected")
        )
        output, error = StringIO(), StringIO()
        with (
            patch.dict(
                release_inputs.os.environ,
                {
                    "GITHUB_EVENT_NAME": "push",
                    "GITHUB_SHA": self.before,
                    "GITHUB_TOKEN": "synthetic-test-token",
                },
                clear=True,
            ),
            patch.object(release_inputs.publisher, "GitHubTransport", return_value=api),
            redirect_stdout(output),
            redirect_stderr(error),
        ):
            self.assertEqual(release_inputs.main(["--repo-root", str(self.root)]), 2)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("release_inputs: injected", error.getvalue())


if __name__ == "__main__":
    unittest.main()
