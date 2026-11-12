from __future__ import annotations

import hashlib
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from scripts.publish_distribution import (
    EXPECTED_REPO,
    EXPECTED_REPO_ID,
    GitHubHTTPError,
    PublishError,
    main,
    publish,
)


SHA = "a" * 40
OTHER_SHA = "b" * 40


def write_bundle(directory: Path, sha: str = SHA) -> tuple[Path, Path]:
    package = directory / f"personal-codex-{sha}.tar.gz"
    package.write_bytes(b"offline test archive bytes")
    checksum = directory / f"personal-codex-{sha}.sha256"
    checksum.write_text(
        f"{hashlib.sha256(package.read_bytes()).hexdigest()}  {package.name}\n",
        encoding="ascii",
    )
    return package, checksum


class FakeTransport:
    def __init__(
        self,
        *,
        existing: dict | None = None,
        tag_sha: str = SHA,
        immutable: bool = True,
        fail_method: str | None = None,
        malformed: bool = False,
        tag_http_status: int | None = None,
        mutate_tag_after_upload: int | None = None,
        tag_error: bool = False,
        tag_kind: str = "commit",
        tag_object_http_status: int | None = None,
    ) -> None:
        self.release = existing
        self.tag_sha = tag_sha
        self.immutable_on_publish = immutable
        self.fail_method = fail_method
        self.malformed = malformed
        self.tag_http_status = tag_http_status
        self.mutate_tag_after_upload = mutate_tag_after_upload
        self.tag_error = tag_error
        self.tag_kind = tag_kind
        self.tag_object_http_status = tag_object_http_status
        self.upload_count = 0
        self.calls: list[tuple[str, str, dict | None, bytes | None]] = []
        self.next_id = 99
        self.tag_exists = bool(existing and existing.get("draft") is False)

    def request(
        self,
        method: str,
        url: str,
        *,
        payload=None,
        body=None,
        content_type="application/json",
    ):
        self.calls.append((method, url, payload, body))
        if method == self.fail_method:
            raise PublishError("injected API error")
        path = urlsplit(url).path
        if path == f"/repos/{EXPECTED_REPO}" and method == "GET":
            if self.malformed:
                return []
            return {
                "id": EXPECTED_REPO_ID,
                "full_name": EXPECTED_REPO,
                "private": False,
                "default_branch": "master",
            }
        if path == f"/repos/{EXPECTED_REPO}/commits/master" and method == "GET":
            return {"sha": SHA}
        if path == f"/repos/{EXPECTED_REPO}/releases" and method == "GET":
            return [self.release] if self.release else []
        if path == f"/repos/{EXPECTED_REPO}/releases" and method == "POST":
            tag = payload["tag_name"]
            self.tag_sha = SHA
            self.release = self._release(tag, draft=True)
            return self.release
        if path.startswith(f"/repos/{EXPECTED_REPO}/git/ref/tags/") and method == "GET":
            if self.tag_error:
                raise PublishError("injected tag transport error")
            if self.tag_http_status is not None:
                raise GitHubHTTPError(
                    self.tag_http_status, "injected tag lookup failure"
                )
            if not self.tag_exists:
                raise GitHubHTTPError(404, "tag ref is absent")
            tag = path.rsplit("/", 1)[1]
            return {"object": {"type": self.tag_kind, "sha": self.tag_sha}}
        if path.startswith(f"/repos/{EXPECTED_REPO}/git/tags/") and method == "GET":
            if self.tag_object_http_status is not None:
                raise GitHubHTTPError(
                    self.tag_object_http_status, "injected annotated tag lookup failure"
                )
            return {"object": {"type": "commit", "sha": self.tag_sha}}
        if path.startswith(f"/repos/{EXPECTED_REPO}/releases/"):
            if path.endswith("/assets") and method == "POST":
                name = parse_qs(urlsplit(url).query)["name"][0]
                self._add_asset(name, body)
                return {"id": len(self.release["assets"])}
            if method == "GET":
                return self.release
            if method == "PATCH":
                self.release["draft"] = payload["draft"]
                self.release["prerelease"] = payload["prerelease"]
                self.release["target_commitish"] = payload["target_commitish"]
                self.tag_exists = True
                self.release["immutable"] = self.immutable_on_publish
                return self.release
        raise AssertionError(f"unexpected API call: {method} {url}")

    def _release(self, tag: str, *, draft: bool) -> dict:
        return {
            "id": self.next_id,
            "tag_name": tag,
            "target_commitish": SHA,
            "upload_url": f"https://uploads.github.com/repos/{EXPECTED_REPO}/releases/{self.next_id}/assets{{?name,label}}",
            "draft": draft,
            "prerelease": False,
            "immutable": not draft,
            "assets": [],
        }

    def _add_asset(self, name: str, body: bytes) -> None:
        self.release["assets"].append(
            {
                "name": name,
                "state": "uploaded",
                "size": len(body),
                "digest": f"sha256:{hashlib.sha256(body).hexdigest()}",
            }
        )
        self.upload_count += 1
        if self.mutate_tag_after_upload == self.upload_count:
            self.tag_sha = OTHER_SHA


class PublishDistributionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.package, self.checksum = write_bundle(self.directory)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_new_release_uploads_two_assets_and_verifies_immutable_terminal_state(
        self,
    ) -> None:
        transport = FakeTransport()
        result = publish(
            transport,
            self.directory,
            SHA,
            now=datetime(2026, 11, 12, 11, 30, 5, tzinfo=timezone.utc),
        )
        self.assertEqual(
            result,
            {
                "published": True,
                "release_id": 99,
                "tag": "personal-codex-20261112-113005-aaaaaaa",
                "sha": SHA,
                "status": "published",
            },
        )
        uploads = [
            call
            for call in transport.calls
            if call[0] == "POST" and "/assets?" in call[1]
        ]
        self.assertEqual(len(uploads), 2)
        patch_index = next(
            i
            for i, call in enumerate(transport.calls)
            if call[0] == "PATCH" and "/releases/" in call[1]
        )
        tag_read_indices = [
            i for i, call in enumerate(transport.calls) if "/git/ref/tags/" in call[1]
        ]
        self.assertTrue(tag_read_indices)
        self.assertEqual(len(tag_read_indices), 3)
        self.assertTrue(all(index < patch_index for index in tag_read_indices[:2]))
        self.assertGreater(tag_read_indices[-1], patch_index)
        self.assertEqual(
            transport.calls[patch_index][2],
            {
                "draft": False,
                "prerelease": False,
                "target_commitish": SHA,
                "make_latest": "true",
            },
        )

    def test_resumed_draft_with_mutated_target_commitish_fails_before_upload(
        self,
    ) -> None:
        tag = "personal-codex-20261112-113005-aaaaaaa"
        transport = FakeTransport()
        transport.release = transport._release(tag, draft=True)
        transport.release["target_commitish"] = OTHER_SHA
        with self.assertRaisesRegex(PublishError, "target_commitish does not match"):
            publish(transport, self.directory, SHA)
        self.assertFalse(any(call[0] in {"POST", "PATCH"} for call in transport.calls))

    def test_correct_existing_release_is_idempotent(self) -> None:
        transport = FakeTransport()
        publish(
            transport,
            self.directory,
            SHA,
            now=datetime(2026, 11, 12, tzinfo=timezone.utc),
        )
        before = len(transport.calls)
        result = publish(transport, self.directory, SHA)
        self.assertEqual(result["status"], "already_published")
        self.assertEqual(
            len(transport.calls) - before, 4
        )  # repo, branch, release list, peeled tag

    def test_matching_tag_that_peels_to_another_commit_fails(self) -> None:
        release = {
            "id": 1,
            "tag_name": "personal-codex-20261112-113005-aaaaaaa",
            "draft": False,
            "assets": [],
        }
        transport = FakeTransport(existing=release, tag_sha=OTHER_SHA)
        with self.assertRaisesRegex(PublishError, "exact requested source SHA"):
            publish(transport, self.directory, SHA)

    def test_existing_wrong_tag_rejects_draft_before_asset_upload(self) -> None:
        tag = "personal-codex-20261112-113005-aaaaaaa"
        transport = FakeTransport(tag_sha=OTHER_SHA)
        transport.release = transport._release(tag, draft=True)
        transport.tag_exists = True
        with self.assertRaisesRegex(PublishError, "existing release tag"):
            publish(transport, self.directory, SHA)
        self.assertFalse(any("/assets?" in call[1] for call in transport.calls))
        self.assertFalse(any(call[0] == "PATCH" for call in transport.calls))

    def test_tag_absence_is_accepted_only_as_exact_ref_http_404(self) -> None:
        # New and resumed drafts use this path; only the exact ref GET can classify absence.
        result = publish(FakeTransport(), self.directory, SHA)
        self.assertEqual(result["status"], "published")

    def test_tag_http_forbidden_and_server_errors_fail_before_publication(self) -> None:
        for status in (403, 500):
            with self.subTest(status=status):
                transport = FakeTransport(tag_http_status=status)
                with self.assertRaises(GitHubHTTPError) as caught:
                    publish(transport, self.directory, SHA)
                self.assertEqual(caught.exception.status_code, status)
                self.assertFalse(any(call[0] == "PATCH" for call in transport.calls))

    def test_tag_transport_and_missing_annotated_object_fail_closed(self) -> None:
        transport_error = FakeTransport(tag_error=True)
        with self.assertRaisesRegex(PublishError, "tag transport error"):
            publish(transport_error, self.directory, SHA)
        self.assertFalse(any(call[0] == "PATCH" for call in transport_error.calls))

        missing_object = FakeTransport(tag_kind="tag", tag_object_http_status=404)
        missing_object.release = missing_object._release(
            "personal-codex-20261112-113005-aaaaaaa", draft=True
        )
        missing_object.tag_exists = True
        with self.assertRaises(GitHubHTTPError) as caught:
            publish(missing_object, self.directory, SHA)
        self.assertEqual(caught.exception.status_code, 404)
        self.assertFalse(any(call[0] == "PATCH" for call in missing_object.calls))

    def test_tag_binding_mutation_during_uploads_fails_final_recheck(self) -> None:
        tag = "personal-codex-20261112-113005-aaaaaaa"
        transport = FakeTransport(mutate_tag_after_upload=1)
        transport.release = transport._release(tag, draft=True)
        transport.tag_exists = True
        with self.assertRaisesRegex(PublishError, "existing release tag"):
            publish(transport, self.directory, SHA)
        self.assertEqual(transport.upload_count, 2)
        self.assertFalse(any(call[0] == "PATCH" for call in transport.calls))

    def test_partial_matching_draft_resumes_only_missing_asset(self) -> None:
        tag = "personal-codex-20261112-113005-aaaaaaa"
        transport = FakeTransport()
        transport.release = transport._release(tag, draft=True)
        transport._add_asset(self.package.name, self.package.read_bytes())
        result = publish(transport, self.directory, SHA)
        uploads = [
            call
            for call in transport.calls
            if call[0] == "POST" and "/assets?" in call[1]
        ]
        self.assertEqual(len(uploads), 1)
        self.assertIn(self.checksum.name, uploads[0][1])
        self.assertEqual(result["status"], "published")

    def test_nonimmutable_terminal_release_fails_without_success_result(self) -> None:
        with self.assertRaisesRegex(PublishError, "not marked immutable"):
            publish(FakeTransport(immutable=False), self.directory, SHA)

    def test_bad_or_missing_local_inputs_fail_before_any_network_call(self) -> None:
        transport = FakeTransport()
        self.checksum.write_text("0" * 64 + "  wrong-name\n", encoding="ascii")
        with self.assertRaisesRegex(PublishError, "checksum file"):
            publish(transport, self.directory, SHA)
        self.assertEqual(transport.calls, [])
        self.checksum.unlink()
        with self.assertRaisesRegex(PublishError, "exact package and checksum"):
            publish(transport, self.directory, SHA)
        self.assertEqual(transport.calls, [])

    def test_malformed_remote_metadata_and_api_errors_fail_closed(self) -> None:
        with self.assertRaisesRegex(PublishError, "malformed repository metadata"):
            publish(FakeTransport(malformed=True), self.directory, SHA)
        with self.assertRaisesRegex(PublishError, "injected API error"):
            publish(FakeTransport(fail_method="GET"), self.directory, SHA)

    def test_cli_requires_exact_default_branch_push_scope(self) -> None:
        import os

        old = {
            key: os.environ.get(key)
            for key in ("GITHUB_EVENT_NAME", "GITHUB_REF", "GITHUB_SHA", "GITHUB_TOKEN")
        }
        try:
            os.environ.update(
                {
                    "GITHUB_EVENT_NAME": "workflow_dispatch",
                    "GITHUB_REF": "refs/heads/master",
                    "GITHUB_SHA": SHA,
                    "GITHUB_TOKEN": "offline-placeholder",
                }
            )
            self.assertEqual(main(["--sha", SHA, "--dist", str(self.directory)]), 1)
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
