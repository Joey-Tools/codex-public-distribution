#!/usr/bin/env python3
"""Build a deterministic public bundle from pinned public Git objects."""

from __future__ import annotations

import argparse
import ast
import ctypes
import hashlib
import importlib.util
import inspect
import json
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import stat
import subprocess
import sys
import tempfile
import time
from types import ModuleType
import unicodedata
from typing import Any


MAX_CONFIG_BYTES = 1024 * 1024
MAX_TREE_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_GIT_STDERR_BYTES = 64 * 1024
MAX_EXPORT_FILES = 10_000
MAX_EXPORT_FILE_BYTES = 32 * 1024 * 1024
MAX_EXPORT_TOTAL_BYTES = 256 * 1024 * 1024
MAX_GIT_COMMAND_SECONDS = 30.0
FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SOURCE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
REGULAR_GIT_MODES = {b"100644", b"100755"}
BUILDER_MANIFEST = PurePosixPath("personal_codex/public-sync-manifest.json")
PROVENANCE_PATH = PurePosixPath("personal_codex/distribution-provenance.json")
SOURCE_LOCK_PATH = PurePosixPath("personal_codex/distribution-sources.lock.json")
AGGREGATOR_LICENSE_PATH = PurePosixPath("personal_codex/AGGREGATOR-LICENSE.txt")
REFERENCE_ONLY_PATHS = [
    PROVENANCE_PATH.as_posix(),
    SOURCE_LOCK_PATH.as_posix(),
    AGGREGATOR_LICENSE_PATH.as_posix(),
]


class DistributionError(RuntimeError):
    """Raised when pinned source data cannot safely produce a public bundle."""


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _read_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise DistributionError(f"cannot inspect {label} {path}: {error}") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise DistributionError(f"{label} must be a regular file: {path}")
    if metadata.st_size > MAX_CONFIG_BYTES:
        raise DistributionError(f"{label} exceeds {MAX_CONFIG_BYTES} bytes: {path}")
    try:
        payload = path.read_bytes()
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_json_object,
            parse_constant=lambda item: (_ for _ in ()).throw(
                ValueError(f"invalid JSON constant: {item}")
            ),
        )
    except (OSError, UnicodeDecodeError, ValueError, RecursionError) as error:
        raise DistributionError(f"cannot read valid {label} {path}: {error}") from error
    if not isinstance(value, dict):
        raise DistributionError(f"{label} must contain a JSON object: {path}")
    return value, payload


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def _safe_relative(
    raw: object, label: str, *, allow_dot: bool = False
) -> PurePosixPath:
    if not isinstance(raw, str) or not raw:
        raise DistributionError(f"{label} must be a non-empty relative POSIX path")
    try:
        raw.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise DistributionError(f"{label} is not valid UTF-8") from error
    if raw == "." and allow_dot:
        return PurePosixPath(".")
    if (
        raw.startswith("/")
        or "\\" in raw
        or "\0" in raw
        or any(part in {"", ".", ".."} for part in raw.split("/"))
    ):
        raise DistributionError(f"unsafe {label}: {raw!r}")
    path = PurePosixPath(raw)
    if path.is_absolute() or not path.parts:
        raise DistributionError(f"unsafe {label}: {raw!r}")
    return path


def _portable_key(path: PurePosixPath) -> tuple[str, ...]:
    return tuple(unicodedata.normalize("NFC", part).casefold() for part in path.parts)


