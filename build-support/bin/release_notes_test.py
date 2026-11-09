# Copyright 2026 Tague Griffith
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

from pathlib import Path

import release_notes as rn

_CHANGELOG = """\
# Changelog

Intro text.

## 1.2.0 (2026-11-09)

Adds a feature and
bumps a default.

- **First item** wraps
  onto a second line.
  - A nested item that
    also wraps.
- See [the docs](README.md#stability), [Pyrefly](https://pyrefly.org/),
  and [above](#changelog).

## 1.1.0 (2026-10-07)

Older release.
"""

_REPO = "https://github.com/o/r"


def test_extracts_unwraps_and_absolutizes() -> None:
    assert rn.release_notes(_CHANGELOG, "v1.2.0", _REPO) == (
        "Adds a feature and bumps a default.\n"
        "\n"
        "- **First item** wraps onto a second line.\n"
        "  - A nested item that also wraps.\n"
        "- See [the docs](https://github.com/o/r/blob/v1.2.0/README.md#stability),"
        " [Pyrefly](https://pyrefly.org/), and [above](#changelog).\n"
    )


def test_last_section_runs_to_end_of_file() -> None:
    assert rn.release_notes(_CHANGELOG, "v1.1.0", _REPO) == "Older release.\n"


def test_missing_version() -> None:
    assert rn.release_notes(_CHANGELOG, "v1.1.1", _REPO) is None
    # A prefix of a real version must not match it.
    assert rn.release_notes(_CHANGELOG, "v1.2", _REPO) is None


def test_main_reports_missing_version(tmp_path: Path, capsys) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(_CHANGELOG, encoding="utf-8")
    assert rn.main(["v9.9.9", "--changelog", str(changelog)]) == 1
    assert "no section for v9.9.9" in capsys.readouterr().err
    assert rn.main(["v1.1.0", "--changelog", str(changelog)]) == 0
    assert capsys.readouterr().out == "Older release.\n"
