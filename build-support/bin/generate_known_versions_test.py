# Copyright 2026 Tague Griffith
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

import json
from pathlib import Path

import pytest  # pants: no-infer-dep

import generate_known_versions as gkv

_SUBSYSTEMS = """\
MINIMUM_PINNED_VERSION = "1.1.1"


class Pyrefly(TemplatedExternalTool):
    options_scope = "pyrefly"

    default_version = "1.9.0"
    default_url_template = (
        "https://github.com/facebook/pyrefly/releases/download/{version}/pyrefly-{platform}.tar.gz"
    )
    default_url_platform_mapping = {
        "macos_arm64": "macos-arm64",
        "linux_x86_64": "linux-x86_64-musl",
    }
    default_known_versions = [
        "1.9.0|macos_arm64|aaaa|111",
        "1.9.0|linux_x86_64|bbbb|222",
    ]

    skip = SkipOption("check")
"""

# The fake GitHub release list, split over two API pages. Tags appear in publish order, not
# version order (a patch for an older line ships after a newer minor), as on the real repo.
_PAGES: list[list[tuple[str, bool, bool]]] = [
    # (tag, prerelease, draft)
    [
        ("1.11.0", False, False),  # newer than the default
        ("1.10.0-dev.1", True, False),  # pre-release
        ("1.9.0", False, False),  # the default
        ("1.2.1", False, False),  # older line, published after 1.9.0
        ("1.9.0-dev.2", True, False),
        ("1.3.0rc1", False, False),  # odd tag that is not flagged pre-release
    ],
    [
        ("1.8.0", False, True),  # draft
        ("1.2.0", False, False),
        ("1.1.1", False, False),  # the minimum
        ("1.1.0", False, False),  # below the minimum
        ("0.64.1", False, False),
    ],
]


def _asset_names() -> list[str]:
    return ["pyrefly-macos-arm64.tar.gz", "pyrefly-linux-x86_64-musl.tar.gz"]


def _sha(tag: str, name: str) -> str:
    return f"sha-{tag}-{name.split('-', 1)[1].split('.')[0]}"


def _size(tag: str, name: str) -> int:
    return 1000 + len(tag) * 10 + len(name)


def _release(tag: str, prerelease: bool, draft: bool) -> dict:
    return {
        "tag_name": tag,
        "prerelease": prerelease,
        "draft": draft,
        "assets": [
            {
                "name": name,
                "size": _size(tag, name),
                "browser_download_url": f"https://dl/{tag}/{name}",
            }
            for name in _asset_names()
        ],
    }


def _expected_pins(tags: list[str]) -> list[str]:
    pins = []
    for tag in tags:
        for pants_platform, name in zip(("macos_arm64", "linux_x86_64"), _asset_names()):
            pins.append(f"{tag}|{pants_platform}|{_sha(tag, name)}|{_size(tag, name)}")
    return pins


@pytest.fixture
def fake_github(monkeypatch) -> list[str]:
    """Serve `_PAGES` from the releases API; returns the list of URLs requested."""
    requested: list[str] = []
    pages = [[_release(*entry) for entry in page] for page in _PAGES]

    def fake_get(url: str, token: str | None) -> bytes:
        requested.append(url)
        for number in range(1, len(pages) + 2):
            if url == gkv.RELEASES_API.format(page=number):
                page = pages[number - 1] if number <= len(pages) else []
                return json.dumps(page).encode()
        if url.startswith("https://dl/") and url.endswith(".sha256"):
            tag, name = url[len("https://dl/") : -len(".sha256")].split("/")
            return f"{_sha(tag, name)}  {name}\n".encode()
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(gkv, "_get", fake_get)
    return requested


def _write_subsystems(tmp_path: Path, known_versions: list[str], default: str = "1.9.0") -> Path:
    text = _SUBSYSTEMS.replace('default_version = "1.9.0"', f'default_version = "{default}"')
    block = "\n".join(gkv.render_known_versions_block(known_versions))
    start = text.index("    default_known_versions = [")
    end = text.index("    ]\n", start) + len("    ]")
    path = tmp_path / "subsystems.py"
    path.write_text(text[:start] + block + text[end:])
    return path


def test_parse_plugin_config() -> None:
    config = gkv.parse_plugin_config(_SUBSYSTEMS)
    assert config.minimum_version == "1.1.1"
    assert config.version == "1.9.0"
    assert config.platform_mapping == {
        "macos_arm64": "macos-arm64",
        "linux_x86_64": "linux-x86_64-musl",
    }
    assert config.known_versions == ["1.9.0|macos_arm64|aaaa|111", "1.9.0|linux_x86_64|bbbb|222"]


def test_parse_requires_minimum_constant() -> None:
    with pytest.raises(ValueError, match="MINIMUM_PINNED_VERSION"):
        gkv.parse_plugin_config(_SUBSYSTEMS.replace('MINIMUM_PINNED_VERSION = "1.1.1"', ""))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1.3.2", (1, 3, 2)),
        ("10.0.11", (10, 0, 11)),
        ("1.4.0-dev.2", None),
        ("1.3.0rc1", None),
        ("1.3.0a1", None),
        ("1.3.0b2", None),
        ("1.3.0.dev4", None),
        ("v1.3.2", None),
        ("1.3", None),
    ],
)
def test_parse_stable_version(text: str, expected) -> None:
    assert gkv.parse_stable_version(text) == expected