def _is_prefix(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    return len(left) <= len(right) and right[: len(left)] == left


def _validate_distinct_paths(
    paths: list[tuple[PurePosixPath, str]], label: str
) -> None:
    seen: list[tuple[tuple[str, ...], str]] = []
    directory_spellings: dict[tuple[str, ...], tuple[str, ...]] = {}
    for path, owner in paths:
        key = _portable_key(path)
        for prior_key, prior_owner in seen:
            if _is_prefix(prior_key, key) or _is_prefix(key, prior_key):
                raise DistributionError(
                    f"overlapping {label}: {prior_owner} ({path.as_posix()}) and {owner}"
                )
        seen.append((key, owner))
        for length in range(1, len(path.parts) + 1):
            prefix = tuple(path.parts[:length])
            prefix_key = tuple(
                unicodedata.normalize("NFC", part).casefold() for part in prefix
            )
            prior_spelling = directory_spellings.get(prefix_key)
            if prior_spelling is not None and prior_spelling != prefix:
                raise DistributionError(
                    f"case or Unicode-colliding {label}: {'/'.join(prior_spelling)} and {'/'.join(prefix)}"
                )
            directory_spellings[prefix_key] = prefix


def _validate_config(
    config: dict[str, Any], lock: dict[str, Any], revision: str
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any]]:
    if config.get("version") != 1:
        raise DistributionError("distribution config must use version 1")
    if not isinstance(config.get("repository"), str) or not config["repository"]:
        raise DistributionError("distribution config must name its public repository")
    if FULL_SHA_RE.fullmatch(revision) is None:
        raise DistributionError(
            "aggregator revision must be 40 lowercase hexadecimal characters"
        )
    if lock.get("version") != 1:
        raise DistributionError("source lock must use version 1")

    raw_sources = lock.get("sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise DistributionError("source lock must contain a non-empty sources array")
    sources: dict[str, dict[str, Any]] = {}
    for item in raw_sources:
        if not isinstance(item, dict):
            raise DistributionError("each source lock entry must be an object")
        name = item.get("name")
        if not isinstance(name, str) or SOURCE_NAME_RE.fullmatch(name) is None:
            raise DistributionError(f"invalid source name in source lock: {name!r}")
        if name in sources:
            raise DistributionError(f"duplicate source lock name: {name}")
        if not isinstance(item.get("repository"), str) or not item["repository"]:
            raise DistributionError(f"source {name} must name a repository")
        if not isinstance(item.get("ref"), str) or not item["ref"]:
            raise DistributionError(f"source {name} must name a ref")
        if FULL_SHA_RE.fullmatch(item.get("sha", "")) is None:
            raise DistributionError(
                f"source {name} must pin a full lowercase commit SHA"
            )
        if FULL_SHA_RE.fullmatch(item.get("tree", "")) is None:
            raise DistributionError(f"source {name} must pin a full lowercase tree SHA")
        sources[name] = item

    raw_builder = config.get("builder")
    if not isinstance(raw_builder, dict):
        raise DistributionError(
            "distribution config must define a public package builder"
        )
    builder_source = raw_builder.get("source")
    if builder_source not in sources:
        raise DistributionError("package builder source must exist in the source lock")
    builder_path = _safe_relative(raw_builder.get("path"), "builder path")

    raw_exports = config.get("exports")
    if not isinstance(raw_exports, list) or not raw_exports:
        raise DistributionError(
            "distribution config must contain a non-empty exports array"
        )
    exports: list[dict[str, Any]] = []
    used_sources: set[str] = set()
    destination_roots: list[tuple[PurePosixPath, str]] = []
    target_roots: list[tuple[PurePosixPath, str]] = []
    for index, raw_export in enumerate(raw_exports):
        if not isinstance(raw_export, dict):
            raise DistributionError(f"export {index} must be an object")
        source = raw_export.get("source")
        if not isinstance(source, str) or source not in sources:
            raise DistributionError(
                f"export {index} names an unlocked source: {source!r}"
            )
        used_sources.add(source)
        source_path = _safe_relative(
            raw_export.get("path"), f"export {index} source path", allow_dot=True
        )
        destination = _safe_relative(
            raw_export.get("destination"), f"export {index} destination"
        )
        target = _safe_relative(raw_export.get("target"), f"export {index} target")
        kind = raw_export.get("kind")
        if kind not in {"file", "skill"}:
            raise DistributionError(
                f"unsupported export kind in export {index}: {kind!r}"
            )
        toolbox_runtime_export = (
            source == "codex-toolbox"
            and source_path == PurePosixPath("scripts/codex_personal_sync.py")
            and destination == PurePosixPath("scripts/codex_personal_sync.py")
            and target == PurePosixPath("bin/codex-personal-sync")
            and kind == "file"
        )
        if destination.parts[0] != "personal_codex" and not toolbox_runtime_export:
            raise DistributionError(
                f"export destination must be under personal_codex/: {destination}"
            )
        excludes = raw_export.get("exclude", [])
        if not isinstance(excludes, list):
            raise DistributionError(f"export {index} exclude must be an array")
        checked_excludes: list[str] = []
        for pattern in excludes:
            if not isinstance(pattern, str) or not pattern:
                raise DistributionError(
                    f"export {index} has an invalid exclude pattern"
                )
            _safe_relative(pattern, f"export {index} exclude pattern")
            checked_excludes.append(pattern)
        transform = raw_export.get("transform")
        if transform is not None and transform != "archify-standalone-v1":
            raise DistributionError(f"unsupported export transform: {transform!r}")
        if transform and kind != "skill":
            raise DistributionError("file exports cannot use a tree transform")
        export = {
            "source": source,
            "path": source_path,
            "destination": destination,
            "target": target,
            "kind": kind,
            "exclude": checked_excludes,
            "transform": transform,
        }
        exports.append(export)
        destination_roots.append((destination, f"export {index}"))
        target_roots.append((target, f"export {index}"))

    if used_sources != set(sources):
        unused = sorted(set(sources) - used_sources)
        missing = sorted(used_sources - set(sources))
        raise DistributionError(
            f"source lock and exports must name the same sources (unused={unused}, missing={missing})"
        )
    _validate_distinct_paths(destination_roots, "destination roots")
    _validate_distinct_paths(target_roots, "install targets")

    _safe_relative(config.get("repository"), "repository name")
    builder = {"source": builder_source, "path": builder_path}
    return exports, sources, builder


def _git_environment() -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
            "LANG": "C",
        }
    )
    return environment


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        process.wait()
        return
    try:
        process.kill()
    except ProcessLookupError:
        pass
    process.wait()


