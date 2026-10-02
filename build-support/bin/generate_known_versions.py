#!/usr/bin/env python3
# Copyright 2026 Tague Griffith
# Licensed under the Apache License, Version 2.0 (see LICENSE).

"""Maintain the Pyrefly `default_known_versions` pins in `subsystems.py`.

Each pin is `"<version>|<pants_platform>|<sha256>|<size_bytes>"`, ordered newest version first and
platforms in `default_url_platform_mapping` order. Every version listed is pinned and tested; none
is older than `MINIMUM_PINNED_VERSION`, and none is in `DENYLISTED_VERSIONS` (version -> reason).
Only stable releases are pinned, never pre-releases (`X.Y.Z-dev.N` etc.) or drafts.

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

`--write` and `--remove` edit `subsystems.py` line by line: `--write` changes only the
`default_version` literal and inserts new pin lines, and `--remove` deletes only that version's pin
lines and adds only its denylist entry. Every other line (comments, quoting, formatting) is left
byte-identical. `--write` never removes or rewrites an existing pin, and it reports (exit 1) any
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
import http.client
import io
import json
import os
import re
import socket
import sys
import time
import tokenize
import unicodedata
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
# Reading subsystems.py
# ---


@dataclass
class PluginConfig:
    """The Pyrefly download config parsed out of `subsystems.py`."""

    minimum_version: str
    denylist: dict[str, str]
    version: str
    url_template: str
    platform_mapping: dict[str, str]
    known_versions: list[str]


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


def _locate(text: str) -> tuple[dict[str, ast.expr], dict[str, ast.expr]]:
    """The value nodes of the module-level and `class Pyrefly` assignments we read or edit."""
    tree = ast.parse(text)
    module_assigns = _assignments(tree.body)
    for required in (MINIMUM_CONSTANT, DENYLIST_CONSTANT):
        if required not in module_assigns:
            raise ValueError(f"module-level `{required}` not found in subsystems.py")
    class_assigns = _assignments(_find_class(tree, "Pyrefly").body)
    for required in (
        "default_version",
        "default_url_template",
        "default_url_platform_mapping",
        "default_known_versions",
    ):
        if required not in class_assigns:
            raise ValueError(f"`Pyrefly.{required}` not found in subsystems.py")

    def values(assigns: dict[str, ast.Assign | ast.AnnAssign]) -> dict[str, ast.expr]:
        return {name: node.value for name, node in assigns.items() if node.value is not None}

    return values(module_assigns), values(class_assigns)


def parse_plugin_config(text: str) -> PluginConfig:
    module_values, class_values = _locate(text)
    denylist = ast.literal_eval(module_values[DENYLIST_CONSTANT])
    if not isinstance(denylist, dict):
        raise ValueError(f"`{DENYLIST_CONSTANT}` must be a dict of version -> reason")
    return PluginConfig(
        minimum_version=ast.literal_eval(module_values[MINIMUM_CONSTANT]),
        denylist=denylist,
        version=ast.literal_eval(class_values["default_version"]),
        url_template=ast.literal_eval(class_values["default_url_template"]),
        platform_mapping=ast.literal_eval(class_values["default_url_platform_mapping"]),
        known_versions=ast.literal_eval(class_values["default_known_versions"]),
    )


# ---
# Rendering Python string literals the way `ruff format` would leave them
# ---

MAX_LINE_WIDTH = 100
# Characters a reason may not contain: they would break the single-line literal (or the file).
_FORBIDDEN_CATEGORIES = {"Cc", "Zl", "Zp", "Cs"}


def _width(text: str) -> int:
    """Display width as ruff measures it for the line-length limit (wide/fullwidth = 2)."""
    return sum(2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in text)


def py_literal(value: str) -> str:
    """A string literal for `value` in ruff format's preferred quote style.

    Double quotes, unless the value contains more double quotes than single quotes. Characters are
    written as-is (UTF-8), never as `\\u`/surrogate escapes.
    """
    quote = "'" if value.count('"') > value.count("'") else '"'
    body = value.replace("\\", "\\\\").replace(quote, "\\" + quote)
    return f"{quote}{body}{quote}"


def validate_reason(reason: str) -> None:
    if not reason.strip():
        raise ValueError("reason must not be empty")
    for char in reason:
        if unicodedata.category(char) in _FORBIDDEN_CATEGORIES:
            raise ValueError(
                f"reason must be a single line: it contains {char!r}, a newline, tab, or other "
                "control character"
            )


def _split_reason(reason: str, indent: str) -> list[str]:
    """Split `reason` into pieces whose literals fit on their own lines; they join back exactly."""
    budget = MAX_LINE_WIDTH - len(indent)
    pieces: list[str] = []
    current = ""
    for word in re.findall(r"\S+\s*|\s+", reason):
        while word:
            candidate = current + word
            if _width(py_literal(candidate)) <= budget:
                current, word = candidate, ""
            elif current:
                pieces.append(current)
                current = ""
            else:
                # A single word too long for one line: hard-split it.
                cut = len(word)
                while cut > 1 and _width(py_literal(word[:cut])) > budget:
                    cut -= 1
                pieces.append(word[:cut])
                word = word[cut:]
    if current:
        pieces.append(current)
    assert "".join(pieces) == reason
    return pieces


def render_denylist_entry(version: str, reason: str, indent: str) -> list[str]:
    key = py_literal(version)
    one_line = f"{indent}{key}: {py_literal(reason)},"
    if _width(one_line) <= MAX_LINE_WIDTH:
        return [one_line]
    inner = indent + "    "
    return [
        f"{indent}{key}: (",
        *(f"{inner}{py_literal(piece)}" for piece in _split_reason(reason, inner)),
        f"{indent}),",
    ]


def render_denylist_block(denylist: dict[str, str]) -> list[str]:
    """A fresh `DENYLISTED_VERSIONS` assignment (used for test fixtures and docs)."""
    head = f"{DENYLIST_CONSTANT}: dict[str, str] = "
    if not denylist:
        return [head + "{}"]
    lines = [head + "{"]
    for version in sorted(denylist, key=_version_key, reverse=True):
        lines += render_denylist_entry(version, denylist[version], "    ")
    lines.append("}")
    return lines


def render_known_versions_block(pins: list[str]) -> list[str]:
    """A fresh `default_known_versions` assignment (used for test fixtures)."""
    return ["    default_known_versions = [", *(f'        "{pin}",' for pin in pins), "    ]"]


# ---
# Editing subsystems.py in place: only the lines that must change are touched
# ---


class LayoutError(ValueError):
    """The source layout is one the line-level editor does not handle."""


def _split(text: str) -> list[str]:
    # Split on "\n" only (not str.splitlines, which also splits on \x0c,  , ...), so joining
    # with "\n" reproduces the file byte for byte.
    return text.split("\n")


def _leading_ws(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _byte_slice(line: str, start: int, end: int | None = None) -> str:
    # ast column offsets are UTF-8 byte offsets.
    return line.encode()[start:end].decode()


def _char_col(line: str, byte_col: int) -> int:
    return len(_byte_slice(line, 0, byte_col))


def _anchor_above_comments(lines: list[str], index: int, floor: int) -> int:
    """Move an insertion point above the comment lines directly preceding line `index`."""
    while index - 1 > floor and lines[index - 1].lstrip().startswith("#"):
        index -= 1
    return index


def _ensure_trailing_comma(text: str, container: ast.expr) -> str:
    """Make sure the last item of a multi-line list/dict is followed by a comma."""
    lines = _split(text)
    close_row = container.end_lineno or container.lineno
    close_col = _char_col(lines[close_row - 1], (container.end_col_offset or 1) - 1)
    previous = None
    skip = {tokenize.NL, tokenize.NEWLINE, tokenize.COMMENT, tokenize.INDENT, tokenize.DEDENT}
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.start == (close_row, close_col):
            break
        if token.type not in skip:
            previous = token
    if previous is None or previous.string in (",", "[", "{"):
        return text
    row, col = previous.end
    line = lines[row - 1]
    lines[row - 1] = line[:col] + "," + line[col:]
    return "\n".join(lines)


def _check_one_item_per_line(
    lines: list[str], container: ast.expr, items: list[ast.expr], what: str
) -> None:
    open_index = container.lineno - 1
    close_index = (container.end_lineno or container.lineno) - 1
    closer = "]" if isinstance(container, ast.List) else "}"
    ok = lines[close_index].lstrip().startswith(closer) and all(
        item.lineno - 1 > open_index and (item.end_lineno or item.lineno) - 1 < close_index
        for item in items
    )
    if not ok:
        raise LayoutError(
            f"`{what}` must put its opening and closing brackets on their own lines, one entry "
            "per line (the layout `pants fmt` produces); edit it by hand or reformat it first"
        )


def _expand_empty_inline(
    lines: list[str], container: ast.expr, opener: str, closer: str, new_items: list[str]
) -> list[str]:
    """Turn a one-line empty `[]`/`{}` into a block holding `new_items` (already indented)."""
    index = container.lineno - 1
    line = lines[index]
    prefix = _byte_slice(line, 0, container.col_offset)
    suffix = _byte_slice(line, container.end_col_offset or 0)
    return (
        lines[:index]
        + [prefix + opener, *new_items, _leading_ws(line) + closer + suffix]
        + lines[index + 1 :]
    )


def edit_default_version(text: str, version: str) -> str:
    """Replace only the version literal on the `default_version` line (keeping quotes/comments)."""
    _, class_values = _locate(text)
    node = class_values["default_version"]
    lines = _split(text)
    index = node.lineno - 1
    line = lines[index]
    old = _byte_slice(line, node.col_offset, node.end_col_offset)
    quote = old[0] if old[:1] in ("'", '"') else '"'
    lines[index] = (
        _byte_slice(line, 0, node.col_offset)
        + f"{quote}{version}{quote}"
        + _byte_slice(line, node.end_col_offset)
    )
    return "\n".join(lines)


def edit_insert_pins(text: str, new_pins: list[str], platform_mapping: dict[str, str]) -> str:
    """Insert each new pin line at its canonical position; every other line is left as-is."""
    if not new_pins:
        return text
    _, class_values = _locate(text)
    node = class_values["default_known_versions"]
    assert isinstance(node, ast.List)
    lines = _split(text)
    ordered = canonical_order(new_pins, platform_mapping)

    if not node.elts:
        indent = _leading_ws(lines[node.lineno - 1]) + "    "
        rendered = [f'{indent}"{pin}",' for pin in ordered]
        if node.lineno == node.end_lineno:
            return "\n".join(_expand_empty_inline(lines, node, "[", "]", rendered))
        close_index = (node.end_lineno or node.lineno) - 1
        return "\n".join(lines[:close_index] + rendered + lines[close_index:])

    _check_one_item_per_line(lines, node, list(node.elts), "default_known_versions")
    existing = [(ast.literal_eval(element), element) for element in node.elts]
    indent = _leading_ws(lines[node.elts[0].lineno - 1])
    open_index = node.lineno - 1
    close_index = (node.end_lineno or node.lineno) - 1

    groups: dict[int, list[str]] = {}
    for pin in ordered:
        key = _pin_sort_key(pin, platform_mapping)
        following = next(
            (el for value, el in existing if _pin_sort_key(value, platform_mapping) > key), None
        )
        index = (
            _anchor_above_comments(lines, following.lineno - 1, open_index)
            if following is not None
            else close_index
        )
        groups.setdefault(index, []).append(f'{indent}"{pin}",')

    if close_index in groups:
        text = _ensure_trailing_comma(text, node)
        lines = _split(text)
    for index in sorted(groups, reverse=True):
        lines[index:index] = groups[index]
    return "\n".join(lines)


def edit_remove_pins(text: str, version: str) -> str:
    """Delete only the lines holding `version`'s pins."""
    _, class_values = _locate(text)
    node = class_values["default_known_versions"]
    assert isinstance(node, ast.List)
    lines = _split(text)
    doomed: set[int] = set()
    for element in node.elts:
        value = ast.literal_eval(element)
        if not isinstance(value, str) or pin_version(value) != version:
            continue
        first, last = element.lineno - 1, (element.end_lineno or element.lineno) - 1
        before = _byte_slice(lines[first], 0, element.col_offset)
        after = _byte_slice(lines[last], element.end_col_offset or 0)
        if before.strip() or not re.fullmatch(r"\s*,?\s*(#.*)?", after):
            raise LayoutError(f"the pin {value!r} shares a line with other code; remove it by hand")
        doomed.update(range(first, last + 1))
    return "\n".join(line for index, line in enumerate(lines) if index not in doomed)


