#!/usr/bin/env python3
"""Exercise a built public distribution with its packaged runtime."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
import shutil
import sys
import tempfile
from pathlib import Path
from pathlib import PurePosixPath
from types import ModuleType


def load_runtime(release_root: Path) -> ModuleType:
    script = release_root / "scripts" / "codex_personal_sync.py"
    if not script.is_file():
        raise RuntimeError(f"distribution is missing packaged runtime: {script}")
    spec = importlib.util.spec_from_file_location("codex_personal_sync", script)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load packaged runtime: {script}")
    runtime = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = runtime
    spec.loader.exec_module(runtime)
    return runtime


def artifact_paths(directory: Path, sha: str) -> tuple[Path, Path]:
    archive = directory / f"personal-codex-{sha}.tar.gz"
    checksum = directory / f"personal-codex-{sha}.sha256"
    if (
        not archive.is_file()
        or archive.is_symlink()
        or not checksum.is_file()
        or checksum.is_symlink()
    ):
        raise RuntimeError(
            f"distribution directory is missing exact artifacts for {sha}"
        )
    return archive, checksum


def verify_sidecar(archive: Path, checksum: Path) -> None:
    if checksum.stat().st_size > 256:
        raise RuntimeError(f"SHA-256 checksum sidecar is too large: {checksum.name}")
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    expected_line = f"{digest}  {archive.name}\n"
    if checksum.read_text(encoding="ascii") != expected_line:
        raise RuntimeError(f"SHA-256 checksum mismatch for {archive.name}")


def stage_archive(
    runtime: ModuleType, distribution: Path, sha: str, workspace: Path
) -> Path:
    archive, checksum = artifact_paths(distribution, sha)
    staged_archive = workspace / archive.name
    staged_checksum = workspace / checksum.name
    shutil.copy2(archive, staged_archive)
    shutil.copy2(checksum, staged_checksum)
    with runtime.bind_archive_workspace(workspace) as bound:
        extraction = workspace / "extracted"
        release_root, _expectation = runtime.verify_and_extract_archive(
            staged_archive,
            staged_checksum,
            extraction,
            workspace=bound,
        )
    runtime.validate_release_tree(release_root)
    return release_root


def run_archify_smoke(home: Path, workspace: Path) -> None:
    skill_root = home / "skills" / "archify"
    package_path = skill_root / "package.json"
    cli_path = skill_root / "bin" / "archify.mjs"
    example_path = skill_root / "examples" / "web-app.architecture.json"
    checker_path = skill_root / "scripts" / "check-render-output.mjs"
    for path in (package_path, cli_path, example_path, checker_path):
        if not path.is_file():
            raise RuntimeError(
                f"installed Archify package is missing required file: {path}"
            )

    package = json.loads(package_path.read_text(encoding="utf-8"))
    if package.get("bin") != {"archify": "./bin/archify.mjs"}:
        raise RuntimeError(
            "installed Archify package has an unexpected CLI entry point"
        )
    if any(key in package for key in ("scripts", "devDependencies", "overrides")):
        raise RuntimeError(
            "installed Archify package retained source-only package metadata"
        )

    output_dir = workspace / "archify-smoke"
    output_dir.mkdir()
    output_path = output_dir / "web-app.html"
    commands = (
        (
            "render",
            [
                "node",
                str(cli_path),
                "render",
                "architecture",
                str(example_path),
                str(output_path),
            ],
            skill_root,
        ),
        (
            "render-output check",
            ["node", str(checker_path), str(output_path)],
            skill_root,
        ),
    )
    for label, command, cwd in commands:
        try:
            result = subprocess.run(
                command,
                cwd=cwd,
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RuntimeError(
                f"installed Archify {label} failed to start or timed out: {error}"
            ) from error
        if result.returncode != 0:
            details = (result.stdout + result.stderr).strip()[-2000:]
            raise RuntimeError(
                f"installed Archify {label} exited with {result.returncode}: {details}"
            )
    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise RuntimeError("installed Archify CLI did not create its HTML output")


def verify_installed_release(
    runtime: ModuleType,
    home: Path,
    release_root: Path,
    sha: str,
    *,
    expected_link_count: int,
) -> None:
    if not runtime.status(home, runtime.PUBLIC_OWNER):
        raise RuntimeError(f"runtime status reported an unhealthy public release {sha}")
    identities = runtime.release_identities(
        home, mode="public", owner=runtime.PUBLIC_OWNER
    )
    public_identity = identities.get("release_trees", {}).get(runtime.PUBLIC_OWNER)
    if not isinstance(public_identity, dict) or public_identity.get("sha") != sha:
        raise RuntimeError(
            f"runtime could not verify the active public release identity {sha}"
        )

    manifest_path = release_root / "personal_codex" / "sync-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_targets = {entry["target"] for entry in manifest["links"]}
    if len(expected_targets) != expected_link_count:
        raise RuntimeError(
            f"distribution {sha} has {len(expected_targets)} public links; "
            f"expected {expected_link_count}"
        )
    installed_entries = runtime.current_release_entries(home, runtime.PUBLIC_OWNER)
    installed_targets = {entry.target.as_posix() for entry in installed_entries}
    if installed_targets != expected_targets:
        raise RuntimeError(
            f"installed public links do not match distribution {sha}: "
            f"expected {len(expected_targets)}, found {len(installed_targets)}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dist", type=Path, required=True, help="built distribution directory"
    )
    parser.add_argument("--sha", required=True, help="40-character release identity")
    parser.add_argument(
        "--previous-dist", type=Path, help="optional earlier public distribution"
    )
    parser.add_argument("--previous-sha", help="release identity for --previous-dist")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if len(args.sha) != 40 or any(char not in "0123456789abcdef" for char in args.sha):
        raise SystemExit("--sha must be a lowercase 40-character hexadecimal value")
    if bool(args.previous_dist) != bool(args.previous_sha):
        raise SystemExit("--previous-dist and --previous-sha must be supplied together")
    if args.previous_sha and (
        len(args.previous_sha) != 40
        or any(char not in "0123456789abcdef" for char in args.previous_sha)
    ):
        raise SystemExit(
            "--previous-sha must be a lowercase 40-character hexadecimal value"
        )

    with tempfile.TemporaryDirectory(prefix="codex-public-package-smoke-") as temp:
        root = Path(temp)
        bootstrap_runtime = _find_runtime(args.dist, args.sha, root)
        first_runtime: ModuleType | None = None
        first_root: Path | None = None
        if args.previous_dist:
            previous_workspace = root / "previous"
            previous_workspace.mkdir()
            first_root = stage_archive(
                bootstrap_runtime,
                args.previous_dist,
                args.previous_sha,
                previous_workspace,
            )
            first_runtime = load_runtime(first_root)

        target_archive, target_checksum = artifact_paths(args.dist, args.sha)
        verify_sidecar(target_archive, target_checksum)
        target_workspace = root / "target"
        target_workspace.mkdir()
        target_root = stage_archive(
            bootstrap_runtime, args.dist, args.sha, target_workspace
        )
        runtime = load_runtime(target_root)

        home = root / "home" / ".codex"
        preserved = home / "user-owned" / "keep.txt"
        preserved.parent.mkdir(parents=True)
        preserved.write_text("user-owned\n", encoding="utf-8")

        if first_root is not None:
            assert first_runtime is not None
            first_runtime.install_release_tree(
                first_root,
                home,
                args.previous_sha,
                dry_run=False,
            )
            verify_installed_release(
                first_runtime,
                home,
                first_root,
                args.previous_sha,
                expected_link_count=6,
            )
        runtime.install_release_tree(
            target_root,
            home,
            args.sha,
            dry_run=False,
        )
        verify_installed_release(
            runtime,
            home,
            target_root,
            args.sha,
            expected_link_count=16,
        )
        if preserved.read_text(encoding="utf-8") != "user-owned\n":
            raise RuntimeError(
                "install modified a user-owned file outside the public package"
            )
        state = json.loads(
            (home / "personal-sync" / "state" / "managed-links.json").read_text(
                encoding="utf-8"
            )
        )
        if state.get("owners", {}).get(runtime.PUBLIC_OWNER) != args.sha:
            raise RuntimeError("installed public release identity does not match --sha")
        run_archify_smoke(home, root)

        # The runtime intentionally has no public-owner uninstall operation.
        # Exercise its refusal and prove it leaves the active public install intact.
        current_pointer = home / "personal-sync" / "current"
        before_current = current_pointer.readlink()
        try:
            runtime.uninstall_overlay(home, runtime.PUBLIC_OWNER, dry_run=False)
        except runtime.SyncError as error:
            if "refusing to uninstall public as an overlay" not in str(error):
                raise
        else:
            raise RuntimeError("runtime unexpectedly accepted public overlay uninstall")
        if current_pointer.readlink() != before_current or not runtime.status(
            home, runtime.PUBLIC_OWNER
        ):
            raise RuntimeError("rejected public uninstall modified the active install")
        if preserved.read_text(encoding="utf-8") != "user-owned\n":
            raise RuntimeError("rejected public uninstall modified a user-owned file")

        print(f"archive: {target_archive.name}")
        print(f"public release: {args.sha}")
        print(
            "status: healthy; active release identity verified; managed user file preserved"
        )
        print(
            "Archify: installed CLI rendered an architecture example; output checker passed"
        )
        print("uninstall support: public-owner request safely rejected by runtime")
        if args.previous_sha:
            print(f"upgrade: {args.previous_sha} -> {args.sha}")
    return 0


def _extract_for_runtime_discovery(archive: Path, workspace: Path) -> Path:
    """Extract a checksum-verified runtime to bootstrap the real archive verifier."""
    import tarfile

    extract = workspace / "discovery"
    extract.mkdir()
    with tarfile.open(archive, "r:*") as bundle:
        candidates = [
            member
            for member in bundle.getmembers()
            if PurePosixPath(member.name).name == "codex_personal_sync.py"
            and PurePosixPath(member.name).parent.name == "scripts"
        ]
        if len(candidates) != 1 or not candidates[0].isfile():
            raise RuntimeError(
                f"expected one regular packaged runtime, found {len(candidates)} in {archive.name}"
            )
        member = candidates[0]
        relative = PurePosixPath(member.name)
        if relative.is_absolute() or any(
            part in {"", ".", ".."} for part in relative.parts
        ):
            raise RuntimeError(f"unsafe runtime archive path in {archive.name}")
        destination = extract.joinpath(*relative.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        source = bundle.extractfile(member)
        if source is None:
            raise RuntimeError(f"cannot read packaged runtime from {archive.name}")
        with source, destination.open("xb") as output:
            shutil.copyfileobj(source, output)
    candidates = sorted(extract.glob("**/scripts/codex_personal_sync.py"))
    if len(candidates) != 1:
        raise RuntimeError(
            f"expected one packaged runtime, found {len(candidates)} in {archive.name}"
        )
    return candidates[0].parent.parent


def _find_runtime(distribution: Path, sha: str, workspace: Path) -> ModuleType:
    archive, checksum = artifact_paths(distribution, sha)
    verify_sidecar(archive, checksum)
    discovery = workspace / "runtime-discovery"
    discovery.mkdir()
    return load_runtime(_extract_for_runtime_discovery(archive, discovery))


if __name__ == "__main__":
    raise SystemExit(main())
