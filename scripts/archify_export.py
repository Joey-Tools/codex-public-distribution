"""Standalone portability transform for the public Archify skill export."""

from __future__ import annotations

import json
from pathlib import PurePosixPath


TRANSFORM_ID = "archify-standalone-v1"

_EXPECTED_BIN = {"archify": "./bin/archify.mjs"}
_EXPECTED_SCRIPTS = {
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
}
_EXPECTED_DEV_DEPENDENCIES = {
    "ajv": "^8.17.1",
    "parse5": "7.3.0",
    "saxes": "6.0.0",
    "simple-icons": "16.28.0",
}
_EXPECTED_OVERRIDES = {"fast-uri": "3.1.5"}


def _adapt_package_json(data: bytes) -> bytes:
    try:
        package = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("archify/package.json is not valid UTF-8 JSON") from error

    if not isinstance(package, dict):
        raise ValueError("archify/package.json must contain a JSON object")

    expected_identity = {
        "name": "archify",
        "private": True,
        "type": "module",
        "bin": _EXPECTED_BIN,
    }
    for key, expected in expected_identity.items():
        if package.get(key) != expected:
            raise ValueError(f"unsupported archify/package.json field: {key}")

    expected_fields = {
        "scripts": _EXPECTED_SCRIPTS,
        "devDependencies": _EXPECTED_DEV_DEPENDENCIES,
        "overrides": _EXPECTED_OVERRIDES,
    }
    for key, expected in expected_fields.items():
        if package.get(key) != expected:
            raise ValueError(f"unsupported archify/package.json field: {key}")

    # The copied skill is self-contained; these source-only fields advertise
    # commands that depend on excluded generators, tests, or repository files.
    for key in ("private", "scripts", "devDependencies", "overrides"):
        del package[key]

    return (json.dumps(package, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def adapt(path: PurePosixPath, data: bytes) -> bytes:
    """Adapt one file relative to archify/ for its standalone skill install."""
    if path == PurePosixPath("package.json"):
        return _adapt_package_json(data)
    return data
