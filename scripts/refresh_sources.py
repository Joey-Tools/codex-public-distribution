#!/usr/bin/env python3
"""Refresh pinned public source commits after exact native CI verification."""

from __future__ import annotations

import argparse
import copy
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

if __package__:
    from . import source_ci
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from scripts import source_ci


LOCK_NAME = "sources.lock.json"
POLICY_NAME = "source-ci-policy.json"
MAX_INPUT_BYTES = 1024 * 1024
MAX_GITHUB_OUTPUT_BYTES = 64 * 1024
FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
ACL_XATTR_NAMES = {"system.posix_acl_access", "com.apple.acl.text"}
EXPECTED_SOURCES = {
    "codex-toolbox": ("Joey-Tools/codex-toolbox", "master"),
    "archify": ("Joey-Tools/archify", "joey-custom"),
    "codex-review-workflows": ("Joey-Tools/codex-review-workflows", "master"),
    "codex-workflow-hygiene": ("Joey-Tools/codex-workflow-hygiene", "master"),
    "codex-project-journal": ("Joey-Tools/codex-project-journal", "master"),
    "codex-debug-triage": ("Joey-Tools/codex-debug-triage", "master"),
}


class RefreshError(RuntimeError):
    """A source update could not be established and safely committed."""


@dataclass(frozen=True)
class _Snapshot:
    name: str
    fd: int
    identity: tuple[int, int, int]
    access_policy: tuple[Any, ...]
    payload: bytes


@dataclass(frozen=True)
class _DirectorySnapshot:
    path: Path
    fd: int
    identity: tuple[int, int, int]
    access_policy: tuple[Any, ...]


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON object key: {key}")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _identity(metadata: os.stat_result) -> tuple[int, int, int]:
    return metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode)


def _access_policy(fd: int, metadata: os.stat_result) -> tuple[Any, ...]:
    acl_values: list[tuple[str, bytes]] = []
    if not hasattr(os, "listxattr") or not hasattr(os, "getxattr"):
        return (
            metadata.st_uid,
            metadata.st_gid,
            stat.S_IMODE(metadata.st_mode),
            (),
        )
    try:
        names = os.listxattr(fd)
    except OSError as error:
        if error.errno not in {errno.ENOTSUP, errno.EOPNOTSUPP}:
            raise RefreshError(
                f"cannot inspect access policy ({type(error).__name__})"
            ) from None
        names = []
    for name in sorted(ACL_XATTR_NAMES.intersection(names)):
        try:
            acl_values.append((name, os.getxattr(fd, name)))
        except OSError as error:
            raise RefreshError(
                f"cannot inspect access policy ({type(error).__name__})"
            ) from None
    return (
        metadata.st_uid,
        metadata.st_gid,
        stat.S_IMODE(metadata.st_mode),
        tuple(acl_values),
    )


def _read_once(fd: int, limit: int, label: str) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    result = bytearray()
    while True:
        chunk = os.read(fd, min(65536, limit + 1 - len(result)))
        if not chunk:
            return bytes(result)
        result.extend(chunk)
        if len(result) > limit:
            raise RefreshError(f"{label} exceeds {limit} bytes")


def _stable_read(
    fd: int, limit: int, label: str
) -> tuple[bytes, tuple[int, int, int], tuple[Any, ...]]:
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode):
        raise RefreshError(f"{label} must be a regular file")
    before_identity = _identity(before)
    before_access = _access_policy(fd, before)
    first = _read_once(fd, limit, label)
    middle = os.fstat(fd)
    second = _read_once(fd, limit, label)
    after = os.fstat(fd)
    after_identity = _identity(after)
    after_access = _access_policy(fd, after)
    if (
        before_identity != _identity(middle)
        or before_identity != after_identity
        or before_access != _access_policy(fd, middle)
        or before_access != after_access
        or first != second
    ):
        raise RefreshError(f"{label} changed while being read")
    return first, after_identity, after_access


