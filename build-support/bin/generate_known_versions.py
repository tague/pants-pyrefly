#!/usr/bin/env python3
# Copyright 2026 Tague Griffith
# Licensed under the Apache License, Version 2.0 (see LICENSE).

"""Maintain the Pyrefly `default_known_versions` pins in `subsystems.py`.

Each pin is `"<version>|<pants_platform>|<sha256>|<size_bytes>"`, ordered newest version first and
platforms in `default_url_platform_mapping` order. The plugin pins stable Pyrefly releases from
`MINIMUM_PINNED_VERSION` up to (at least) `Pyrefly.default_version`, except the ones recorded in
`DENYLISTED_VERSIONS` (version -> reason). Pre-releases (`X.Y.Z-dev.N` etc.) and drafts are never
pinned.

Everything is read from the plugin's own `subsystems.py` via `ast`, so the script and the plugin can
never disagree on the minimum, denylist, default, URL template, or platform mapping.

Usage (run directly; pure stdlib, no Pants required):

    GEN=build-support/bin/generate_known_versions.py
    python3 $GEN --write                  # add pins for unpinned stable releases up to the default
    python3 $GEN --version 1.4.0 --write  # make 1.4.0 the default and add the missing pins
    python3 $GEN --check                  # CI: verify the committed pins (see below)
    python3 $GEN --check-upstream         # list stable releases that are neither pinned nor
                                          #   denylisted (exit 1 if any)
    python3 $GEN --remove 1.2.2 --reason "miscompiles X"  # drop pins + denylist (only removal path)
    python3 $GEN --list-versions          # JSON list of supported versions, for a CI matrix

`--write` only adds: it never removes or rewrites an existing pin, and it reports (exit 1) any
existing pin that disagrees with the release instead of overwriting it.

`--check` verifies only what ships: every committed pin's sha256/size matches its release, the
default is pinned, the order is canonical, nothing is below the minimum, and no denylisted version
is pinned or the default. An unpinned upstream release (newer, or an in-range backport) never fails
`--check`; that is `--check-upstream`'s job.

Set `GITHUB_TOKEN` (or pass `--token`) to raise the GitHub API rate limit. The token is only ever
sent to api.github.com, never to release-asset downloads or along redirects.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import socket
import sys
import textwrap
import time
import urllib.error
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SUBSYSTEMS = REPO_ROOT / "pants-plugins" / "pants_pyrefly" / "subsystems.py"
RELEASES_API = "https://api.github.com/repos/facebook/pyrefly/releases?per_page=100&page={page}"
API_HOST = "api.github.com"
MINIMUM_CONSTANT = "MINIMUM_PINNED_VERSION"
DENYLIST_CONSTANT = "DENYLISTED_VERSIONS"

# Network retry policy: 3 attempts total, sleeping 1s then 2s, only on transient failures.
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (1, 2)
REQUEST_TIMEOUT_SECONDS = 60

# Indirections so tests can mock the network and skip real sleeping.
_urlopen = urllib.request.urlopen
_sleep = time.sleep

# A stable release tag is exactly `X.Y.Z`; anything else (`1.4.0-dev.2`, `1.3.0rc1`, …) is not.
_STABLE_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

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


def _version_key(version: str) -> Version:
    return _require_stable(version, "version")


# ---
# Reading / writing subsystems.py
# ---


Span = tuple[int, int]


@dataclass
class PluginConfig:
    """The Pyrefly download config parsed out of `subsystems.py`."""

    minimum_version: str
    denylist: dict[str, str]
    version: str
    url_template: str
    platform_mapping: dict[str, str]
    known_versions: list[str]
    # 1-based inclusive line spans of the assignments the script rewrites.
    denylist_span: Span
    version_span: Span
    known_versions_span: Span


def _find_class(tree: ast.Module, name: str) -> ast.ClassDef:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise ValueError(f"class `{name}` not found")


def _assignments(body: Iterable[ast.stmt]) -> dict[str, ast.Assign | ast.AnnAssign]:
    out: dict[str, ast.Assign | ast.AnnAssign] = {}
    for node in body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                out[target.id] = node
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            if isinstance(node.target, ast.Name):
                out[node.target.id] = node
    return out


def _value(node: ast.Assign | ast.AnnAssign):
    assert node.value is not None
    return ast.literal_eval(node.value)


def _span(node: ast.stmt) -> Span:
    return node.lineno, node.end_lineno or node.lineno


def parse_plugin_config(text: str) -> PluginConfig:
    tree = ast.parse(text)
    module_assigns = _assignments(tree.body)
    for required in (MINIMUM_CONSTANT, DENYLIST_CONSTANT):
        if required not in module_assigns:
            raise ValueError(f"module-level `{required}` not found in subsystems.py")
    assigns = _assignments(_find_class(tree, "Pyrefly").body)
    for required in (
        "default_version",
        "default_url_template",
        "default_url_platform_mapping",
        "default_known_versions",
    ):
        if required not in assigns:
            raise ValueError(f"`Pyrefly.{required}` not found in subsystems.py")

    denylist = _value(module_assigns[DENYLIST_CONSTANT])
    if not isinstance(denylist, dict):
        raise ValueError(f"`{DENYLIST_CONSTANT}` must be a dict of version -> reason")
    return PluginConfig(
        minimum_version=_value(module_assigns[MINIMUM_CONSTANT]),
        denylist=denylist,
        version=_value(assigns["default_version"]),
        url_template=_value(assigns["default_url_template"]),
        platform_mapping=_value(assigns["default_url_platform_mapping"]),
        known_versions=_value(assigns["default_known_versions"]),
        denylist_span=_span(module_assigns[DENYLIST_CONSTANT]),
        version_span=_span(assigns["default_version"]),
        known_versions_span=_span(assigns["default_known_versions"]),
    )


def render_known_versions_block(pins: list[str]) -> list[str]:
    lines = ["    default_known_versions = ["]
    lines += [f'        "{pin}",' for pin in pins]
    lines.append("    ]")
    return lines


def render_denylist_block(denylist: dict[str, str]) -> list[str]:
    head = f"{DENYLIST_CONSTANT}: dict[str, str] = "
    if not denylist:
        return [head + "{}"]
    lines = [head + "{"]
    for version in sorted(denylist, key=_version_key, reverse=True):
        key = json.dumps(version)
        value = json.dumps(denylist[version])
        one_line = f"    {key}: {value},"
        if len(one_line) <= 100:
            lines.append(one_line)
            continue
        # Too long for the 100-column lint limit: split into implicitly concatenated pieces.
        pieces = textwrap.wrap(
            denylist[version], width=80, drop_whitespace=False, break_on_hyphens=False
        )
        lines.append(f"    {key}: (")
        lines += [f"        {json.dumps(piece)}" for piece in pieces]
        lines.append("    ),")
    lines.append("}")
    return lines


def rewrite_subsystems(
    path: Path,
    config: PluginConfig,
    *,
    version: str,
    pins: list[str],
    denylist: dict[str, str],
) -> None:
    lines = path.read_text().splitlines()
    replacements = [
        (config.known_versions_span, render_known_versions_block(pins)),
        (config.version_span, [f'    default_version = "{version}"']),
        (config.denylist_span, render_denylist_block(denylist)),
    ]
    # Replace bottom-up so earlier spans' line numbers stay valid.
    for (start, end), new_lines in sorted(replacements, key=lambda r: r[0][0], reverse=True):
        lines[start - 1 : end] = new_lines
    path.write_text("\n".join(lines) + "\n")


# ---
# Pins: parsing, ordering, merging
# ---


def pin_version(pin: str) -> str:
    return pin.split("|", 1)[0]


def pin_platform(pin: str) -> str:
    return pin.split("|", 2)[1]


def pinned_versions(pins: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(pin_version(pin) for pin in pins))


def _pin_sort_key(pin: str, platform_mapping: dict[str, str]) -> tuple:
    platforms = list(platform_mapping)
    version = parse_stable_version(pin_version(pin)) or (-1, -1, -1)
    platform = pin_platform(pin)
    index = platforms.index(platform) if platform in platforms else len(platforms)
    # Newest version first, then platforms in mapping order.
    return tuple(-part for part in version), index


def canonical_order(pins: list[str], platform_mapping: dict[str, str]) -> list[str]:
    return sorted(pins, key=lambda pin: _pin_sort_key(pin, platform_mapping))


def insert_pins(existing: list[str], new: list[str], platform_mapping: dict[str, str]) -> list[str]:
    """Insert `new` pins at their canonical positions without moving or editing `existing` ones."""
    merged = list(existing)
    for pin in canonical_order(new, platform_mapping):
        key = _pin_sort_key(pin, platform_mapping)
        index = next(
            (i for i, other in enumerate(merged) if _pin_sort_key(other, platform_mapping) > key),
            len(merged),
        )
        merged.insert(index, pin)
    return merged


# ---
# Network
# ---


class _TransientError(Exception):
    pass


def _is_transient(error: Exception) -> bool:
    if isinstance(error, urllib.error.HTTPError):
        return error.code == 429 or error.code >= 500
    # URLError wraps connection failures (refused, DNS, reset); timeouts surface as
    # socket.timeout / TimeoutError either directly or wrapped in URLError.
    return isinstance(error, (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError))


def _request(url: str, token: str | None) -> urllib.request.Request:
    request = urllib.request.Request(url, headers={"User-Agent": "pants-pyrefly-known-versions"})
    if token and urlparse(url).hostname == API_HOST:
        # Unredirected: urllib does not copy it onto a redirected request, so the token can never
        # follow a redirect off api.github.com.
        request.add_unredirected_header("Authorization", f"Bearer {token}")
    return request


def _get(url: str, token: str | None) -> bytes:
    """GET `url`, retrying transient failures (5xx, 429, connection errors, timeouts)."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with _urlopen(_request(url, token), timeout=REQUEST_TIMEOUT_SECONDS) as response:
                return response.read()
        except Exception as error:
            if not _is_transient(error) or attempt == MAX_ATTEMPTS:
                raise
            delay = BACKOFF_SECONDS[attempt - 1]
            print(f"warning: {url}: {error}; retrying in {delay}s", file=sys.stderr)
            _sleep(delay)
    raise AssertionError("unreachable")


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


