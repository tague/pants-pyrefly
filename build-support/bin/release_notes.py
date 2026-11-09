#!/usr/bin/env python3
# Copyright 2026 Tague Griffith
# Licensed under the Apache License, Version 2.0 (see LICENSE).

"""Print the GitHub Release notes for a tag, taken from its `CHANGELOG.md` section.

The notes are the body of the `## X.Y.Z (date)` section for tag `vX.Y.Z`, rewritten for a release
page:

- Hard-wrapped lines are joined. GitHub renders a release body like a comment, where every newline
  is a line break, so the CHANGELOG's wrapping would show up as ragged lines.
- Relative links (`README.md#stability`) become absolute links to that file at the tag, since a
  release page has no directory to resolve them against.

Usage (run directly; pure stdlib, no Pants required):

    python3 build-support/bin/release_notes.py v1.2.0 > notes.md

Exits 1 if the CHANGELOG has no section for the tag's version.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_CHANGELOG = Path(__file__).resolve().parents[2] / "CHANGELOG.md"
_DEFAULT_REPO_URL = "https://github.com/tague/pants-pyrefly"

# A line that starts a new block rather than continuing the previous line's paragraph or item.
_BLOCK_START = re.compile(r"\s*([-*+]|\d+\.)\s|#")
# `](target)` where target is neither a URL (has a scheme) nor an in-page anchor.
_RELATIVE_LINK = re.compile(r"\]\((?![a-z][a-z0-9+.-]*:|#)([^)\s]+)\)")


def section(changelog: str, version: str) -> str | None:
    """Return the body of `version`'s `## ` section, or None if there is none."""
    lines: list[str] | None = None
    for line in changelog.splitlines():
        if line.startswith("## "):
            if lines is not None:
                break
            if line.split()[1:2] == [version]:
                lines = []
        elif lines is not None:
            lines.append(line)
    return None if lines is None else "\n".join(lines).strip("\n")


def unwrap(text: str) -> str:
    """Join each hard-wrapped paragraph or list item onto one line."""
    out: list[str] = []
    for line in text.splitlines():
        if out and out[-1].strip() and line.strip() and not _BLOCK_START.match(line):
            out[-1] = f"{out[-1]} {line.strip()}"
        else:
            out.append(line)
    return "\n".join(out)


def absolutize_links(text: str, base_url: str) -> str:
    return _RELATIVE_LINK.sub(lambda m: f"]({base_url}/{m.group(1)})", text)


def release_notes(changelog: str, tag: str, repo_url: str) -> str | None:
    body = section(changelog, tag.removeprefix("v"))
    if body is None:
        return None
    return absolutize_links(unwrap(body), f"{repo_url}/blob/{tag}") + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("tag", help="release tag, e.g. v1.2.0")
    parser.add_argument("--repo-url", default=_DEFAULT_REPO_URL, help="base for absolute links")
    parser.add_argument("--changelog", type=Path, default=_CHANGELOG)
    args = parser.parse_args(argv)

    notes = release_notes(args.changelog.read_text(encoding="utf-8"), args.tag, args.repo_url)
    if notes is None:
        print(f"error: {args.changelog} has no section for {args.tag}", file=sys.stderr)
        return 1
    sys.stdout.write(notes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