def _open_root(repo_root: str | os.PathLike[str]) -> _DirectorySnapshot:
    supplied = Path(repo_root).expanduser()
    fd: int | None = None
    try:
        if supplied.is_symlink():
            raise RefreshError("repo root must not be a symlink")
        resolved = supplied.resolve(strict=True)
        if resolved == Path(resolved.anchor):
            raise RefreshError("refusing an unsafe repo root")
        flags = (
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        if not getattr(os, "O_DIRECTORY", 0) or not getattr(os, "O_NOFOLLOW", 0):
            raise RefreshError("safe repo-root directory open is unavailable")
        fd = os.open(resolved, flags)
        metadata = os.fstat(fd)
        path_metadata = os.stat(resolved, follow_symlinks=False)
        if not stat.S_ISDIR(metadata.st_mode) or _identity(metadata) != _identity(
            path_metadata
        ):
            raise RefreshError("repo root changed while being opened")
        root = _DirectorySnapshot(
            resolved,
            fd,
            _identity(metadata),
            _access_policy(fd, metadata),
        )
        fd = None
        return root
    except RefreshError:
        if fd is not None:
            os.close(fd)
        raise
    except OSError as error:
        if fd is not None:
            os.close(fd)
        raise RefreshError(f"cannot open repo root ({type(error).__name__})") from None


def _assert_root_unchanged(root: _DirectorySnapshot) -> None:
    try:
        metadata = os.stat(root.path, follow_symlinks=False)
        fd_metadata = os.fstat(root.fd)
        if (
            stat.S_ISLNK(metadata.st_mode)
            or _identity(metadata) != root.identity
            or _identity(fd_metadata) != root.identity
            or _access_policy(root.fd, fd_metadata) != root.access_policy
        ):
            raise RefreshError("repo root object or access policy changed")
    except RefreshError:
        raise
    except OSError as error:
        raise RefreshError(
            f"cannot recheck repo root ({type(error).__name__})"
        ) from None


def _open_snapshot(root: _DirectorySnapshot, name: str) -> _Snapshot:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        raise RefreshError("safe source-lock open is unavailable")
    fd: int | None = None
    try:
        fd = os.open(name, os.O_RDONLY | nofollow, dir_fd=root.fd)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RefreshError(
                f"cannot acquire {name} stability guard ({type(error).__name__})"
            ) from None
        payload, identity, access_policy = _stable_read(fd, MAX_INPUT_BYTES, name)
        return _Snapshot(name, fd, identity, access_policy, payload)
    except RefreshError:
        if fd is not None:
            os.close(fd)
        raise
    except OSError as error:
        if fd is not None:
            os.close(fd)
        label = "source lock" if name == LOCK_NAME else "source CI policy"
        raise RefreshError(
            f"cannot safely open {label} ({type(error).__name__})"
        ) from None


def _assert_snapshot_unchanged(root: _DirectorySnapshot, snapshot: _Snapshot) -> None:
    _assert_root_unchanged(root)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(snapshot.name, os.O_RDONLY | nofollow, dir_fd=root.fd)
        try:
            payload, identity, access_policy = _stable_read(
                fd, MAX_INPUT_BYTES, snapshot.name
            )
        finally:
            os.close(fd)
    except RefreshError:
        raise
    except OSError as error:
        raise RefreshError(
            f"cannot recheck {snapshot.name} ({type(error).__name__})"
        ) from None
    if identity != snapshot.identity:
        raise RefreshError(f"{snapshot.name} object was replaced")
    if access_policy != snapshot.access_policy:
        raise RefreshError(f"{snapshot.name} access policy changed")
    if payload != snapshot.payload:
        raise RefreshError(f"{snapshot.name} content changed")


def _parse_document(snapshot: _Snapshot) -> dict[str, Any]:
    try:
        parsed = json.loads(
            snapshot.payload.decode("utf-8"),
            object_pairs_hook=_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise RefreshError(f"{snapshot.name} is not valid bounded JSON") from None
    if not isinstance(parsed, dict):
        raise RefreshError(f"{snapshot.name} must contain a JSON object")
    return parsed


def _load_contract(
    lock_snapshot: _Snapshot, policy_snapshot: _Snapshot
) -> tuple[dict[str, Any], dict[str, Any], dict[str, dict[str, Any]]]:
    lock = _parse_document(lock_snapshot)
    policy_document = _parse_document(policy_snapshot)
    if lock.get("version") != 1 or policy_document.get("version") != 1:
        raise RefreshError("unsupported source lock or source CI policy version")
    sources = lock.get("sources")
    policies = policy_document.get("sources")
    if not isinstance(sources, list) or not isinstance(policies, dict):
        raise RefreshError("malformed source lock or source CI policy")
    if len(sources) != len(EXPECTED_SOURCES):
        raise RefreshError("source lock must contain the six fixed sources")

    by_name: dict[str, dict[str, Any]] = {}
    for row in sources:
        locked = source_ci._obj(row, "source lock entry")
        name = source_ci._nonempty(locked.get("name"), "source name")
        if name in by_name:
            raise RefreshError("source lock contains duplicate source names")
        expected = EXPECTED_SOURCES.get(name)
        if (
            expected is None
            or (locked.get("repository"), locked.get("ref")) != expected
        ):
            raise RefreshError(f"source identity is not fixed for {name}")
        by_name[name] = locked
    if set(by_name) != set(EXPECTED_SOURCES) or set(policies) != set(EXPECTED_SOURCES):
        raise RefreshError(
            "source lock and CI policy must contain the six fixed sources"
        )
    for name, locked in by_name.items():
        try:
            source_ci._validate_source_pair(
                locked, source_ci._obj(policies[name], f"policy for {name}")
            )
        except source_ci.VerificationError as error:
            raise RefreshError(f"invalid source contract for {name}: {error}") from None
    return lock, policy_document, by_name


def _resolve_pin(api: Any, locked: dict[str, Any]) -> tuple[str, str]:
    name = source_ci._nonempty(locked.get("name"), "source name")
    repository = locked["repository"]
    ref = locked["ref"]
    repo = source_ci._obj(source_ci._get(api, repository, ""), "repository metadata")
    if (
        repo.get("id") != locked.get("repository_id")
        or repo.get("full_name") != repository
    ):
        raise RefreshError(f"repository identity changed for {name}")
    if repo.get("private") is not False or repo.get("archived") is True:
        raise RefreshError(f"source is not an active public repository: {name}")
    if repo.get("default_branch") != ref:
        raise RefreshError(f"source ref is no longer the default branch: {name}")

    branch = source_ci._obj(
        source_ci._get(api, repository, f"/branches/{quote(ref, safe='')}"),
        "source branch metadata",
    )
    if branch.get("name") != ref:
        raise RefreshError(f"source branch name does not match the locked ref: {name}")
    branch_commit = source_ci._obj(branch.get("commit"), "source branch commit")
    commit = source_ci._obj(branch_commit.get("commit"), "source branch commit data")
    tree = source_ci._obj(commit.get("tree"), "source ref tree")
    sha_value, tree_value = branch_commit.get("sha"), tree.get("sha")
    if not isinstance(sha_value, str) or not FULL_SHA_RE.fullmatch(sha_value):
        raise RefreshError(f"source ref did not resolve to a full commit SHA: {name}")
    if not isinstance(tree_value, str) or not FULL_SHA_RE.fullmatch(tree_value):
        raise RefreshError(f"source ref did not resolve to a full tree SHA: {name}")
    return sha_value, tree_value


def _without_mutable_pins(lock: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(lock)
    for row in result["sources"]:
        row.pop("sha", None)
        row.pop("tree", None)
    return result


def _validate_candidate_metadata(
    original: dict[str, Any], candidate: dict[str, Any]
) -> None:
    if _without_mutable_pins(original) != _without_mutable_pins(candidate):
        raise RefreshError("candidate changed source metadata outside sha/tree")


def _verify_candidates(
    api: Any,
    candidate: dict[str, Any],
    policies: dict[str, dict[str, Any]],
) -> None:
    for row in candidate["sources"]:
        name = row["name"]
        try:
            evidence = source_ci._verify_source(api, row, policies[name])
        except source_ci.VerificationError as error:
            raise RefreshError(
                f"native CI verification failed for {name}: {error}"
            ) from None
        except Exception as error:
            raise RefreshError(
                f"native CI verification has unknown evidence for {name} ({type(error).__name__})"
            ) from None
        if not isinstance(evidence, dict) or evidence.get("status") != "verified":
            raise RefreshError(
                f"native CI verification has unknown evidence for {name}"
            )


def _copy_acl_xattrs(source_fd: int, destination_fd: int) -> None:
    if not all(
        hasattr(os, name)
        for name in ("listxattr", "getxattr", "setxattr", "removexattr")
    ):
        return
    try:
        source_names = set(os.listxattr(source_fd))
        destination_names = set(os.listxattr(destination_fd))
    except OSError as error:
        if error.errno in {errno.ENOTSUP, errno.EOPNOTSUPP}:
            return
        raise RefreshError(
            f"cannot preserve source-lock access policy ({type(error).__name__})"
        ) from None
    source_acl = ACL_XATTR_NAMES.intersection(source_names)
    for name in ACL_XATTR_NAMES.intersection(destination_names) - source_acl:
        try:
            os.removexattr(destination_fd, name)
        except OSError as error:
            raise RefreshError(
                f"cannot preserve source-lock access policy ({type(error).__name__})"
            ) from None
    for name in source_acl:
        try:
            os.setxattr(destination_fd, name, os.getxattr(source_fd, name))
        except OSError as error:
            raise RefreshError(
                f"cannot preserve source-lock access policy ({type(error).__name__})"
            ) from None


def _write_all(fd: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        offset += os.write(fd, payload[offset:])


def _atomic_replace(
    root: _DirectorySnapshot,
    lock_snapshot: _Snapshot,
    policy_snapshot: _Snapshot,
    payload: bytes,
) -> None:
    temp_name = f".{LOCK_NAME}.{secrets.token_hex(8)}.tmp"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        fd = os.open(temp_name, flags, 0o600, dir_fd=root.fd)
    except OSError as error:
        raise RefreshError(
            f"cannot create atomic source-lock candidate ({type(error).__name__})"
        ) from None
    try:
        try:
            metadata = os.fstat(fd)
            if (metadata.st_uid, metadata.st_gid) != lock_snapshot.access_policy[:2]:
                os.fchown(
                    fd, lock_snapshot.access_policy[0], lock_snapshot.access_policy[1]
                )
            _copy_acl_xattrs(lock_snapshot.fd, fd)
            os.fchmod(fd, lock_snapshot.access_policy[2])
            _write_all(fd, payload)
            os.fsync(fd)
            metadata = os.fstat(fd)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or _access_policy(fd, metadata) != lock_snapshot.access_policy
            ):
                raise RefreshError(
                    "atomic candidate access policy does not match source lock"
                )

            _assert_snapshot_unchanged(root, lock_snapshot)
            _assert_snapshot_unchanged(root, policy_snapshot)
            os.replace(
                temp_name,
                LOCK_NAME,
                src_dir_fd=root.fd,
                dst_dir_fd=root.fd,
            )
            os.fsync(root.fd)
        except RefreshError:
            raise
        except OSError as error:
            raise RefreshError(
                f"cannot atomically replace source lock ({type(error).__name__})"
            ) from None
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temp_name, dir_fd=root.fd)
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _render_candidate(candidate: dict[str, Any]) -> bytes:
    try:
        return (
            json.dumps(candidate, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise RefreshError("cannot serialize source-lock candidate") from None


def refresh_sources(repo_root: str | os.PathLike[str], api: Any) -> dict[str, Any]:
    """Refresh only commit/tree pins, after every candidate passes native CI."""
    root = _open_root(repo_root)
    lock_snapshot: _Snapshot | None = None
    policy_snapshot: _Snapshot | None = None
    try:
        lock_snapshot = _open_snapshot(root, LOCK_NAME)
        policy_snapshot = _open_snapshot(root, POLICY_NAME)
        lock, policy_document, locked_by_name = _load_contract(
            lock_snapshot, policy_snapshot
        )
        policies = policy_document["sources"]

        resolved_pins: dict[str, tuple[str, str]] = {}
        for name in [row["name"] for row in lock["sources"]]:
            try:
                resolved_pins[name] = _resolve_pin(api, locked_by_name[name])
            except source_ci.VerificationError as error:
                raise RefreshError(f"cannot resolve source {name}: {error}") from None

        changed_names = [
            name
            for name, source in locked_by_name.items()
            if (source["sha"], source["tree"]) != resolved_pins[name]
        ]
        _assert_snapshot_unchanged(root, lock_snapshot)
        _assert_snapshot_unchanged(root, policy_snapshot)

        if not changed_names:
            return {
                "changed": False,
                "candidate_lock_sha256": hashlib.sha256(
                    lock_snapshot.payload
                ).hexdigest(),
            }

        candidate = copy.deepcopy(lock)
        candidate_by_name = {row["name"]: row for row in candidate["sources"]}
        for name in changed_names:
            candidate_by_name[name]["sha"], candidate_by_name[name]["tree"] = (
                resolved_pins[name]
            )
        _validate_candidate_metadata(lock, candidate)
        for row in candidate["sources"]:
            try:
                source_ci._validate_source_pair(
                    row, source_ci._obj(policies[row["name"]], "source CI policy")
                )
            except source_ci.VerificationError as error:
                raise RefreshError(
                    f"candidate source contract changed for {row['name']}: {error}"
                ) from None

        _verify_candidates(api, candidate, policies)
        _assert_snapshot_unchanged(root, lock_snapshot)
        _assert_snapshot_unchanged(root, policy_snapshot)

        payload = _render_candidate(candidate)
        _atomic_replace(root, lock_snapshot, policy_snapshot, payload)
        return {
            "changed": True,
            "candidate_lock_sha256": hashlib.sha256(payload).hexdigest(),
        }
    except source_ci.VerificationError as error:
        raise RefreshError(str(error)) from None
    finally:
        for snapshot in (lock_snapshot, policy_snapshot):
            if snapshot is not None:
                try:
                    os.close(snapshot.fd)
                except OSError:
                    pass
        try:
            os.close(root.fd)
        except OSError:
            pass


def _write_github_output(path: str, result: dict[str, Any], repo_root: str) -> None:
    output_path = Path(path).expanduser()
    lock_path = (Path(repo_root).expanduser().resolve() / LOCK_NAME).resolve()
    policy_path = (Path(repo_root).expanduser().resolve() / POLICY_NAME).resolve()
    try:
        if output_path.is_symlink():
            raise RefreshError("GitHub output path must not be a symlink")
        if output_path.resolve(strict=False) in {lock_path, policy_path}:
            raise RefreshError("GitHub output path must not overwrite source inputs")
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(output_path, flags, 0o600)
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise RefreshError("GitHub output target must be a regular file")
            if metadata.st_size > MAX_GITHUB_OUTPUT_BYTES:
                raise RefreshError(
                    "GitHub output target exceeds the configured size limit"
                )
            content = (
                f"changed={'true' if result['changed'] else 'false'}\n"
                f"candidate_lock_sha256={result['candidate_lock_sha256']}\n"
            ).encode("ascii")
            _write_all(fd, content)
            os.fsync(fd)
        finally:
            os.close(fd)
    except RefreshError:
        raise
    except OSError as error:
        raise RefreshError(
            f"cannot write GitHub output ({type(error).__name__})"
        ) from None


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--github-output")
    args = parser.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN")
    if token is not None and ("\n" in token or "\r" in token):
        print("GITHUB_TOKEN contains invalid line breaks", file=sys.stderr)
        return 2
    try:
        result = refresh_sources(args.repo_root, source_ci.GitHubAPI(token))
        if args.github_output:
            _write_github_output(args.github_output, result, args.repo_root)
    except RefreshError as error:
        print(f"source-pin refresh failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
