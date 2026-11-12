import json
import unittest
from pathlib import PurePosixPath

from scripts.archify_export import TRANSFORM_ID, adapt


def package_fixture() -> bytes:
    package = {
        "name": "archify",
        "version": "2.17.0-dev.1",
        "private": True,
        "type": "module",
        "description": "JSON-IR diagram renderers.",
        "license": "MIT",
        "bin": {"archify": "./bin/archify.mjs"},
        "engines": {"node": ">=18"},
        "scripts": {
            "generate:brand-marks": "node scripts/generate-brand-marks.mjs",
            "check:brand-marks": "node scripts/generate-brand-marks.mjs --check",
            "generate:validators": "node scripts/generate-validators.mjs",
            "check:validators": "node scripts/generate-validators.mjs --check",
            "check:release-identity": "node ../scripts/check-release-identity.mjs",
            "build:gallery": "node ../scripts/build-gallery.mjs ../docs",
            "build:guide": "node ../scripts/build-guide.mjs ../docs/guide.html",
            "build:start": "node ../scripts/build-start.mjs ../docs/start.html",
            "build:readme-showcase": "node ../scripts/build-readme-showcase.mjs",
            "test:webm": "node test/webm-artifact.smoke.mjs && node --test test/site-language-integration.mjs",
            "test": (
                "npm run check:brand-marks && npm run check:validators && "
                "npm run check:release-identity && node test/golden.mjs && "
                "node ../scripts/run-tests.mjs"
            ),
            "render:examples": "node scripts/render-examples.mjs ../examples",
        },
        "devDependencies": {
            "ajv": "^8.17.1",
            "parse5": "7.3.0",
            "saxes": "6.0.0",
            "simple-icons": "16.28.0",
        },
        "overrides": {"fast-uri": "3.1.5"},
    }
    return (json.dumps(package, ensure_ascii=False, indent=2) + "\n").encode()


class ArchifyExportTests(unittest.TestCase):
    def test_standalone_package_metadata_adaptation(self) -> None:
        self.assertEqual(TRANSFORM_ID, "archify-standalone-v1")

        result = json.loads(adapt(PurePosixPath("package.json"), package_fixture()))

        self.assertEqual(
            result,
            {
                "name": "archify",
                "version": "2.17.0-dev.1",
                "type": "module",
                "description": "JSON-IR diagram renderers.",
                "license": "MIT",
                "bin": {"archify": "./bin/archify.mjs"},
                "engines": {"node": ">=18"},
            },
        )

    def test_unrelated_files_are_byte_identical(self) -> None:
        cases = {
            PurePosixPath(
                "bin/archify.mjs"
            ): b"const root = path.resolve(__dirname, '..');\n",
            PurePosixPath(
                "scripts/update-contract.mjs"
            ): b"../scripts stays as source text\x00\xff",
            PurePosixPath("assets/JetBrainsMono-OFL.txt"): b"font license\x00\xff",
        }
        for path, content in cases.items():
            with self.subTest(path=path):
                self.assertIs(adapt(path, content), content)

    def test_unsupported_package_shape_is_rejected(self) -> None:
        package = json.loads(package_fixture())
        package["scripts"]["test"] = "node test/new-source-only-test.mjs"
        changed_source = json.dumps(package).encode()

        with self.assertRaisesRegex(
            ValueError, "unsupported archify/package.json field: scripts"
        ):
            adapt(PurePosixPath("package.json"), changed_source)

    def test_invalid_package_json_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "not valid UTF-8 JSON"):
            adapt(PurePosixPath("package.json"), b"{not json}")


if __name__ == "__main__":
    unittest.main()