def _asset_filename(url_template: str, version: str, url_platform: str) -> str:
    return url_template.format(version=version, platform=url_platform).rsplit("/", 1)[-1]


def parse_sha256_sidecar(content: bytes, asset: str) -> str:
    """The digest from a `<asset>.sha256` sidecar (`<hex>  <name>` or just `<hex>`)."""
    tokens = content.split()
    digest = tokens[0].decode(errors="replace").lower() if tokens else ""
    if not _SHA256_RE.match(digest):
        raise ValueError(
            f"`{asset}.sha256` does not start with a 64-character hex sha256 (got {digest[:80]!r})"
        )
    return digest


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
            sidecar = _get(f"{asset_url}.sha256", token)
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
            # No published sidecar: download the asset and compute both locally.
            blob = _get(asset_url, token)
            pins.append(
                f"{version}|{pants_platform}|{hashlib.sha256(blob).hexdigest()}|{len(blob)}"
            )
            continue
        sha256 = parse_sha256_sidecar(sidecar, filename)
        pins.append(f"{version}|{pants_platform}|{sha256}|{sizes[filename]}")
    return pins


# ---
# Modes
# ---


def verify_pins(
    config: PluginConfig, releases: dict[str, dict], token: str | None
) -> tuple[list[str], dict[str, list[str]]]:
    """Check the committed pins. Returns (problems, upstream pins by version)."""
    problems: list[str] = []
    pins = config.known_versions
    versions = pinned_versions(pins)
    platforms = list(config.platform_mapping)
    minimum = _version_key(config.minimum_version)

    if config.version not in versions:
        problems.append(f"the default version {config.version} is not pinned")
    if config.version in config.denylist:
        problems.append(f"the default version {config.version} is denylisted")
    for version in versions:
        if version in config.denylist:
            problems.append(f"{version} is pinned but denylisted: {config.denylist[version]}")
        parsed = parse_stable_version(version)
        if parsed is None:
            problems.append(f"{version} is pinned but is not a stable X.Y.Z version")
            continue
        if parsed < minimum:
            problems.append(
                f"{version} is pinned but older than MINIMUM_PINNED_VERSION "
                f"{config.minimum_version}"
            )
        found_platforms = [pin_platform(pin) for pin in pins if pin_version(pin) == version]
        if found_platforms != platforms and sorted(found_platforms) != sorted(platforms):
            problems.append(
                f"{version} must have exactly one pin per platform {platforms}, "
                f"found {found_platforms}"
            )
    if pins != canonical_order(pins, config.platform_mapping):
        problems.append("pins are not in canonical order (newest version first, mapping order)")

    upstream: dict[str, list[str]] = {}
    for version in versions:
        if parse_stable_version(version) is None:
            continue
        if version not in releases:
            problems.append(f"{version} is pinned but is not a published stable Pyrefly release")
            continue
        upstream[version] = pins_for_release(config, releases[version], token)
        mismatched = [pin for pin in pins if pin_version(pin) == version]
        mismatched = [pin for pin in mismatched if pin not in upstream[version]]
        for pin in mismatched:
            expected = [p for p in upstream[version] if pin_platform(p) == pin_platform(pin)]
            problems.append(
                f"pin does not match the release: {pin} (release: "
                f"{expected[0] if expected else 'no such platform'})"
            )
    return problems, upstream