def test_list_stable_releases_paginates_and_excludes_prereleases(fake_github) -> None:
    releases = gkv.list_stable_releases(token=None)
    assert sorted(releases) == ["0.64.1", "1.1.0", "1.1.1", "1.11.0", "1.2.0", "1.2.1", "1.9.0"]
    # Walked both pages, then stopped at the first empty one.
    assert fake_github == [gkv.RELEASES_API.format(page=n) for n in (1, 2, 3)]


def test_select_versions_filters_range_and_orders_numerically() -> None:
    tags = ["1.2.0", "1.10.0", "1.9.0", "1.1.0", "1.1.1", "1.11.0", "1.4.0-dev.1", "2.0.0"]
    # Inclusive at both ends; 1.10.0 sorts above 1.9.0 (numeric, not string, comparison).
    assert gkv.select_versions(tags, "1.1.1", "1.10.0") == [
        "1.10.0",
        "1.9.0",
        "1.2.0",
        "1.1.1",
    ]
    assert gkv.newer_than(tags, "1.10.0") == ["2.0.0", "1.11.0"]


def test_select_versions_rejects_inverted_range() -> None:
    with pytest.raises(ValueError, match="newer than the default"):
        gkv.select_versions(["1.2.0"], "1.3.0", "1.2.0")


def test_compute_known_versions(fake_github) -> None:
    config = gkv.parse_plugin_config(_SUBSYSTEMS)
    pins = gkv.compute_known_versions(config, "1.9.0", token=None)
    # Newest first, platforms in mapping order; nothing below the minimum, above the default,
    # pre-release, draft, or non-X.Y.Z.
    assert pins == _expected_pins(["1.9.0", "1.2.1", "1.2.0", "1.1.1"])


def test_compute_known_versions_requires_published_default(fake_github) -> None:
    config = gkv.parse_plugin_config(_SUBSYSTEMS)
    with pytest.raises(ValueError, match="not a published stable"):
        gkv.compute_known_versions(config, "1.10.0-dev.1", token=None)


def test_write_updates_version_and_pins(fake_github, tmp_path: Path) -> None:
    path = _write_subsystems(tmp_path, ["1.2.0|macos_arm64|old|1"], default="1.2.0")
    assert gkv.main(["--subsystems", str(path), "--version", "1.11.0", "--write"]) == 0

    reparsed = gkv.parse_plugin_config(path.read_text())
    assert reparsed.version == "1.11.0"
    assert reparsed.minimum_version == "1.1.1"
    assert reparsed.known_versions == _expected_pins(["1.11.0", "1.9.0", "1.2.1", "1.2.0", "1.1.1"])
    # Surrounding lines are preserved.
    assert 'skip = SkipOption("check")' in path.read_text()


def test_check_passes_on_current_pins_despite_newer_upstream(fake_github, tmp_path) -> None:
    # 1.11.0 exists upstream, but `--check` only validates [minimum, default].
    path = _write_subsystems(tmp_path, _expected_pins(["1.9.0", "1.2.1", "1.2.0", "1.1.1"]))
    assert gkv.main(["--subsystems", str(path), "--check"]) == 0


@pytest.mark.parametrize(
    ("known", "reason"),
    [
        (_expected_pins(["1.9.0", "1.2.0", "1.1.1"]), "missing pins for: 1.2.1"),
        (_expected_pins(["1.9.0", "1.2.1", "1.2.0", "1.1.1", "1.1.0"]), "unexpected pins for"),
        (_expected_pins(["1.9.0", "1.2.1", "1.2.0", "1.1.1", "1.11.0"]), "unexpected pins for"),
        (
            [
                p.replace("sha-1.2.0-macos", "tampered")
                for p in _expected_pins(["1.9.0", "1.2.1", "1.2.0", "1.1.1"])
            ],
            "do not match the release",
        ),
        (_expected_pins(["1.1.1", "1.2.0", "1.2.1", "1.9.0"]), "canonical order"),
    ],
)
def test_check_fails_on_drift(fake_github, tmp_path, capsys, known, reason) -> None:
    path = _write_subsystems(tmp_path, known)
    assert gkv.main(["--subsystems", str(path), "--check"]) == 1
    assert reason in capsys.readouterr().err


def test_check_upstream(fake_github, tmp_path, capsys) -> None:
    pins = _expected_pins(["1.9.0", "1.2.1", "1.2.0", "1.1.1"])
    behind = _write_subsystems(tmp_path, pins)
    assert gkv.main(["--subsystems", str(behind), "--check-upstream"]) == 1
    assert "1.11.0 is newer than the pinned default 1.9.0" in capsys.readouterr().err

    latest = _write_subsystems(tmp_path, pins, default="1.11.0")
    assert gkv.main(["--subsystems", str(latest), "--check-upstream"]) == 0
