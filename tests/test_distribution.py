from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
import zlib

from scripts.distribution import (
    DistributionError,
    build_distribution,
    stage_distribution,
)


REVISION = "a" * 40
BUILDER_SOURCE = r"""import gzip
import hashlib
import io
import stat
import tarfile
from pathlib import Path

GENERATED_DIR_NAMES = set()
GENERATED_FILE_NAMES = set()
GENERATED_SUFFIXES = set()

def build_package(repo_root, manifest_path, output_dir, sha, *, require_clean_sources):
    assert require_clean_sources is False
    assert (repo_root / manifest_path).is_file()
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / f"personal-codex-{sha}.tar.gz"
    with archive.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as zipped:
            with tarfile.open(fileobj=zipped, mode="w") as bundle:
                for path in sorted(repo_root.rglob("*")):
                    if not path.is_file():
                        continue
                    relative = path.relative_to(repo_root).as_posix()
                    data = path.read_bytes()
                    info = tarfile.TarInfo(relative)
                    info.size = len(data)
                    info.mode = stat.S_IMODE(path.stat().st_mode)
                    info.mtime = 0
                    info.uid = 0
                    info.gid = 0
                    bundle.addfile(info, io.BytesIO(data))
    checksum = output_dir / f"personal-codex-{sha}.sha256"
    checksum.write_text(
        f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n",
        encoding="ascii",
    )
    return archive, checksum
"""


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


class DistributionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="distribution-tests-")
        self.root = Path(self.temp.name)
        self.sources_root = self.root / "sources"
        self.sources_root.mkdir()
        self.repo = self.sources_root / "codex-toolbox"
        self.repo.mkdir()
        git(self.repo, "init", "--quiet")
        git(self.repo, "config", "user.name", "Distribution Test")
        git(self.repo, "config", "user.email", "distribution@example.invalid")
        self.write_repo("scripts/build_personal_codex_package.py", BUILDER_SOURCE)
        self.write_repo(
            "scripts/codex_personal_sync.py", "#!/usr/bin/env python3\n", mode=0o755
        )
        self.write_repo(
            "skills/demo/SKILL.md", "---\nname: demo\n---\nCommitted skill.\n"
        )
        self.write_repo("skills/demo/README.md", "Committed README.\n")
        self.write_repo(
            "skills/demo/tools/run.sh", "#!/bin/sh\necho committed\n", mode=0o755
        )
        self.write_repo("skills/demo/private.txt", "private payload\n")
        self.write_repo(
            "personal_codex/bin/example", "committed executable\n", mode=0o755
        )
        git(self.repo, "add", "--all")
        git(self.repo, "-c", "gc.auto=0", "commit", "--quiet", "-m", "fixture")
        self.commit = git(self.repo, "rev-parse", "HEAD")
        self.tree = git(self.repo, "rev-parse", "HEAD^{tree}")
        self.config_path = self.root / "distribution.json"
        self.lock_path = self.root / "sources.lock.json"
        self.write(self.config_path.name, "")
        self.write(self.lock_path.name, "")
        self.write(self.root / "LICENSE", "Apache License\nVersion 2.0\n")
        self.write_json(self.config_path, self.config())
        self.write_json(self.lock_path, self.lock())

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write(self, relative: str | Path, content: str, *, mode: int = 0o644) -> Path:
        path = Path(relative) if Path(relative).is_absolute() else self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        path.chmod(mode)
        return path

    def write_repo(self, relative: str, content: str, *, mode: int = 0o644) -> Path:
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        path.chmod(mode)
        return path

    def write_json(self, path: Path, value: object) -> None:
        path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")

    def config(self) -> dict:
        return {
            "version": 1,
            "repository": "Joey-Tools/codex-public-distribution",
            "builder": {
                "source": "codex-toolbox",
                "path": "scripts/build_personal_codex_package.py",
            },
            "exports": [
                {
                    "source": "codex-toolbox",
                    "path": "skills/demo",
                    "destination": "personal_codex/skills/demo",
                    "target": "skills/demo",
                    "kind": "skill",
                    "exclude": ["private.txt"],
                },
                {
                    "source": "codex-toolbox",
                    "path": "personal_codex/bin/example",
                    "destination": "personal_codex/bin/example",
                    "target": "bin/example",
                    "kind": "file",
                },
                {
                    "source": "codex-toolbox",
                    "path": "scripts/codex_personal_sync.py",
                    "destination": "scripts/codex_personal_sync.py",
                    "target": "bin/codex-personal-sync",
                    "kind": "file",
                },
            ],
        }

    def lock(self) -> dict:
        return {
            "version": 1,
            "sources": [
                {
                    "name": "codex-toolbox",
                    "repository": "Joey-Tools/codex-toolbox",
                    "repository_id": 123,
                    "ref": "master",
                    "sha": self.commit,
                    "tree": self.tree,
                }
            ],
        }

    def stage(self, name: str = "stage") -> tuple[Path, dict]:
        destination = self.root / name
        manifest = stage_distribution(
            self.config_path,
            self.lock_path,
            self.sources_root,
            destination,
            REVISION,
        )
        return destination, manifest

    def recommit(self) -> None:
        git(self.repo, "add", "--all")
        git(self.repo, "-c", "gc.auto=0", "commit", "--quiet", "-m", "fixture update")
        self.commit = git(self.repo, "rev-parse", "HEAD")
        self.tree = git(self.repo, "rev-parse", "HEAD^{tree}")
        self.write_json(self.lock_path, self.lock())

    def test_stages_only_pinned_blobs_with_modes_provenance_and_public_manifest(
        self,
    ) -> None:
        # Dirty tracked and untracked worktree data must not replace the pinned Git objects.
        self.write_repo("skills/demo/README.md", "uncommitted worktree edit\n")
        self.write_repo("skills/demo/untracked.txt", "uncommitted addition\n")
        stage, manifest = self.stage()

        readme = stage / "personal_codex/skills/demo/README.md"
        tool = stage / "personal_codex/skills/demo/tools/run.sh"
        self.assertEqual(readme.read_text(encoding="utf-8"), "Committed README.\n")
        self.assertFalse((stage / "personal_codex/skills/demo/untracked.txt").exists())
        self.assertFalse((stage / "personal_codex/skills/demo/private.txt").exists())
        self.assertEqual(stat.S_IMODE(tool.stat().st_mode), 0o755)
        self.assertEqual(
            (stage / "personal_codex/bin/example").read_text(encoding="utf-8"),
            "committed executable\n",
        )
        self.assertEqual(manifest["owner"], "public")
        self.assertEqual(
            manifest["links"],
            [
                {
                    "source": "personal_codex/skills/demo",
                    "target": "skills/demo",
                    "kind": "skill",
                },
                {
                    "source": "personal_codex/bin/example",
                    "target": "bin/example",
                    "kind": "file",
                },
                {
                    "source": "scripts/codex_personal_sync.py",
                    "target": "bin/codex-personal-sync",
                    "kind": "file",
                },
            ],
        )
        self.assertEqual(
            manifest["reference_only"],
            [
                "personal_codex/distribution-provenance.json",
                "personal_codex/distribution-sources.lock.json",
                "personal_codex/AGGREGATOR-LICENSE.txt",
            ],
        )

        provenance = json.loads(
            (stage / "personal_codex/distribution-provenance.json").read_text(
                encoding="utf-8"
            )
        )
        selected = {item["path"]: item for item in provenance["files"]}
        builder_blob = git(
            self.repo,
            "rev-parse",
            f"{self.commit}:scripts/build_personal_codex_package.py",
        )
        committed_blob = git(
            self.repo, "rev-parse", f"{self.commit}:skills/demo/README.md"
        )
        self.assertEqual(
            provenance["repository"], "Joey-Tools/codex-public-distribution"
        )
        self.assertEqual(provenance["revision"], REVISION)
        self.assertEqual(provenance["sources"][0]["sha"], self.commit)
        self.assertEqual(provenance["sources"][0]["tree"], self.tree)
        self.assertEqual(provenance["builder"]["blob"], builder_blob)
        self.assertEqual(provenance["builder"]["mode"], "100644")
        self.assertEqual(selected["skills/demo/README.md"]["blob"], committed_blob)
        self.assertEqual(selected["skills/demo/README.md"]["mode"], "100644")
        self.assertEqual(selected["scripts/codex_personal_sync.py"]["mode"], "100755")
        self.assertEqual(
            stat.S_IMODE((stage / "scripts/codex_personal_sync.py").stat().st_mode),
            0o755,
        )
        self.assertEqual(
            selected["skills/demo/README.md"]["output_sha256"],
            hashlib.sha256(b"Committed README.\n").hexdigest(),
        )
        self.assertEqual(
            (stage / "personal_codex/public-sync-manifest.json").read_bytes(),
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode()
            + b"\n",
        )
        self.assertEqual(
            (stage / "personal_codex/distribution-sources.lock.json").read_text(
                encoding="utf-8"
            ),
            json.dumps(self.lock(), ensure_ascii=False, sort_keys=True, indent=2)
            + "\n",
        )
        self.assertIn(
            "applies only to the codex-public-distribution aggregation code",
            (stage / "personal_codex/AGGREGATOR-LICENSE.txt").read_text(
                encoding="utf-8"
            ),
        )

    def test_rejects_unsafe_paths_and_conflicting_destinations(self) -> None:
        config = self.config()
        config["exports"][0]["path"] = "../outside"
        self.write_json(self.config_path, config)
        with self.assertRaisesRegex(DistributionError, "unsafe export 0 source path"):
            self.stage("unsafe-source")

        config = self.config()
        config["exports"][0]["target"] = "../outside"
        self.write_json(self.config_path, config)
        with self.assertRaisesRegex(DistributionError, "unsafe export 0 target"):
            self.stage("unsafe-target")

        config = self.config()
        config["exports"][1]["destination"] = config["exports"][0]["destination"]
        self.write_json(self.config_path, config)
        with self.assertRaisesRegex(DistributionError, "overlapping destination roots"):
            self.stage("conflicting-destination")

        config = self.config()
        config["exports"][2]["target"] = "bin/not-codex-personal-sync"
        self.write_json(self.config_path, config)
        with self.assertRaisesRegex(
            DistributionError, "export destination must be under"
        ):
            self.stage("unexpected-top-level-destination")

    def test_rejects_selected_symlink_and_gitlink_entries(self) -> None:
        os.symlink("README.md", self.repo / "skills/demo/linked.md")
        self.recommit()
        with self.assertRaisesRegex(DistributionError, "refusing symlink, gitlink"):
            self.stage("symlink")

        (self.repo / "skills/demo/linked.md").unlink()
        git(self.repo, "rm", "--quiet", "--ignore-unmatch", "skills/demo/linked.md")
        placeholder = "d" * 40
        git(
            self.repo,
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{placeholder},skills/demo/nested-module",
        )
        git(self.repo, "-c", "gc.auto=0", "commit", "--quiet", "-m", "gitlink fixture")
        self.commit = git(self.repo, "rev-parse", "HEAD")
        self.tree = git(self.repo, "rev-parse", "HEAD^{tree}")
        self.write_json(self.lock_path, self.lock())
        with self.assertRaisesRegex(DistributionError, "refusing symlink, gitlink"):
            self.stage("gitlink")

    def test_rejects_pinned_tree_mismatch_and_corrupt_blob_content(self) -> None:
        lock = self.lock()
        lock["sources"][0]["tree"] = "0" * 40
        self.write_json(self.lock_path, lock)
        with self.assertRaisesRegex(DistributionError, "tree mismatch"):
            self.stage("tree-mismatch")

        self.write_json(self.lock_path, self.lock())
        blob = git(self.repo, "rev-parse", f"{self.commit}:skills/demo/README.md")
        loose = self.repo / ".git" / "objects" / blob[:2] / blob[2:]
        self.assertTrue(
            loose.is_file(), "fixture expects the selected blob to remain loose"
        )
        original = zlib.decompress(loose.read_bytes())
        header, _content = original.split(b"\0", 1)
        tampered = b"altered Git blob bytes\n"
        loose.chmod(0o644)
        loose.write_bytes(zlib.compress(header + b"\0" + tampered))
        with self.assertRaises(DistributionError):
            self.stage("corrupt-blob")

    def test_root_export_applies_exclusions_and_requires_skill_marker(self) -> None:
        config = self.config()
        config["exports"] = [
            {
                "source": "codex-toolbox",
                "path": ".",
                "destination": "personal_codex/skills/root",
                "target": "skills/root",
                "kind": "skill",
                "exclude": [
                    "skills/**",
                    "personal_codex/**",
                    "scripts/**",
                    "private.txt",
                ],
            }
        ]
        self.write_repo("SKILL.md", "---\nname: root\n---\n")
        self.write_repo("private.txt", "excluded\n")
        self.recommit()
        self.write_json(self.config_path, config)
        stage, _manifest = self.stage("root-export")
        self.assertEqual(
            (stage / "personal_codex/skills/root/SKILL.md").read_text(encoding="utf-8"),
            "---\nname: root\n---\n",
        )
        self.assertFalse((stage / "personal_codex/skills/root/private.txt").exists())
        self.assertFalse((stage / "personal_codex/skills/root/scripts").exists())

    def test_build_is_deterministic_with_a_pinned_standard_library_builder(
        self,
    ) -> None:
        first = self.root / "first"
        second = self.root / "second"
        archive_one, checksum_one = build_distribution(
            self.config_path, self.lock_path, self.sources_root, first, REVISION
        )
        archive_two, checksum_two = build_distribution(
            self.config_path, self.lock_path, self.sources_root, second, REVISION
        )
        self.assertEqual(archive_one.read_bytes(), archive_two.read_bytes())
        self.assertEqual(checksum_one.read_bytes(), checksum_two.read_bytes())
        self.assertEqual(
            checksum_one.read_text(encoding="ascii"),
            f"{hashlib.sha256(archive_one.read_bytes()).hexdigest()}  {archive_one.name}\n",
        )


if __name__ == "__main__":
    unittest.main()