def edit_add_denylist_entry(text: str, version: str, reason: str) -> str:
    """Add one `DENYLISTED_VERSIONS` entry (newest first); existing lines are left as-is."""
    module_values, _ = _locate(text)
    node = module_values[DENYLIST_CONSTANT]
    if not isinstance(node, ast.Dict):
        raise LayoutError(f"`{DENYLIST_CONSTANT}` must be a dict literal")
    lines = _split(text)
    open_index = node.lineno - 1
    close_index = (node.end_lineno or node.lineno) - 1

    if not node.keys:
        indent = _leading_ws(lines[open_index]) + "    "
        rendered = render_denylist_entry(version, reason, indent)
        if node.lineno == node.end_lineno:
            return "\n".join(_expand_empty_inline(lines, node, "{", "}", rendered))
        return "\n".join(lines[:close_index] + rendered + lines[close_index:])

    keys = [key for key in node.keys if key is not None]
    _check_one_item_per_line(lines, node, [*keys, *node.values], DENYLIST_CONSTANT)
    indent = _leading_ws(lines[keys[0].lineno - 1])
    rendered = render_denylist_entry(version, reason, indent)
    new_key = _version_key(version)
    following = next((key for key in keys if _version_key(ast.literal_eval(key)) < new_key), None)
    if following is not None:
        index = _anchor_above_comments(lines, following.lineno - 1, open_index)
    else:
        text = _ensure_trailing_comma(text, node)
        lines = _split(text)
        index = close_index
    lines[index:index] = rendered
    return "\n".join(lines)


