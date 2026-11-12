#!/usr/bin/env python3
"""Publish when committed distribution inputs differ from the last immutable release."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

if __package__:
    from . import publish_distribution as publisher
else:
    import publish_distribution as publisher


RELEASE_INPUTS = (
    "LICENSE",
    "distribution.json",
    "sources.lock.json",
    "source-ci-policy.json",
    "scripts/distribution.py",
    "scripts/archify_export.py",
    "scripts/source_ci.py",
    "scripts/publish_distribution.py",
)
SHA = re.compile(r"[0-9a-f]{40}\Z")
SHA256_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _git_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_PARAMETERS",
    ):
        environment.pop(name, None)
    environment.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_NO_LAZY_FETCH="1",
        GIT_OPTIONAL_LOCKS="0",
    )
    return environment


def _git(root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ["git", *arguments],
            cwd=root,
            env=_git_environment(),
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        raise ValueError("unable to inspect distribution Git history") from None


def _git_text(root: Path, *arguments: str) -> str:
    result = _git(root, *arguments)
    if result.returncode != 0:
        raise ValueError("unable to inspect distribution Git history")
    try:
        value = result.stdout.decode("ascii").strip()
    except UnicodeDecodeError:
        raise ValueError("Git returned a malformed commit SHA") from None
    if SHA.fullmatch(value) is None:
        raise ValueError("Git returned a malformed commit SHA")
    return value


def _require_commit(root: Path, sha: str, label: str) -> None:
    if SHA.fullmatch(sha) is None:
        raise ValueError(f"{label} must be an exact lowercase commit SHA")
    resolved = _git_text(root, "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}")
    if resolved != sha:
        raise ValueError(f"{label} does not resolve to the supplied commit")


def release_required(
    root: Path,
    *,
    event: str,
    after: str,
    published_sha: str | None = None,
) -> bool:
    """Compare committed release inputs with the last published release commit."""
    if event != "push":
        return False
    if SHA.fullmatch(after) is None:
        raise ValueError("push classification requires an exact after SHA")
    if _git_text(root, "rev-parse", "--verify", "HEAD") != after:
        raise ValueError("checked-out HEAD differs from the push commit")
    _require_commit(root, after, "push commit")
    if published_sha is None:
        return True
    _require_commit(root, published_sha, "published baseline")
    ancestor = _git(root, "merge-base", "--is-ancestor", published_sha, after)
    if ancestor.returncode != 0:
        if ancestor.returncode == 1:
            raise ValueError("published baseline is not an ancestor of the push commit")
        raise ValueError("unable to verify published baseline ancestry")
    changed = _git(
        root,
        "diff",
        "--quiet",
        "--no-ext-diff",
        "--no-textconv",
        published_sha,
        after,
        "--",
        *RELEASE_INPUTS,
    )
    if changed.returncode == 0:
        return False
    if changed.returncode == 1:
        return True
    raise ValueError("unable to compare committed distribution inputs")


def latest_published_sha(transport: Any) -> str | None:
    """Return the exact commit behind the latest stable immutable release."""
    url = publisher._api(f"/repos/{publisher.EXPECTED_REPO}/releases/latest")
    try:
        release = transport.request("GET", url)
    except publisher.GitHubHTTPError as error:
        if error.status_code == 404:
            return None
        raise
    if not isinstance(release, Mapping):
        raise publisher.PublishError("GitHub returned a malformed latest release")
    if (
        release.get("draft") is not False
        or release.get("prerelease") is not False
        or release.get("immutable") is not True
    ):
        raise publisher.PublishError("latest release is not stable and immutable")
    tag = release.get("tag_name")
    if not isinstance(tag, str):
        raise publisher.PublishError("latest release has no tag")
    match = publisher.TAG_RE.fullmatch(tag)
    if match is None:
        raise publisher.PublishError("latest release tag has an unexpected format")
    sha = publisher._resolve_tag_commit(transport, tag)
    if SHA.fullmatch(sha) is None or match.group(1) != sha[:7]:
        raise publisher.PublishError("latest release tag does not bind to its commit")
    if release.get("target_commitish") not in (
        None,
        "",
        sha,
        publisher.EXPECTED_DEFAULT_BRANCH,
    ):
        raise publisher.PublishError("latest release target commit is unexpected")
    if type(release.get("id")) is not int or release["id"] <= 0:
        raise publisher.PublishError("latest release has a malformed ID")

    assets = publisher._assets(transport, release)
    expected = {template.format(sha=sha) for template in publisher.ASSET_NAMES}
    if set(assets) != expected:
        raise publisher.PublishError("latest release has an unexpected asset set")
    for name, asset in assets.items():
        size, digest = asset.get("size"), asset.get("digest")
        if type(size) is not int or size <= 0:
            raise publisher.PublishError("latest release asset has an invalid size")
        if not isinstance(digest, str) or SHA256_DIGEST.fullmatch(digest) is None:
            raise publisher.PublishError("latest release asset has an invalid digest")
        if not publisher._asset_valid(asset, name, size, digest[7:]):
            raise publisher.PublishError(
                "latest release asset is not uploaded and bound"
            )
    return sha


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    args = parser.parse_args(argv)
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    if event != "push":
        print("release_required=false")
        return 0
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token or "\n" in token or "\r" in token:
        print(
            "release_inputs: GITHUB_TOKEN is required for push classification",
            file=sys.stderr,
        )
        return 2
    try:
        baseline = latest_published_sha(publisher.GitHubTransport(token))
        needed = release_required(
            args.repo_root,
            event=event,
            after=os.environ.get("GITHUB_SHA", ""),
            published_sha=baseline,
        )
    except (publisher.PublishError, ValueError) as error:
        print(f"release_inputs: {error}", file=sys.stderr)
        return 2
    print(f"release_required={str(needed).lower()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
