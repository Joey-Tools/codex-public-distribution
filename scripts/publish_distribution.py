#!/usr/bin/env python3
"""Publish a validated personal Codex bundle as an immutable GitHub release."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


EXPECTED_REPO = "Joey-Tools/codex-public-distribution"
EXPECTED_REPO_ID = 1455178220
EXPECTED_DEFAULT_BRANCH = "master"
MAX_PACKAGE_BYTES = 512 * 1024 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_PAGES = 5
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
TAG_RE = re.compile(r"^personal-codex-\d{8}-\d{6}-([0-9a-f]{7})$")
ASSET_NAMES = ("personal-codex-{sha}.tar.gz", "personal-codex-{sha}.sha256")


class PublishError(RuntimeError):
    """A validation or remote-state error that must stop publication."""


@dataclass(frozen=True)
class LocalBundle:
    package: Path
    checksum_file: Path
    package_name: str
    checksum_name: str
    package_size: int
    package_sha256: str
    checksum_size: int
    checksum_sha256: str


class GitHubTransport:
    """Small REST transport; redirects are rejected to prevent credential forwarding."""

    def __init__(self, token: str, api_url: str = "https://api.github.com") -> None:
        self.token = token
        self.api_url = api_url.rstrip("/")

    def request(
        self,
        method: str,
        url: str,
        *,
        payload: Mapping[str, Any] | None = None,
        body: bytes | None = None,
        content_type: str = "application/json",
    ) -> Any:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in {
            "api.github.com",
            "uploads.github.com",
        }:
            raise PublishError("refusing an untrusted GitHub API or upload URL")
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "codex-public-distribution-publisher",
        }
        data = body
        if payload is not None:
            data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        if data is not None:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        opener = urllib.request.build_opener(_NoRedirect())
        try:
            with opener.open(request, timeout=30) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise PublishError(
                        "GitHub API response exceeded the configured limit"
                    )
                if not raw:
                    return None
                try:
                    return json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise PublishError("GitHub API returned malformed JSON") from exc
        except urllib.error.HTTPError as exc:
            # A response (including 404) is evidence only when its status is explicit.
            detail = exc.read(4096).decode("utf-8", errors="replace")
            raise PublishError(
                f"GitHub API {method} failed with HTTP {exc.code}: {detail[:500]}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise PublishError(
                f"GitHub API {method} failed without a conclusive response: {exc}"
            ) from exc


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def _api(path: str) -> str:
    return f"https://api.github.com{path}"


def _validate_sha(sha: str) -> str:
    value = sha.lower()
    if not SHA_RE.fullmatch(value):
        raise PublishError("--sha must be exactly 40 hexadecimal characters")
    return value


def validate_bundle(directory: Path, sha: str) -> LocalBundle:
    sha = _validate_sha(sha)
    package_name, checksum_name = (name.format(sha=sha) for name in ASSET_NAMES)
    package = directory / package_name
    checksum_file = directory / checksum_name
    if (
        not package.is_file()
        or package.is_symlink()
        or not checksum_file.is_file()
        or checksum_file.is_symlink()
    ):
        raise PublishError(
            "distribution directory must contain the exact package and checksum files"
        )
    package_size = package.stat().st_size
    checksum_size = checksum_file.stat().st_size
    if package_size <= 0 or package_size > MAX_PACKAGE_BYTES:
        raise PublishError(
            "package size is empty or exceeds the 512 MiB publisher limit"
        )
    if checksum_size > 256:
        raise PublishError("checksum file exceeds 256 bytes")
    package_digest = hashlib.sha256(package.read_bytes()).hexdigest()
    checksum_text = checksum_file.read_text(encoding="ascii")
    expected_line = f"{package_digest}  {package_name}\n"
    if checksum_text != expected_line:
        raise PublishError(
            "checksum file does not exactly match the package bytes and filename"
        )
    return LocalBundle(
        package,
        checksum_file,
        package_name,
        checksum_name,
        package_size,
        package_digest,
        checksum_size,
        hashlib.sha256(checksum_text.encode("ascii")).hexdigest(),
    )


def _expect_object(value: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise PublishError(f"GitHub returned malformed {what}")
    return value


def _list_releases(transport: Any) -> list[dict[str, Any]]:
    releases: list[dict[str, Any]] = []
    for page in range(1, MAX_PAGES + 1):
        url = _api(f"/repos/{EXPECTED_REPO}/releases?per_page=100&page={page}")
        value = transport.request("GET", url)
        if not isinstance(value, list):
            raise PublishError("GitHub returned malformed release list")
        releases.extend(_expect_object(item, "release entry") for item in value)
        if len(value) < 100:
            return releases
    raise PublishError("release pagination exceeded the bounded five-page scan")


def _repo_preflight(transport: Any, sha: str) -> None:
    repo = _expect_object(
        transport.request("GET", _api(f"/repos/{EXPECTED_REPO}")), "repository metadata"
    )
    if (
        repo.get("id") != EXPECTED_REPO_ID
        or repo.get("full_name", "").lower() != EXPECTED_REPO.lower()
    ):
        raise PublishError(
            "repository identity did not match the configured public target"
        )
    if (
        repo.get("private") is not False
        or repo.get("default_branch") != EXPECTED_DEFAULT_BRANCH
    ):
        raise PublishError(
            "target repository is not public on the configured master default branch"
        )
    commit = _expect_object(
        transport.request(
            "GET", _api(f"/repos/{EXPECTED_REPO}/commits/{EXPECTED_DEFAULT_BRANCH}")
        ),
        "default branch commit",
    )
    commit_sha = commit.get("sha")
    if not isinstance(commit_sha, str) or commit_sha.lower() != sha:
        raise PublishError("requested SHA is not the current master commit")


def _resolve_tag_commit(transport: Any, tag: str) -> str:
    encoded = urllib.parse.quote(tag, safe="")
    ref = _expect_object(
        transport.request(
            "GET", _api(f"/repos/{EXPECTED_REPO}/git/ref/tags/{encoded}")
        ),
        "tag ref",
    )
    obj = _expect_object(ref.get("object"), "tag object")
    kind, oid = obj.get("type"), obj.get("sha")
    if not isinstance(oid, str) or not SHA_RE.fullmatch(oid.lower()):
        raise PublishError("tag ref has malformed object SHA")
    if kind == "commit":
        return oid.lower()
    if kind != "tag":
        raise PublishError("release tag does not resolve to a commit or annotated tag")
    tag_obj = _expect_object(
        transport.request("GET", _api(f"/repos/{EXPECTED_REPO}/git/tags/{oid}")),
        "annotated tag",
    )
    target = _expect_object(tag_obj.get("object"), "annotated tag target")
    if target.get("type") != "commit" or not isinstance(target.get("sha"), str):
        raise PublishError("annotated release tag does not directly target a commit")
    return target["sha"].lower()


def _assets(transport: Any, release: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    raw_assets = release.get("assets")
    if not isinstance(raw_assets, list):
        raise PublishError("release has malformed asset list")
    result: dict[str, dict[str, Any]] = {}
    for raw in raw_assets:
        asset = _expect_object(raw, "asset")
        name = asset.get("name")
        if not isinstance(name, str) or name in result:
            raise PublishError("release contains a malformed or duplicate asset name")
        result[name] = dict(asset)
    return result


def _asset_valid(asset: Mapping[str, Any], name: str, size: int, digest: str) -> bool:
    return (
        asset.get("name") == name
        and asset.get("state") == "uploaded"
        and asset.get("size") == size
        and asset.get("digest") == f"sha256:{digest}"
    )


def _upload(transport: Any, release: Mapping[str, Any], path: Path, name: str) -> None:
    upload_template = release.get("upload_url")
    if not isinstance(upload_template, str):
        raise PublishError("draft release has no upload URL")
    upload_url = upload_template.split("{", 1)[0]
    separator = "&" if "?" in upload_url else "?"
    url = f"{upload_url}{separator}{urllib.parse.urlencode({'name': name})}"
    transport.request(
        "POST", url, body=path.read_bytes(), content_type="application/octet-stream"
    )


def _verify_release(
    transport: Any, release: Mapping[str, Any], sha: str, bundle: LocalBundle
) -> dict[str, Any]:
    tag = release.get("tag_name")
    if not isinstance(tag, str):
        raise PublishError("release has no tag")
    match = TAG_RE.fullmatch(tag)
    if match is None or match.group(1) != sha[:7]:
        raise PublishError(
            "release tag does not match the requested personal-codex SHA"
        )
    if _resolve_tag_commit(transport, tag) != sha:
        raise PublishError(
            "release tag does not peel to the exact requested source SHA"
        )
    assets = _assets(transport, release)
    expected = {bundle.package_name, bundle.checksum_name}
    if set(assets) != expected:
        raise PublishError("release does not contain exactly the two expected assets")
    checks = (
        (bundle.package_name, bundle.package_size, bundle.package_sha256),
        (bundle.checksum_name, bundle.checksum_size, bundle.checksum_sha256),
    )
    for name, size, digest in checks:
        if not _asset_valid(assets[name], name, size, digest):
            raise PublishError(
                f"release asset failed uploaded-state, size, or server digest verification: {name}"
            )
    if release.get("immutable") is not True:
        raise PublishError("GitHub release is not marked immutable")
    if release.get("draft") is not False or release.get("prerelease") is not False:
        raise PublishError("GitHub release is not published as a stable release")
    if release.get("target_commitish") not in (None, "", sha, EXPECTED_DEFAULT_BRANCH):
        raise PublishError("release target_commitish is unexpected")
    release_id = release.get("id")
    if not isinstance(release_id, int):
        raise PublishError("release has malformed ID")
    return {"published": True, "release_id": release_id, "tag": tag, "sha": sha}


def publish(
    transport: Any, directory: Path, sha: str, *, now: datetime | None = None
) -> dict[str, Any]:
    sha = _validate_sha(sha)
    bundle = validate_bundle(directory, sha)
    _repo_preflight(transport, sha)
    releases = _list_releases(transport)
    candidates = [
        r
        for r in releases
        if isinstance(r.get("tag_name"), str)
        and TAG_RE.fullmatch(r["tag_name"])
        and TAG_RE.fullmatch(r["tag_name"]).group(1) == sha[:7]
    ]
    if len(candidates) > 1:
        raise PublishError("more than one release tag matches the requested SHA prefix")
    if candidates:
        release = candidates[0]
        if release.get("draft") is False:
            result = _verify_release(transport, release, sha, bundle)
            result["status"] = "already_published"
            return result
        if release.get("draft") is not True:
            raise PublishError("matching release has an invalid draft state")
        if release.get("target_commitish") != sha:
            raise PublishError(
                "existing draft target_commitish does not match the exact requested SHA"
            )
    else:
        timestamp = (
            (now or datetime.now(timezone.utc))
            .astimezone(timezone.utc)
            .strftime("%Y%m%d-%H%M%S")
        )
        tag = f"personal-codex-{timestamp}-{sha[:7]}"
        # One create attempt only. If delivery is ambiguous, a later invocation selects the draft.
        release = _expect_object(
            transport.request(
                "POST",
                _api(f"/repos/{EXPECTED_REPO}/releases"),
                payload={
                    "tag_name": tag,
                    "target_commitish": sha,
                    "name": f"Personal Codex distribution {sha[:7]}",
                    "draft": True,
                    "prerelease": False,
                    "make_latest": "false",
                },
            ),
            "created draft release",
        )
        if release.get("tag_name") != tag or release.get("draft") is not True:
            raise PublishError(
                "created release did not match the requested draft intent"
            )
        if release.get("target_commitish") != sha:
            raise PublishError(
                "created draft target_commitish does not match the exact requested SHA"
            )

    assets = _assets(transport, release)
    allowed = {bundle.package_name, bundle.checksum_name}
    if set(assets) - allowed:
        raise PublishError(
            "matching draft contains unexpected assets; refusing to alter it"
        )
    expected_assets = (
        (
            bundle.package_name,
            bundle.package,
            bundle.package_size,
            bundle.package_sha256,
        ),
        (
            bundle.checksum_name,
            bundle.checksum_file,
            bundle.checksum_size,
            bundle.checksum_sha256,
        ),
    )
    for name, path, size, digest in expected_assets:
        existing = assets.get(name)
        if existing is not None:
            if not _asset_valid(existing, name, size, digest):
                raise PublishError(
                    f"existing draft asset conflicts with local bundle: {name}"
                )
        else:
            _upload(transport, release, path, name)

    release_id = release.get("id")
    if not isinstance(release_id, int):
        raise PublishError("draft release has malformed ID")
    # Re-read draft before publish so the exact uploaded server state is checked.
    current = _expect_object(
        transport.request("GET", _api(f"/repos/{EXPECTED_REPO}/releases/{release_id}")),
        "draft release",
    )
    current_assets = _assets(transport, current)
    if set(current_assets) != allowed:
        raise PublishError(
            "draft asset set changed or is incomplete before publication"
        )
    for name, _, size, digest in expected_assets:
        if not _asset_valid(current_assets[name], name, size, digest):
            raise PublishError(f"draft asset failed server digest verification: {name}")
    if (
        current.get("draft") is not True
        or current.get("tag_name") != release.get("tag_name")
        or current.get("target_commitish") != sha
    ):
        raise PublishError(
            "draft state, tag, or target_commitish changed before publication"
        )
    transport.request(
        "PATCH",
        _api(f"/repos/{EXPECTED_REPO}/releases/{release_id}"),
        payload={
            "draft": False,
            "prerelease": False,
            "target_commitish": sha,
            "make_latest": "true",
        },
    )
    published = _expect_object(
        transport.request("GET", _api(f"/repos/{EXPECTED_REPO}/releases/{release_id}")),
        "published release",
    )
    result = _verify_release(transport, published, sha, bundle)
    result["status"] = "published"
    return result


def _write_github_output(result: Mapping[str, Any]) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    with open(output_path, "a", encoding="utf-8") as stream:
        for key in ("status", "published", "release_id", "tag", "sha"):
            stream.write(
                f"{key}={str(result.get(key, '')).lower() if isinstance(result.get(key), bool) else result.get(key, '')}\n"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=EXPECTED_REPO)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--dist", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.repo != EXPECTED_REPO:
            raise PublishError(
                "--repo must equal the configured public distribution repository"
            )
        sha = _validate_sha(args.sha)
        if (
            os.environ.get("GITHUB_EVENT_NAME") != "push"
            or os.environ.get("GITHUB_REF") != f"refs/heads/{EXPECTED_DEFAULT_BRANCH}"
            or os.environ.get("GITHUB_SHA", "").lower() != sha
        ):
            raise PublishError(
                "live publication is allowed only for the exact default-branch push event"
            )
        token = os.environ.get("GITHUB_TOKEN", "")
        if not token:
            raise PublishError("GITHUB_TOKEN with contents:write is required")
        result = publish(GitHubTransport(token), args.dist, sha)
        _write_github_output(result)
        print(json.dumps(result, sort_keys=True))
        return 0
    except PublishError as exc:
        print(f"publish_distribution: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