# ---
# Pins: parsing and ordering
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


# ---
# Network
# ---


def _is_transient(error: Exception) -> bool:
    if isinstance(error, urllib.error.HTTPError):
        return error.code == 429 or error.code >= 500
    # URLError wraps connection failures (refused, DNS); timeouts surface as socket.timeout /
    # TimeoutError, directly or wrapped in URLError. A connection reset mid-body is a
    # ConnectionError (ConnectionResetError), and a body cut short is http.client.IncompleteRead.
    return isinstance(
        error,
        (
            urllib.error.URLError,
            TimeoutError,
            socket.timeout,
            ConnectionError,
            http.client.IncompleteRead,
        ),
    )


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


def _write_checked(path: Path, text: str, check) -> None:
    """Write `text` only if it still parses and `check(config)` holds for the result."""
    check(parse_plugin_config(text))
    path.write_text(text, encoding="utf-8")


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

    # Edit lines in place: only the `default_version` literal changes, and new pin lines are
    # inserted. Every other line stays byte-identical.
    text = path.read_text(encoding="utf-8")
    if version != config.version:
        text = edit_default_version(text, version)
    text = edit_insert_pins(text, new_pins, config.platform_mapping)

    def check(updated: PluginConfig) -> None:
        # The default moved, exactly the new pins were added, and existing pins kept their order.
        assert updated.version == version
        assert sorted(updated.known_versions) == sorted([*config.known_versions, *new_pins])
        kept = [pin for pin in updated.known_versions if pin not in new_pins]
        assert kept == config.known_versions

    _write_checked(path, text, check)

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
    try:
        validate_reason(reason)
    except ValueError as error:
        raise SystemExit(f"error: --reason: {error}") from None
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
    if version in config.denylist:
        raise SystemExit(f"error: {version} is already denylisted: {config.denylist[version]}")

    # Edit lines in place: delete only this version's pin lines and add only the new entry.
    text = path.read_text(encoding="utf-8")
    text = edit_remove_pins(text, version)
    text = edit_add_denylist_entry(text, version, reason)
    remaining = [pin for pin in config.known_versions if pin_version(pin) != version]

    def check(updated: PluginConfig) -> None:
        assert updated.known_versions == remaining
        assert updated.denylist == {**config.denylist, version: reason}

    _write_checked(path, text, check)
    removed = len(config.known_versions) - len(remaining)
    print(f"Removed {removed} pin(s) for {version} and denylisted it: {reason}")
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

    config = parse_plugin_config(args.subsystems.read_text(encoding="utf-8"))

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
