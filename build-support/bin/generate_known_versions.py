#!/usr/bin/env python3
# Copyright 2026 Tague Griffith
# Licensed under the Apache License, Version 2.0 (see LICENSE).

"""Generate or verify the Pyrefly `default_known_versions` pins in `subsystems.py`.

Each pin is `"<version>|<pants_platform>|<sha256>|<size_bytes>"`. The plugin ships pins for every
stable Pyrefly release from `MINIMUM_PINNED_VERSION` up to `Pyrefly.default_version` (inclusive),
newest version first, platforms in `default_url_platform_mapping` order. Pre-releases
(`X.Y.Z-dev.N` and the like) and drafts are never pinned.

This reads the plugin's own `subsystems.py` via `ast`, so the script and the plugin can never
disagree on the minimum, the default, the URL template, or the platform mapping. It lists the
facebook/pyrefly GitHub releases, fetches each asset's published `.sha256` sidecar and size, and
emits the pins.

Usage (run directly; pure stdlib, no Pants required):

    GEN=build-support/bin/generate_known_versions.py
    python3 $GEN                         # print the pins for the current range
    python3 $GEN --version 1.4.0         # print the pins with 1.4.0 as the default
    python3 $GEN --write                 # rewrite subsystems.py for the current range
    python3 $GEN --version 1.4.0 --write # bump the default to 1.4.0 and rewrite all pins
    python3 $GEN --check                 # CI: fail if any pin in [minimum, default] is wrong,
                                         #     missing, or extra
    python3 $GEN --check-upstream        # fail if a stable Pyrefly newer than the default exists

`--check` deliberately ignores releases newer than the default, so a new Pyrefly release never
turns unrelated CI red; `--check-upstream` is the opt-in signal that a bump is available.

Set `GITHUB_TOKEN` (or pass `--token`) to raise the GitHub API rate limit.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import sys
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SUBSYSTEMS = REPO_ROOT / "pants-plugins" / "pants_pyrefly" / "subsystems.py"
RELEASES_API = "https://api.github.com/repos/facebook/pyrefly/releases?per_page=100&page={page}"
MINIMUM_CONSTANT = "MINIMUM_PINNED_VERSION"

# A stable release tag is exactly `X.Y.Z`; anything else (`1.4.0-dev.2`, `1.3.0rc1`, …) is not.
_STABLE_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

Version = tuple[int, int, int]


def parse_stable_version(text: str) -> Version | None:
    """Return `(major, minor, patch)` for a stable `X.Y.Z` version, else None."""
    match = _STABLE_VERSION_RE.match(text.strip())
    if not match:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def _require_stable(text: str, what: str) -> Version:
    version = parse_stable_version(text)
    if version is None:
        raise ValueError(f"{what} `{text}` is not a stable X.Y.Z version")
    return version


@dataclass
class PluginConfig:
    """The Pyrefly download config parsed out of `subsystems.py`."""

    minimum_version: str
    version: str
    url_template: str
    platform_mapping: dict[str, str]
    known_versions: list[str]
    # 1-based inclusive line spans of the two assignments we rewrite with `--write`.
    version_span: tuple[int, int]
    known_versions_span: tuple[int, int]


def _find_class(tree: ast.Module, name: str) -> ast.ClassDef:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise ValueError(f"class `{name}` not found")


def _assignments(body: Iterable[ast.stmt]) -> dict[str, ast.Assign]:
    out: dict[str, ast.Assign] = {}
    for node in body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                out[target.id] = node
    return out


def parse_plugin_config(text: str) -> PluginConfig:
    tree = ast.parse(text)
    module_assigns = _assignments(tree.body)
    if MINIMUM_CONSTANT not in module_assigns:
        raise ValueError(f"module-level `{MINIMUM_CONSTANT}` not found in subsystems.py")
    assigns = _assignments(_find_class(tree, "Pyrefly").body)
    for required in (
        "default_version",
        "default_url_template",
        "default_url_platform_mapping",
        "default_known_versions",
    ):
        if required not in assigns:
            raise ValueError(f"`Pyrefly.{required}` not found in subsystems.py")

    version_node = assigns["default_version"]
    known_node = assigns["default_known_versions"]
    return PluginConfig(
        minimum_version=ast.literal_eval(module_assigns[MINIMUM_CONSTANT].value),
        version=ast.literal_eval(version_node.value),
        url_template=ast.literal_eval(assigns["default_url_template"].value),
        platform_mapping=ast.literal_eval(assigns["default_url_platform_mapping"].value),
        known_versions=ast.literal_eval(known_node.value),
        version_span=(version_node.lineno, version_node.end_lineno or version_node.lineno),
        known_versions_span=(known_node.lineno, known_node.end_lineno or known_node.lineno),
    )


def _get(url: str, token: str | None) -> bytes:
    headers = {"User-Agent": "pants-pyrefly-known-versions"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request) as response:
        return response.read()


def list_stable_releases(token: str | None) -> dict[str, dict]:
    """Every stable (non-draft, non-prerelease, `X.Y.Z`-tagged) release, keyed by tag.

    Walks the paginated releases API until an empty page comes back.
    """
    releases: dict[str, dict] = {}
    page = 1
    while True:
        batch = json.loads(_get(RELEASES_API.format(page=page), token))
        if not batch:
            return releases
        for release in batch:
            tag = release.get("tag_name", "")
            if release.get("draft") or release.get("prerelease"):
                continue
            if parse_stable_version(tag) is None:
                continue
            releases[tag] = release
        page += 1


def select_versions(stable_tags: Iterable[str], minimum: str, default: str) -> list[str]:
    """The stable versions in `[minimum, default]`, newest first (numeric, not string, order)."""
    low = _require_stable(minimum, "minimum version")
    high = _require_stable(default, "default version")
    if low > high:
        raise ValueError(f"minimum version {minimum} is newer than the default {default}")
    in_range = [
        tag
        for tag in stable_tags
        if (parsed := parse_stable_version(tag)) is not None and low <= parsed <= high
    ]
    return sorted(in_range, key=lambda tag: parse_stable_version(tag) or (0, 0, 0), reverse=True)


def newer_than(stable_tags: Iterable[str], default: str) -> list[str]:
    """Stable versions strictly newer than `default`, newest first."""
    high = _require_stable(default, "default version")
    newer = [
        tag
        for tag in stable_tags
        if (parsed := parse_stable_version(tag)) is not None and parsed > high
    ]
    return sorted(newer, key=lambda tag: parse_stable_version(tag) or (0, 0, 0), reverse=True)


def _asset_filename(url_template: str, version: str, url_platform: str) -> str:
    return url_template.format(version=version, platform=url_platform).rsplit("/", 1)[-1]


def pins_for_release(config: PluginConfig, release: dict, token: str | None) -> list[str]:
    """Fetch sha256 + size for each mapped platform and return the pin lines, in mapping order."""
    version = release["tag_name"]
    sizes = {asset["name"]: asset["size"] for asset in release.get("assets", [])}
    downloads = {
        asset["name"]: asset["browser_download_url"] for asset in release.get("assets", [])
    }

    pins: list[str] = []
    for pants_platform, url_platform in config.platform_mapping.items():
        filename = _asset_filename(config.url_template, version, url_platform)
        if filename not in downloads:
            raise ValueError(f"release {version} has no asset named `{filename}`")
        asset_url = downloads[filename]

        try:
            # Cheap path: read the published `<asset>.sha256` sidecar (first token is the digest).
            sha256 = _get(f"{asset_url}.sha256", token).split()[0].decode()
            size = sizes[filename]
        except Exception:
            # Fallback: download the asset and compute both locally.
            blob = _get(asset_url, token)
            sha256 = hashlib.sha256(blob).hexdigest()
            size = len(blob)
        pins.append(f"{version}|{pants_platform}|{sha256}|{size}")
    return pins


def compute_known_versions(
    config: PluginConfig,
    default: str,
    token: str | None,
    releases: dict[str, dict] | None = None,
) -> list[str]:
    """All pins for the stable releases in `[config.minimum_version, default]`, newest first."""
    if releases is None:
        releases = list_stable_releases(token)
    if default not in releases:
        raise ValueError(f"default version {default} is not a published stable Pyrefly release")
    pins: list[str] = []
    for version in select_versions(releases, config.minimum_version, default):
        pins.extend(pins_for_release(config, releases[version], token))
    return pins


def _pin_version(pin: str) -> str:
    return pin.split("|", 1)[0]


def describe_drift(expected: list[str], found: list[str]) -> list[str]:
    """Human-readable reasons `found` differs from `expected` (empty if they match exactly)."""
    if expected == found:
        return []
    problems: list[str] = []
    expected_versions = list(dict.fromkeys(_pin_version(pin) for pin in expected))
    found_versions = list(dict.fromkeys(_pin_version(pin) for pin in found))
    missing = [v for v in expected_versions if v not in found_versions]
    extra = [v for v in found_versions if v not in expected_versions]
    if missing:
        problems.append(f"missing pins for: {', '.join(missing)}")
    if extra:
        problems.append(f"unexpected pins for: {', '.join(extra)}")
    wrong = sorted(set(found) - set(expected), key=found.index)
    wrong = [pin for pin in wrong if _pin_version(pin) not in extra]
    if wrong:
        problems.append("pins that do not match the release:")
        problems.extend(f"  {pin}" for pin in wrong)
    absent = [pin for pin in expected if pin not in found and _pin_version(pin) not in missing]
    if absent:
        problems.append("expected pins not present:")
        problems.extend(f"  {pin}" for pin in absent)
    if not problems:
        problems.append("pins are correct but not in canonical order (newest version first)")
    return problems


def render_known_versions_block(pins: list[str]) -> list[str]:
    lines = ["    default_known_versions = ["]
    lines += [f'        "{pin}",' for pin in pins]
    lines.append("    ]")
    return lines


def rewrite_subsystems(path: Path, config: PluginConfig, version: str, pins: list[str]) -> None:
    lines = path.read_text().splitlines()
    # Replace the later assignment first so the earlier span's line numbers stay valid.
    kv_start, kv_end = config.known_versions_span
    lines[kv_start - 1 : kv_end] = render_known_versions_block(pins)
    v_start, v_end = config.version_span
    lines[v_start - 1 : v_end] = [f'    default_version = "{version}"']
    path.write_text("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--subsystems",
        type=Path,
        default=DEFAULT_SUBSYSTEMS,
        help="Path to subsystems.py (default: the plugin's).",
    )
    parser.add_argument(
        "--version",
        default=None,
        help="Pyrefly version to make the default (default: the current `default_version`).",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="Rewrite subsystems.py in place.")
    mode.add_argument(
        "--check",
        action="store_true",
        help=(
            "Exit non-zero if any pin in [minimum, default] is wrong, missing, or extra (for CI). "
            "Releases newer than the default are ignored. Ignores --version."
        ),
    )
    mode.add_argument(
        "--check-upstream",
        action="store_true",
        help="Exit non-zero if a stable Pyrefly release newer than the default exists.",
    )
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN"), help="GitHub API token.")
    args = parser.parse_args(argv)

    config = parse_plugin_config(args.subsystems.read_text())
    releases = list_stable_releases(args.token)

    if args.check_upstream:
        newer = newer_than(releases, config.version)
        if newer:
            print(
                f"Pyrefly {', '.join(newer)} is newer than the pinned default {config.version}.\n"
                "Bump with `python3 build-support/bin/generate_known_versions.py "
                f"--version {newer[0]} --write`.",
                file=sys.stderr,
            )
            return 1
        print(f"Pyrefly {config.version} is the newest stable release.")
        return 0

    if args.check:
        expected = compute_known_versions(config, config.version, args.token, releases)
        problems = describe_drift(expected, config.known_versions)
        if problems:
            print(
                f"Pyrefly pins are stale for [{config.minimum_version}, {config.version}]:",
                file=sys.stderr,
            )
            for problem in problems:
                print(f"  {problem}", file=sys.stderr)
            print(
                "Regenerate with `python3 build-support/bin/generate_known_versions.py --write`.",
                file=sys.stderr,
            )
            return 1
        versions = list(dict.fromkeys(_pin_version(pin) for pin in expected))
        print(
            f"Pyrefly pins are current: {len(expected)} pins for {len(versions)} versions "
            f"({', '.join(versions)})."
        )
        return 0

    version = args.version or config.version
    pins = compute_known_versions(config, version, args.token, releases)

    if args.write:
        rewrite_subsystems(args.subsystems, config, version, pins)
        versions = list(dict.fromkeys(_pin_version(pin) for pin in pins))
        print(
            f"Wrote {len(pins)} Pyrefly pin(s) for {', '.join(versions)} (default {version}) "
            f"to {args.subsystems}."
        )
        return 0

    print("\n".join(render_known_versions_block(pins)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