def unpinned_upstream(config: PluginConfig, releases: Iterable[str]) -> list[str]:
    """Stable releases >= the minimum that are neither pinned nor denylisted, newest first."""
    minimum = _version_key(config.minimum_version)
    pinned = set(pinned_versions(config.known_versions))
    candidates = [
        tag
        for tag in releases
        if (parsed := parse_stable_version(tag)) is not None
        and parsed >= minimum
        and tag not in pinned
        and tag not in config.denylist
    ]
    return sorted(candidates, key=_version_key, reverse=True)


def supported_versions(config: PluginConfig) -> list[str]:
    """Pinned minus denylisted, newest first."""
    return [v for v in pinned_versions(config.known_versions) if v not in config.denylist]


def run_check(config: PluginConfig, token: str | None) -> int:
    releases = list_stable_releases(token)
    problems, _ = verify_pins(config, releases, token)
    if problems:
        print("Pyrefly pins are invalid:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    versions = pinned_versions(config.known_versions)
    print(
        f"Pyrefly pins are valid: {len(config.known_versions)} pins for {len(versions)} versions "
        f"({', '.join(versions)}); default {config.version}."
    )
    return 0


def run_check_upstream(config: PluginConfig, token: str | None) -> int:
    missing = unpinned_upstream(config, list_stable_releases(token))
    if not missing:
        print(f"Every stable Pyrefly release >= {config.minimum_version} is pinned or denylisted.")
        return 0
    default = _version_key(config.version)
    newer = [v for v in missing if _version_key(v) > default]
    in_range = [v for v in missing if _version_key(v) <= default]
    print("Stable Pyrefly releases that are neither pinned nor denylisted:", file=sys.stderr)
    if newer:
        print(f"  newer than the default {config.version}: {', '.join(newer)}", file=sys.stderr)
    if in_range:
        print(f"  at or below the default (backports): {', '.join(in_range)}", file=sys.stderr)
    print(
        "Pin with `--write` (plus `--version X` to move the default), or deliberately skip one "
        'with `--remove X --reason "..."`.',
        file=sys.stderr,
    )
    return 1


def run_write(path: Path, config: PluginConfig, version: str, token: str | None) -> int:
    target = _require_stable(version, "--version")
    if target < _version_key(config.minimum_version):
        raise SystemExit(
            f"error: {version} is older than MINIMUM_PINNED_VERSION {config.minimum_version}"
        )
    if version in config.denylist:
        raise SystemExit(f"error: {version} is denylisted: {config.denylist[version]}")
    releases = list_stable_releases(token)
    if version not in releases:
        raise SystemExit(f"error: {version} is not a published stable Pyrefly release")

    # Existing pins are kept verbatim; disagreements are reported, never overwritten.
    problems, _ = verify_pins(config, releases, token)
    mismatches = [p for p in problems if p.startswith("pin does not match")]

    minimum = _version_key(config.minimum_version)
    pinned = set(pinned_versions(config.known_versions))
    to_add = sorted(
        (
            tag
            for tag in releases
            if minimum <= _version_key(tag) <= target
            and tag not in pinned
            and tag not in config.denylist
        ),
        key=_version_key,
        reverse=True,
    )
    new_pins = [pin for tag in to_add for pin in pins_for_release(config, releases[tag], token)]
    merged = insert_pins(config.known_versions, new_pins, config.platform_mapping)
    rewrite_subsystems(path, config, version=version, pins=merged, denylist=config.denylist)

    added = ", ".join(to_add) if to_add else "none"
    print(f"Set default_version = {version}; added pins for: {added}.")
    if mismatches:
        print("Existing pins that disagree with the release were NOT changed:", file=sys.stderr)
        for problem in mismatches:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    return 0


def run_remove(path: Path, config: PluginConfig, version: str, reason: str) -> int:
    _require_stable(version, "--remove")
    if not reason.strip():
        raise SystemExit("error: --remove needs a non-empty --reason")
    if version == config.version:
        raise SystemExit(
            f"error: refusing to remove the default version {version}; move the default first "
            "(`--version X --write`)"
        )
    if version == config.minimum_version:
        raise SystemExit(
            f"error: refusing to remove MINIMUM_PINNED_VERSION {version}; raise "
            "MINIMUM_PINNED_VERSION in subsystems.py deliberately instead (with a CHANGELOG note)"
        )
    remaining = [pin for pin in config.known_versions if pin_version(pin) != version]
    removed = len(config.known_versions) - len(remaining)
    denylist = {**config.denylist, version: reason.strip()}
    rewrite_subsystems(path, config, version=config.version, pins=remaining, denylist=denylist)
    print(f"Removed {removed} pin(s) for {version} and denylisted it: {reason.strip()}")
    return 0


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
        help="With --write: the Pyrefly version to make the default (default: unchanged).",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="Add missing pins (never removes).")
    mode.add_argument("--check", action="store_true", help="Verify the committed pins.")
    mode.add_argument(
        "--check-upstream",
        action="store_true",
        help="Exit 1 if a stable release >= the minimum is neither pinned nor denylisted.",
    )
    mode.add_argument(
        "--remove",
        metavar="VERSION",
        help="Delete VERSION's pins (if any) and denylist it. Requires --reason.",
    )
    mode.add_argument(
        "--list-versions",
        action="store_true",
        help="Print the supported versions (pinned minus denylisted) as a JSON list.",
    )
    parser.add_argument("--reason", default=None, help="Why --remove is denylisting VERSION.")
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN"), help="GitHub API token.")
    args = parser.parse_args(argv)

    if args.reason is not None and args.remove is None:
        parser.error("--reason is only valid with --remove")
    if args.version is not None and not args.write:
        parser.error("--version is only valid with --write")

    config = parse_plugin_config(args.subsystems.read_text())

    if args.list_versions:
        print(json.dumps(supported_versions(config)))
        return 0
    if args.remove is not None:
        if args.reason is None:
            parser.error("--remove requires --reason")
        return run_remove(args.subsystems, config, args.remove, args.reason)
    if args.check:
        return run_check(config, args.token)
    if args.check_upstream:
        return run_check_upstream(config, args.token)
    return run_write(args.subsystems, config, args.version or config.version, args.token)


if __name__ == "__main__":
    raise SystemExit(main())
