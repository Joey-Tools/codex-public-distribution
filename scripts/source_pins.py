#!/usr/bin/env python3
"""Emit validated source revisions for native checkout actions."""

import json
from pathlib import Path
import re


SOURCES = {
    "codex-toolbox",
    "archify",
    "codex-review-workflows",
    "codex-workflow-hygiene",
    "codex-project-journal",
    "codex-debug-triage",
}


def main():
    lock = json.loads(Path("sources.lock.json").read_text(encoding="utf-8"))
    pins = {}
    for source in lock["sources"]:
        name, sha = source["name"], source["sha"]
        if (
            name not in SOURCES
            or name in pins
            or re.fullmatch(r"[0-9a-f]{40}", sha) is None
        ):
            raise ValueError(
                "source lock must contain unique expected names and full commit SHAs"
            )
        pins[name] = sha
    if set(pins) != SOURCES:
        raise ValueError("source lock must contain every configured public source")
    print("pins=" + json.dumps(pins, separators=(",", ":"), sort_keys=True))


if __name__ == "__main__":
    main()