def _run_bounded(
    command: list[str],
    cwd: Path,
    *,
    stdout_limit: int,
    stdin_payload: bytes | None = None,
    timeout: float = MAX_GIT_COMMAND_SECONDS,
) -> bytes:
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=_git_environment(),
            stdin=subprocess.PIPE if stdin_payload is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
    except OSError as error:
        raise DistributionError(
            f"cannot start Git command {command[1:]}: {error}"
        ) from error

    selector = selectors.DefaultSelector()
    stdout = bytearray()
    stderr = bytearray()
    input_offset = 0
    try:
        assert process.stdout is not None and process.stderr is not None
        os.set_blocking(process.stdout.fileno(), False)
        os.set_blocking(process.stderr.fileno(), False)
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        if stdin_payload is not None:
            assert process.stdin is not None
            os.set_blocking(process.stdin.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
        deadline = time.monotonic() + timeout
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DistributionError(f"Git command exceeded {timeout:g} seconds")
            events = selector.select(remaining)
            if not events:
                raise DistributionError(f"Git command exceeded {timeout:g} seconds")
            for key, _mask in events:
                if key.data == "stdin":
                    assert process.stdin is not None and stdin_payload is not None
                    if input_offset >= len(stdin_payload):
                        selector.unregister(process.stdin)
                        process.stdin.close()
                        continue
                    try:
                        input_offset += os.write(
                            process.stdin.fileno(),
                            stdin_payload[input_offset : input_offset + 65536],
                        )
                    except BrokenPipeError:
                        selector.unregister(process.stdin)
                        process.stdin.close()
                    continue

                target = stdout if key.data == "stdout" else stderr
                cap = stdout_limit if key.data == "stdout" else MAX_GIT_STDERR_BYTES
                try:
                    chunk = os.read(key.fileobj.fileno(), 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                target.extend(chunk)
                if len(target) > cap:
                    label = "stdout" if key.data == "stdout" else "stderr"
                    raise DistributionError(
                        f"Git command exceeded its {label} output limit"
                    )

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DistributionError(f"Git command exceeded {timeout:g} seconds")
        try:
            return_code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as error:
            raise DistributionError(
                f"Git command exceeded {timeout:g} seconds"
            ) from error
        if return_code != 0:
            detail = bytes(stderr).decode("utf-8", errors="replace").strip()
            if not detail:
                detail = bytes(stdout).decode("utf-8", errors="replace").strip()
            raise DistributionError(
                f"Git command failed ({return_code}): {' '.join(command[1:])}: {detail}"
            )
        return bytes(stdout)
    except BaseException:
        _terminate_process(process)
        raise
    finally:
        selector.close()
        if process.stdin is not None:
            process.stdin.close()
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()


def _git(repo_root: Path, *args: str, stdout_limit: int = 64 * 1024) -> bytes:
    return _run_bounded(
        ["git", "-C", str(repo_root), *args],
        repo_root,
        stdout_limit=stdout_limit,
    )


def _decode_git_line(payload: bytes, label: str) -> str:
    try:
        value = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise DistributionError(f"Git returned non-UTF-8 {label}") from error
    return value.rstrip("\n")


def _source_root(sources_root: Path, name: str) -> Path:
    candidate = sources_root / name
    try:
        metadata = candidate.lstat()
        resolved = candidate.resolve(strict=True)
        resolved_parent = resolved.parent
        if not stat.S_ISDIR(metadata.st_mode) or resolved_parent != sources_root:
            raise DistributionError(
                f"source checkout must be a direct non-symlink directory: {candidate}"
            )
    except OSError as error:
        raise DistributionError(
            f"source checkout is missing or unreadable: {candidate}: {error}"
        ) from error
    top_level = _decode_git_line(
        _git(resolved, "rev-parse", "--show-toplevel", stdout_limit=16 * 1024),
        "repository root",
    )
    try:
        actual_top = Path(top_level).resolve(strict=True)
    except OSError as error:
        raise DistributionError(
            f"cannot resolve Git worktree root for {candidate}: {error}"
        ) from error
    if actual_top != resolved:
        raise DistributionError(f"source path is not the checkout root: {candidate}")
    object_format = _decode_git_line(
        _git(resolved, "rev-parse", "--show-object-format", stdout_limit=128),
        "object format",
    )
    if object_format != "sha1":
        raise DistributionError(
            f"source checkout must use SHA-1 Git objects: {candidate}"
        )
    return resolved


def _verify_source_commit(root: Path, source: dict[str, Any]) -> None:
    name = source["name"]
    sha = source["sha"]
    resolved_commit = _decode_git_line(
        _git(
            root,
            "rev-parse",
            "--verify",
            "--quiet",
            f"{sha}^{{commit}}",
            stdout_limit=128,
        ),
        f"{name} commit",
    )
    if resolved_commit != sha:
        raise DistributionError(
            f"source {name} does not resolve to the pinned commit {sha}"
        )
    resolved_tree = _decode_git_line(
        _git(
            root,
            "rev-parse",
            "--verify",
            "--quiet",
            f"{sha}^{{tree}}",
            stdout_limit=128,
        ),
        f"{name} tree",
    )
    if resolved_tree != source["tree"]:
        raise DistributionError(
            f"source {name} tree mismatch: expected {source['tree']}, found {resolved_tree}"
        )


def _parse_tree(raw: bytes, source_name: str) -> dict[str, tuple[bytes, bytes, str]]:
    entries: dict[str, tuple[bytes, bytes, str]] = {}
    if raw and not raw.endswith(b"\0"):
        raise DistributionError(
            f"Git returned a truncated tree inventory for {source_name}"
        )
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, object_type, object_id = metadata.split(b" ", 2)
        except ValueError as error:
            raise DistributionError(
                f"Git returned an invalid tree record for {source_name}"
            ) from error
        try:
            path = raw_path.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise DistributionError(
                f"source {source_name} contains a non-UTF-8 Git path"
            ) from error
        relative = _safe_relative(path, f"source {source_name} Git path")
        if relative.as_posix() != path:
            raise DistributionError(
                f"source {source_name} contains a non-canonical Git path: {path!r}"
            )
        if path in entries:
            raise DistributionError(
                f"source {source_name} contains a duplicate Git path: {path}"
            )
        try:
            oid = object_id.decode("ascii")
        except UnicodeDecodeError as error:
            raise DistributionError(
                f"source {source_name} contains a non-ASCII object ID"
            ) from error
        if FULL_SHA_RE.fullmatch(oid) is None:
            raise DistributionError(
                f"source {source_name} contains an invalid object ID: {oid!r}"
            )
        entries[path] = (mode, object_type, oid)
    return entries


def _source_inventory(
    root: Path, source: dict[str, Any]
) -> dict[str, tuple[bytes, bytes, str]]:
    _verify_source_commit(root, source)
    raw = _git(
        root,
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        source["sha"],
        stdout_limit=MAX_TREE_OUTPUT_BYTES,
    )
    return _parse_tree(raw, source["name"])


def _glob_regex(pattern: str) -> re.Pattern[str]:
    result = ["^"]
    index = 0
    while index < len(pattern):
        character = pattern[index]
        if character == "*":
            if index + 1 < len(pattern) and pattern[index + 1] == "*":
                if index + 2 < len(pattern) and pattern[index + 2] == "/":
                    result.append("(?:.*/)?")
                    index += 3
                    continue
                result.append(".*")
                index += 2
                continue
            result.append("[^/]*")
        elif character == "?":
            result.append("[^/]")
        elif character == "[":
            closing = pattern.find("]", index + 1)
            if closing < 0:
                result.append(r"\[")
            else:
                content = pattern[index + 1 : closing]
                if content.startswith("!"):
                    content = "^" + content[1:]
                result.append("[" + content.replace("\\", r"\\") + "]")
                index = closing
        else:
            result.append(re.escape(character))
        index += 1
    result.append("$")
    return re.compile("".join(result))


def _is_excluded(relative: PurePosixPath, patterns: list[str]) -> bool:
    path = relative.as_posix()
    for pattern in patterns:
        if _glob_regex(pattern).fullmatch(path):
            return True
        if pattern.endswith("/**"):
            directory = pattern[:-3].rstrip("/")
            if path == directory or path.startswith(directory + "/"):
                return True
    return False


def _select_export(
    export: dict[str, Any], inventory: dict[str, tuple[bytes, bytes, str]]
) -> list[tuple[PurePosixPath, str, bytes, str]]:
    source_path = export["path"]
    prefix = "" if source_path == PurePosixPath(".") else source_path.as_posix()
    exact = inventory.get(prefix) if prefix else None
    if export["kind"] == "file":
        if exact is None:
            raise DistributionError(
                f"configured file source is missing: {export['source']}:{prefix}"
            )
        mode, object_type, oid = exact
        if object_type != b"blob" or mode not in REGULAR_GIT_MODES:
            raise DistributionError(
                f"configured file source is not a regular Git blob: {prefix}"
            )
        if _is_excluded(PurePosixPath(Path(prefix).name), export["exclude"]):
            raise DistributionError(
                f"configured file source is excluded: {export['source']}:{prefix}"
            )
        return [(PurePosixPath(Path(prefix).name), prefix, mode, oid)]

    if prefix and exact is not None:
        raise DistributionError(
            f"configured skill source is not a Git directory: {prefix}"
        )
    start = prefix + "/" if prefix else ""
    selected: list[tuple[PurePosixPath, str, bytes, str]] = []
    for path in sorted(inventory):
        if not path.startswith(start):
            continue
        relative_text = path[len(start) :]
        if not relative_text or any(
            part == ".git" for part in PurePosixPath(relative_text).parts
        ):
            raise DistributionError(
                f"refusing Git metadata path in export {export['source']}: {path}"
            )
        relative = PurePosixPath(relative_text)
        if _is_excluded(relative, export["exclude"]):
            continue
        mode, object_type, oid = inventory[path]
        if object_type != b"blob" or mode not in REGULAR_GIT_MODES:
            raise DistributionError(
                f"refusing symlink, gitlink, or unsupported Git entry in export {export['source']}: {path}"
            )
        selected.append((relative, path, mode, oid))
    if not selected:
        raise DistributionError(
            f"configured skill source is empty after exclusions: {export['source']}:{prefix or '.'}"
        )
    if not any(
        relative == PurePosixPath("SKILL.md")
        for relative, _path, _mode, _oid in selected
    ):
        raise DistributionError(
            f"configured skill lacks SKILL.md: {export['source']}:{prefix or '.'}"
        )
    return selected


def _cat_file_batch(root: Path, object_ids: list[str]) -> dict[str, bytes]:
    if not object_ids:
        return {}
    request = ("".join(f"{oid}\n" for oid in object_ids)).encode("ascii")
    size_output = _run_bounded(
        ["git", "-C", str(root), "cat-file", "--batch-check"],
        root,
        stdout_limit=min(4 * 1024 * 1024, len(object_ids) * 128 + 1),
        stdin_payload=request,
    )
    sizes: dict[str, int] = {}
    total = 0
    lines = size_output.splitlines()
    if len(lines) != len(object_ids):
        raise DistributionError("Git returned an incomplete batch object size response")
    for expected, line in zip(object_ids, lines, strict=True):
        fields = line.split(b" ")
        if len(fields) != 3 or fields[0].decode("ascii", errors="replace") != expected:
            raise DistributionError(
                "Git returned an invalid batch object size response"
            )
        if fields[1] != b"blob" or not fields[2].isdigit():
            raise DistributionError(f"selected Git object is not a blob: {expected}")
        size = int(fields[2])
        if size > MAX_EXPORT_FILE_BYTES:
            raise DistributionError(
                f"Git blob exceeds per-file export limit ({size} > {MAX_EXPORT_FILE_BYTES}): {expected}"
            )
        total += size
        if total > MAX_EXPORT_TOTAL_BYTES:
            raise DistributionError(
                "selected Git blobs exceed the total export size limit"
            )
        sizes[expected] = size

    output_limit = total + len(object_ids) * 96 + 1
    output = _run_bounded(
        ["git", "-C", str(root), "cat-file", "--batch"],
        root,
        stdout_limit=output_limit,
        stdin_payload=request,
    )
    results: dict[str, bytes] = {}
    offset = 0
    for expected in object_ids:
        newline = output.find(b"\n", offset)
        if newline < 0:
            raise DistributionError("Git returned a truncated batch object header")
        fields = output[offset:newline].split(b" ")
        if (
            len(fields) != 3
            or fields[0].decode("ascii", errors="replace") != expected
            or fields[1] != b"blob"
            or not fields[2].isdigit()
            or int(fields[2]) != sizes[expected]
        ):
            raise DistributionError(
                f"Git returned an invalid batch object header: {expected}"
            )
        start = newline + 1
        end = start + sizes[expected]
        if end >= len(output) or output[end : end + 1] != b"\n":
            raise DistributionError(
                f"Git returned truncated object content: {expected}"
            )
        data = output[start:end]
        header = b"blob " + str(len(data)).encode("ascii") + b"\0"
        try:
            actual_oid = hashlib.sha1(header + data, usedforsecurity=False).hexdigest()
        except (TypeError, ValueError):
            actual_oid = hashlib.sha1(header + data).hexdigest()
        if actual_oid != expected:
            raise DistributionError(
                f"Git object bytes do not match object ID {expected}"
            )
        results[expected] = data
        offset = end + 1
    if offset != len(output):
        raise DistributionError("Git returned unexpected trailing batch object data")
    return results


def _load_archify_adapter() -> tuple[str, Any]:
    adapter_path = Path(__file__).with_name("archify_export.py")
    if not adapter_path.is_file():
        raise DistributionError(
            f"configured Archify transform adapter is missing: {adapter_path}"
        )
    spec = importlib.util.spec_from_file_location(
        "_distribution_archify_export", adapter_path
    )
    if spec is None or spec.loader is None:
        raise DistributionError("cannot load the Archify transform adapter")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as error:
        raise DistributionError(
            f"cannot initialize the Archify transform adapter: {error}"
        ) from error
    transform_id = getattr(module, "TRANSFORM_ID", None)
    adapt = getattr(module, "adapt", None)
    if transform_id != "archify-standalone-v1" or not callable(adapt):
        raise DistributionError("Archify transform adapter has an incompatible API")
    return transform_id, adapt


def _load_builder(
    root: Path, source: dict[str, Any], path: PurePosixPath
) -> tuple[Any, str, bytes, str, dict[str, Any]]:
    inventory = _source_inventory(root, source)
    record = inventory.get(path.as_posix())
    if record is None:
        raise DistributionError(
            f"public package builder is missing from pinned source: {path}"
        )
    mode, object_type, oid = record
    if object_type != b"blob" or mode not in REGULAR_GIT_MODES:
        raise DistributionError("public package builder must be a regular Git blob")
    data = _cat_file_batch(root, [oid])[oid]
    try:
        syntax = ast.parse(
            data.decode("utf-8"),
            filename=f"{source['repository']}@{source['sha']}:{path}",
        )
    except (UnicodeDecodeError, SyntaxError) as error:
        raise DistributionError(
            f"pinned public package builder is not valid Python: {error}"
        ) from error
    stdlib = getattr(sys, "stdlib_module_names", frozenset())
    for node in ast.walk(syntax):
        if isinstance(node, ast.Import):
            imported = [alias.name.split(".", 1)[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                raise DistributionError(
                    "public package builder cannot import relative modules"
                )
            imported = [node.module.split(".", 1)[0]] if node.module else []
        else:
            continue
        unexpected = [name for name in imported if name not in stdlib]
        if unexpected:
            raise DistributionError(
                "public package builder import closure is not standard-library-only: "
                + ", ".join(sorted(set(unexpected)))
            )
    module_name = f"_pinned_public_personal_codex_builder_{source['sha']}"
    module = ModuleType(module_name)
    namespace = module.__dict__
    namespace.update(
        {
            "__name__": module_name,
            "__file__": f"{source['repository']}@{source['sha']}:{path.as_posix()}",
            "__package__": "",
        }
    )
    sys.modules[module_name] = module
    try:
        exec(compile(data, namespace["__file__"], "exec"), namespace)
    except Exception as error:
        if sys.modules.get(module_name) is module:
            del sys.modules[module_name]
        raise DistributionError(
            f"cannot load pinned public package builder: {error}"
        ) from error
    build_package = namespace.get("build_package")
    if not callable(build_package):
        raise DistributionError(
            "pinned public package builder does not expose build_package"
        )
    try:
        parameters = inspect.signature(build_package).parameters
    except (TypeError, ValueError) as error:
        raise DistributionError(
            "cannot inspect pinned public package builder API"
        ) from error
    required_parameters = {
        "repo_root",
        "manifest_path",
        "output_dir",
        "sha",
        "require_clean_sources",
    }
    if not required_parameters.issubset(parameters):
        raise DistributionError("pinned public package builder API is incompatible")
    return build_package, oid, data, mode.decode("ascii"), namespace


def _reject_builder_filtered_paths(
    destinations: list[tuple[PurePosixPath, str]], builder_namespace: dict[str, Any]
) -> None:
    directory_names = builder_namespace.get("GENERATED_DIR_NAMES")
    file_names = builder_namespace.get("GENERATED_FILE_NAMES")
    suffixes = builder_namespace.get("GENERATED_SUFFIXES")
    if not all(
        isinstance(item, (set, frozenset))
        for item in (directory_names, file_names, suffixes)
    ):
        raise DistributionError(
            "pinned public package builder has no compatible generated-path policy"
        )
    for path, owner in destinations:
        if any(part in directory_names for part in path.parts):
            raise DistributionError(
                f"selected path would be omitted by the package builder: {owner}"
            )
        if path.name in file_names or any(
            path.name.endswith(suffix) for suffix in suffixes
        ):
            raise DistributionError(
                f"selected path would be omitted by the package builder: {owner}"
            )


def _read_aggregator_license(config_path: Path) -> bytes:
    license_path = config_path.parent / "LICENSE"
    try:
        metadata = license_path.lstat()
        payload = license_path.read_bytes()
    except OSError as error:
        raise DistributionError(
            f"cannot read the aggregator Apache-2.0 license: {error}"
        ) from error
    if not stat.S_ISREG(metadata.st_mode) or len(payload) > MAX_CONFIG_BYTES:
        raise DistributionError("aggregator LICENSE must be a bounded regular file")
    if b"Apache License" not in payload or b"Version 2.0" not in payload:
        raise DistributionError("aggregator LICENSE must identify Apache License 2.0")
    scope = (
        "This Apache-2.0 license applies only to the codex-public-distribution aggregation code, "
        "configuration, and generated distribution metadata. Bundled upstream components keep "
        "their own licenses and notices; this file does not relicense those components.\n\n"
    ).encode("utf-8")
    return scope + payload


def _mkdir_for_file(root: Path, relative: PurePosixPath) -> Path:
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        current.mkdir(mode=0o755, exist_ok=True)
        try:
            metadata = current.lstat()
        except OSError as error:
            raise DistributionError(
                f"cannot inspect staged directory {current}: {error}"
            ) from error
        if not stat.S_ISDIR(metadata.st_mode):
            raise DistributionError(
                f"staged path component is not a directory: {current}"
            )
    return current / relative.name


def _write_staged_file(
    root: Path, relative: PurePosixPath, data: bytes, mode: int = 0o644
) -> None:
    destination = _mkdir_for_file(root, relative)
    try:
        with destination.open("xb") as output:
            written = output.write(data)
            if written != len(data):
                raise DistributionError(f"incomplete staged file write: {relative}")
        destination.chmod(mode)
    except FileExistsError as error:
        raise DistributionError(f"duplicate staged output path: {relative}") from error
    except OSError as error:
        raise DistributionError(
            f"cannot write staged output {relative}: {error}"
        ) from error


def _rename_no_replace(source: Path, destination: Path) -> None:
    source_parent = source.parent
    destination_parent = destination.parent
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        source_parent_fd = os.open(source_parent, flags)
    except OSError as error:
        raise DistributionError(
            f"cannot safely open stage parent {source_parent}: {error}"
        ) from error
    try:
        destination_parent_fd = os.open(destination_parent, flags)
    except OSError as error:
        os.close(source_parent_fd)
        raise DistributionError(
            f"cannot safely open stage parent {destination_parent}: {error}"
        ) from error
    try:
        if sys.platform == "darwin":
            rename = ctypes.CDLL(None, use_errno=True).renameatx_np
            rename.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            rename.restype = ctypes.c_int
            result = rename(
                source_parent_fd,
                os.fsencode(source.name),
                destination_parent_fd,
                os.fsencode(destination.name),
                0x00000004,
            )
        elif sys.platform.startswith("linux"):
            rename = ctypes.CDLL(None, use_errno=True).renameat2
            rename.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            rename.restype = ctypes.c_int
            result = rename(
                source_parent_fd,
                os.fsencode(source.name),
                destination_parent_fd,
                os.fsencode(destination.name),
                0x00000001,
            )
        else:
            raise DistributionError(
                f"safe no-replace stage publication is unsupported on {sys.platform}"
            )
        if result != 0:
            error_number = ctypes.get_errno()
            if error_number == 17:
                raise DistributionError(
                    f"stage destination already exists: {destination}"
                )
            raise OSError(error_number, os.strerror(error_number), str(destination))
    except AttributeError as error:
        raise DistributionError(
            "platform lacks an atomic no-replace directory rename"
        ) from error
    except OSError as error:
        raise DistributionError(
            f"cannot publish stage directory {destination}: {error}"
        ) from error
    finally:
        os.close(source_parent_fd)
        os.close(destination_parent_fd)


def _load_inputs(
    config_path: Path, lock_path: Path, sources_root: Path, revision: str
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    bytes,
    list[dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, Any],
    Path,
]:
    config, _config_bytes = _read_json(config_path, "distribution config")
    lock, _lock_bytes = _read_json(lock_path, "source lock")
    exports, sources, builder = _validate_config(config, lock, revision)
    try:
        metadata = sources_root.lstat()
        resolved_sources_root = sources_root.resolve(strict=True)
    except OSError as error:
        raise DistributionError(
            f"cannot resolve sources root {sources_root}: {error}"
        ) from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise DistributionError(
            f"sources root must be a non-symlink directory: {sources_root}"
        )
    return (
        config,
        lock,
        _canonical_json(lock),
        exports,
        sources,
        builder,
        resolved_sources_root,
    )


def _build_stage(
    config_path: Path,
    config: dict[str, Any],
    lock: dict[str, Any],
    lock_payload: bytes,
    exports: list[dict[str, Any]],
    sources: dict[str, dict[str, Any]],
    builder: dict[str, Any],
    sources_root: Path,
    stage_root: Path,
    revision: str,
) -> dict[str, Any]:
    if stage_root.exists() or os.path.lexists(stage_root):
        raise DistributionError(f"stage destination already exists: {stage_root}")
    try:
        stage_parent = stage_root.parent.resolve(strict=True)
    except OSError as error:
        raise DistributionError(
            f"stage parent must exist before staging: {stage_root.parent}: {error}"
        ) from error
    stage_name = stage_root.name
    if stage_name in {"", ".", ".."} or "/" in stage_name:
        raise DistributionError(f"invalid stage destination: {stage_root}")
    final_stage = stage_parent / stage_name

    roots: dict[str, Path] = {}
    inventories: dict[str, dict[str, tuple[bytes, bytes, str]]] = {}
    for name, source in sources.items():
        roots[name] = _source_root(sources_root, name)
        inventories[name] = _source_inventory(roots[name], source)

    export_plans: list[
        tuple[dict[str, Any], list[tuple[PurePosixPath, str, bytes, str]]]
    ] = []
    selected_count = 0
    expected_destinations: list[tuple[PurePosixPath, str]] = []
    for index, export in enumerate(exports):
        selected = _select_export(export, inventories[export["source"]])
        export_plans.append((export, selected))
        for relative, source_path, mode, oid in selected:
            destination = (
                export["destination"]
                if export["kind"] == "file"
                else export["destination"] / relative
            )
            expected_destinations.append(
                (destination, f"export {index} source {source_path}")
            )
            selected_count += 1
            if selected_count > MAX_EXPORT_FILES:
                raise DistributionError(
                    f"selected export exceeds {MAX_EXPORT_FILES} files"
                )

    generated_paths = [
        (BUILDER_MANIFEST, "generated package manifest"),
        (PROVENANCE_PATH, "generated provenance"),
        (SOURCE_LOCK_PATH, "generated source lock"),
        (AGGREGATOR_LICENSE_PATH, "aggregator license"),
    ]
    _validate_distinct_paths(
        expected_destinations + generated_paths, "staged output paths"
    )

    builder_source = sources[builder["source"]]
    builder_root = roots[builder["source"]]
    build_package, builder_blob, builder_bytes, builder_mode, builder_namespace = (
        _load_builder(builder_root, builder_source, builder["path"])
    )
    _reject_builder_filtered_paths(expected_destinations, builder_namespace)

    license_payload = _read_aggregator_license(config_path)
    file_provenance: list[dict[str, Any]] = []
    source_summaries = [
        {
            "name": source["name"],
            "repository": source["repository"],
            "ref": source["ref"],
            "sha": source["sha"],
            "tree": source["tree"],
        }
        for source in sorted(sources.values(), key=lambda item: item["name"])
    ]
    total_written = 0

    with tempfile.TemporaryDirectory(
        prefix=f".{stage_name}.stage.", dir=stage_parent
    ) as temporary_root:
        temp_path = Path(temporary_root)
        staging_tree = temp_path / "tree"
        staging_tree.mkdir(mode=0o755)
        for export, selected in export_plans:
            source = sources[export["source"]]
            blobs = _cat_file_batch(
                roots[export["source"]], sorted({item[3] for item in selected})
            )
            transform_id: str | None = None
            adapter: Any = None
            if export["transform"] == "archify-standalone-v1":
                transform_id, adapter = _load_archify_adapter()
            for relative, source_path, mode, oid in selected:
                original = blobs[oid]
                data = adapter(relative, original) if adapter is not None else original
                if not isinstance(data, bytes):
                    raise DistributionError(
                        f"transform returned non-bytes for {source_path}"
                    )
                if len(data) > MAX_EXPORT_FILE_BYTES:
                    raise DistributionError(
                        f"transformed export exceeds per-file limit: {source_path}"
                    )
                total_written += len(data)
                if total_written > MAX_EXPORT_TOTAL_BYTES:
                    raise DistributionError(
                        "transformed export exceeds total export size limit"
                    )
                source_mode = mode.decode("ascii")
                destination = (
                    export["destination"]
                    if export["kind"] == "file"
                    else export["destination"] / relative
                )
                _write_staged_file(
                    staging_tree,
                    destination,
                    data,
                    0o755 if mode == b"100755" else 0o644,
                )
                file_provenance.append(
                    {
                        "source": export["source"],
                        "path": source_path,
                        "destination": destination.as_posix(),
                        "blob": oid,
                        "mode": source_mode,
                        "output_sha256": hashlib.sha256(data).hexdigest(),
                        "transform_id": transform_id,
                    }
                )

        link_manifest = {
            "version": 1,
            "owner": "public",
            "links": [
                {
                    "source": export["destination"].as_posix(),
                    "target": export["target"].as_posix(),
                    "kind": export["kind"],
                }
                for export in exports
            ],
            "reference_only": REFERENCE_ONLY_PATHS,
        }
        provenance = {
            "version": 1,
            "repository": config["repository"],
            "revision": revision,
            "builder": {
                "source": builder["source"],
                "repository": builder_source["repository"],
                "ref": builder_source["ref"],
                "sha": builder_source["sha"],
                "tree": builder_source["tree"],
                "path": builder["path"].as_posix(),
                "blob": builder_blob,
                "mode": builder_mode,
                "sha256": hashlib.sha256(builder_bytes).hexdigest(),
            },
            "sources": source_summaries,
            "files": sorted(
                file_provenance,
                key=lambda item: (item["destination"], item["source"], item["path"]),
            ),
        }
        _write_staged_file(
            staging_tree, BUILDER_MANIFEST, _canonical_json(link_manifest)
        )
        _write_staged_file(staging_tree, PROVENANCE_PATH, _canonical_json(provenance))
        _write_staged_file(staging_tree, SOURCE_LOCK_PATH, lock_payload)
        _write_staged_file(staging_tree, AGGREGATOR_LICENSE_PATH, license_payload)
        _rename_no_replace(staging_tree, final_stage)

    return link_manifest


def stage_distribution(
    config_path: Path,
    lock_path: Path,
    sources_root: Path,
    stage_root: Path,
    revision: str,
) -> dict[str, Any]:
    """Stage selected pinned Git blobs and return the public install manifest."""
    config_path = Path(config_path)
    lock_path = Path(lock_path)
    sources_root = Path(sources_root)
    stage_root = Path(stage_root)
    config, lock, lock_payload, exports, sources, builder, resolved_sources_root = (
        _load_inputs(config_path, lock_path, sources_root, revision)
    )
    return _build_stage(
        config_path,
        config,
        lock,
        lock_payload,
        exports,
        sources,
        builder,
        resolved_sources_root,
        stage_root,
        revision,
    )


def build_distribution(
    config_path: Path,
    lock_path: Path,
    sources_root: Path,
    output_dir: Path,
    revision: str,
) -> tuple[Path, Path]:
    """Stage pinned public sources and package them with the pinned public builder."""
    config_path = Path(config_path)
    lock_path = Path(lock_path)
    sources_root = Path(sources_root)
    output_dir = Path(output_dir)
    config, lock, lock_payload, exports, sources, builder, resolved_sources_root = (
        _load_inputs(config_path, lock_path, sources_root, revision)
    )
    builder_source = sources[builder["source"]]
    with tempfile.TemporaryDirectory(
        prefix="codex-public-distribution."
    ) as temporary_root:
        staged = Path(temporary_root) / "stage"
        _build_stage(
            config_path,
            config,
            lock,
            lock_payload,
            exports,
            sources,
            builder,
            resolved_sources_root,
            staged,
            revision,
        )
        builder_root = _source_root(resolved_sources_root, builder_source["name"])
        (
            build_package,
            _builder_blob,
            _builder_bytes,
            _builder_mode,
            _builder_namespace,
        ) = _load_builder(builder_root, builder_source, builder["path"])
        try:
            return build_package(
                staged,
                BUILDER_MANIFEST,
                output_dir,
                revision,
                require_clean_sources=False,
            )
        except Exception as error:
            if error.__class__.__name__ == "PackageError":
                raise DistributionError(
                    f"public package builder failed: {error}"
                ) from error
            raise


def build_parser() -> argparse.ArgumentParser:
    repo_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=repo_root / "distribution.json")
    parser.add_argument("--lock", type=Path, default=repo_root / "sources.lock.json")
    parser.add_argument("--sources-root", type=Path, default=repo_root / ".sources")
    parser.add_argument("--output-dir", type=Path, default=repo_root / "dist")
    parser.add_argument("--sha", required=True, help="40-character aggregator revision")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        archive, checksum = build_distribution(
            args.config,
            args.lock,
            args.sources_root,
            args.output_dir,
            args.sha,
        )
    except DistributionError as error:
        print(f"distribution: error: {error}", file=sys.stderr)
        return 2
    print(archive)
    print(checksum)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
