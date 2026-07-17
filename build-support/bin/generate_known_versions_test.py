# Copyright 2026 Tague Griffith
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

import json
from pathlib import Path

import generate_known_versions as gkv

_SUBSYSTEMS = """\
class Pyrefly(TemplatedExternalTool):
    options_scope = "pyrefly"

    default_version = "1.1.1"
    default_url_template = (
        "https://github.com/facebook/pyrefly/releases/download/{version}/pyrefly-{platform}.tar.gz"
    )
    default_url_platform_mapping = {
        "macos_arm64": "macos-arm64",
        "linux_x86_64": "linux-x86_64-musl",
    }
    default_known_versions = [
        "1.1.1|macos_arm64|aaaa|111",
        "1.1.1|linux_x86_64|bbbb|222",
    ]

    skip = SkipOption("check")
"""


def test_parse_plugin_config() -> None:
    config = gkv.parse_plugin_config(_SUBSYSTEMS)
    assert config.version == "1.1.1"
    assert config.platform_mapping == {
        "macos_arm64": "macos-arm64",
        "linux_x86_64": "linux-x86_64-musl",
    }
    assert config.known_versions == [
        "1.1.1|macos_arm64|aaaa|111",
        "1.1.1|linux_x86_64|bbbb|222",
    ]


def _fake_get(monkeypatch, version: str) -> None:
    # Asset name -> (size, sha256 sidecar bytes).
    assets = {
        "pyrefly-macos-arm64.tar.gz": (12345, b"deadbeef  pyrefly-macos-arm64.tar.gz\n"),
        "pyrefly-linux-x86_64-musl.tar.gz": (
            67890,
            b"cafef00d  pyrefly-linux-x86_64-musl.tar.gz\n",
        ),
    }
    release = {
        "assets": [
            {"name": name, "size": size, "browser_download_url": f"https://dl/{name}"}
            for name, (size, _sha) in assets.items()
        ]
    }

    def fake_get(url: str, token: str | None) -> bytes:
        if url == gkv.RELEASE_API.format(version=version):
            return json.dumps(release).encode()
        for name, (_size, sha) in assets.items():
            if url == f"https://dl/{name}.sha256":
                return sha
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(gkv, "_get", fake_get)


def test_compute_known_versions(monkeypatch) -> None:
    _fake_get(monkeypatch, "1.1.1")
    config = gkv.parse_plugin_config(_SUBSYSTEMS)
    pins = gkv.compute_known_versions(config, "1.1.1", token=None)
    # Emitted in platform-mapping order, sha256 from the sidecar, size from the release API.
    assert pins == [
        "1.1.1|macos_arm64|deadbeef|12345",
        "1.1.1|linux_x86_64|cafef00d|67890",
    ]


def test_write_updates_version_and_pins(monkeypatch, tmp_path: Path) -> None:
    _fake_get(monkeypatch, "2.0.0")
    path = tmp_path / "subsystems.py"
    path.write_text(_SUBSYSTEMS)
    config = gkv.parse_plugin_config(path.read_text())
    pins = gkv.compute_known_versions(config, "2.0.0", token=None)
    gkv.rewrite_subsystems(path, config, "2.0.0", pins)

    # The rewrite is parseable and reflects the new version + pins.
    reparsed = gkv.parse_plugin_config(path.read_text())
    assert reparsed.version == "2.0.0"
    assert reparsed.known_versions == [
        "2.0.0|macos_arm64|deadbeef|12345",
        "2.0.0|linux_x86_64|cafef00d|67890",
    ]
    # Surrounding lines are preserved.
    assert 'skip = SkipOption("check")' in path.read_text()
