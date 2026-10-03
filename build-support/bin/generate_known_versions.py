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
    python3 $GEN --default-version        # the default version, for a CI job

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
# How `--check-upstream` spells this script in the commands it suggests (run from the repo root).
COMMAND = "python3 build-support/bin/generate_known_versions.py"

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


class GeneratorError(Exception):
    """An expected failure: reported as one `error: ...` line on stderr, exit status 1."""


class ConfigError(GeneratorError):
    """`subsystems.py` is missing, malformed, or uses a shape the script does not support."""


class LayoutError(GeneratorError):
    """The source layout is one the line-level editor does not handle."""


class NetworkError(GeneratorError):
    """A GitHub request failed (after retries, for transient failures)."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class EditCheckError(GeneratorError):
    """An edit failed its pre-write check, so the file was not written."""


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
        raise GeneratorError(f"{what} `{text}` is not a stable X.Y.Z version")
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


DEFAULT_SOURCE = "subsystems.py"


def _find_class(tree: ast.Module, name: str, source: str) -> ast.ClassDef:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise ConfigError(f"{source}: class `{name}` not found")


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


def _locate(
    text: str, source: str = DEFAULT_SOURCE
) -> tuple[dict[str, ast.expr], dict[str, ast.expr]]:
    """The value nodes of the module-level and `class Pyrefly` assignments we read or edit."""
    try:
        tree = ast.parse(text)
    except SyntaxError as error:
        raise ConfigError(f"{source} line {error.lineno}: not valid Python ({error.msg})") from None
    module_assigns = _assignments(tree.body)
    for required in (MINIMUM_CONSTANT, DENYLIST_CONSTANT):
        if required not in module_assigns:
            raise ConfigError(f"{source}: module-level `{required}` assignment not found")
    class_assigns = _assignments(_find_class(tree, "Pyrefly", source).body)
    for required in (
        "default_version",
        "default_url_template",
        "default_url_platform_mapping",
        "default_known_versions",
    ):
        if required not in class_assigns:
            raise ConfigError(f"{source}: `Pyrefly.{required}` assignment not found")

    def values(assigns: dict[str, ast.Assign | ast.AnnAssign]) -> dict[str, ast.expr]:
        return {name: node.value for name, node in assigns.items() if node.value is not None}

    return values(module_assigns), values(class_assigns)


_INVALID = object()


def _literal(node: ast.expr, source: str, name: str, valid, shape: str):
    """`ast.literal_eval(node)`, or a ConfigError naming the file, line, and expected shape."""
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        value = _INVALID
    if value is _INVALID or not valid(node, value):
        raise ConfigError(f"{source} line {node.lineno}: {name} must be {shape}")
    return value


def _is_str(node: ast.expr, value) -> bool:
    return isinstance(value, str)


def _is_str_dict(node: ast.expr, value) -> bool:
    return (
        isinstance(node, ast.Dict)
        and isinstance(value, dict)
        and all(isinstance(k, str) and isinstance(v, str) for k, v in value.items())
    )


_PIN_RE = re.compile(r"^[^|]+\|[^|]+\|[^|]+\|[^|]+$")


def _is_pin_list(node: ast.expr, value) -> bool:
    return (
        isinstance(node, ast.List)
        and isinstance(value, list)
        and all(isinstance(item, str) and _PIN_RE.match(item) for item in value)
    )


def parse_plugin_config(text: str, source: str = DEFAULT_SOURCE) -> PluginConfig:
    module_values, class_values = _locate(text, source)
    return PluginConfig(
        minimum_version=_literal(
            module_values[MINIMUM_CONSTANT], source, MINIMUM_CONSTANT, _is_str, "a string literal"
        ),
        denylist=_literal(
            module_values[DENYLIST_CONSTANT],
            source,
            DENYLIST_CONSTANT,
            _is_str_dict,
            'a {...} dict literal of "version": "reason" strings',
        ),
        version=_literal(
            class_values["default_version"], source, "default_version", _is_str, "a string literal"
        ),
        url_template=_literal(
            class_values["default_url_template"],
            source,
            "default_url_template",
            _is_str,
            "a string literal",
        ),
        platform_mapping=_literal(
            class_values["default_url_platform_mapping"],
            source,
            "default_url_platform_mapping",
            _is_str_dict,
            "a {...} dict literal of strings",
        ),
        known_versions=_literal(
            class_values["default_known_versions"],
            source,
            "default_known_versions",
            _is_pin_list,
            'a [...] list literal of "<version>|<platform>|<sha256>|<size>" strings',
        ),
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
        raise GeneratorError("--reason must not be empty")
    for char in reason:
        if unicodedata.category(char) in _FORBIDDEN_CATEGORIES:
            raise GeneratorError(
                f"--reason: reason must be a single line, but it contains {char!r} (a newline, "
                "tab, or other control character)"
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
    if "".join(pieces) != reason:
        raise EditCheckError("internal error: the wrapped reason does not join back exactly")
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


def _check_alone_on_lines(lines: list[str], node: ast.expr, label: str, source: str) -> None:
    """Refuse if `node` shares a line with other code (only a comma/comment may follow it)."""
    first, last = node.lineno - 1, (node.end_lineno or node.lineno) - 1
    before = _byte_slice(lines[first], 0, node.col_offset)
    after = _byte_slice(lines[last], node.end_col_offset or 0)
    if before.strip() or not re.fullmatch(r"\s*,?\s*(#.*)?", after):
        raise LayoutError(
            f"{source} line {node.lineno}: {label} shares a line with other code; put one entry "
            "per line (the layout `pants fmt` produces) or edit it by hand"
        )


def _check_one_item_per_line(
    lines: list[str], container: ast.expr, items: list[ast.expr], what: str, source: str
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
            f"{source} line {container.lineno}: `{what}` must put its opening and closing "
            "brackets on their own lines, one entry per line (the layout `pants fmt` produces); "
            "edit it by hand or reformat it first"
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


def _known_versions_node(text: str, source: str) -> ast.List:
    _, class_values = _locate(text, source)
    node = class_values["default_known_versions"]
    if not isinstance(node, ast.List):
        raise LayoutError(f"{source} line {node.lineno}: default_known_versions must be a list")
    return node


def check_pins_layout(text: str, source: str = DEFAULT_SOURCE) -> list[tuple[str, ast.expr]]:
    """Refuse pin layouts the line editor can't edit safely; returns (pin, node) pairs."""
    node = _known_versions_node(text, source)
    lines = _split(text)
    if node.elts:
        _check_one_item_per_line(lines, node, list(node.elts), "default_known_versions", source)
    existing = [(ast.literal_eval(element), element) for element in node.elts]
    for value, element in existing:
        # Two pins on one line would make "insert a line before/after this pin" ambiguous.
        _check_alone_on_lines(lines, element, f"the pin {value!r}", source)
    return existing


def edit_default_version(text: str, version: str, source: str = DEFAULT_SOURCE) -> str:
    """Replace only the version literal on the `default_version` line (keeping quotes/comments)."""
    _, class_values = _locate(text, source)
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


def edit_insert_pins(
    text: str,
    new_pins: list[str],
    platform_mapping: dict[str, str],
    source: str = DEFAULT_SOURCE,
) -> str:
    """Insert each new pin line at its canonical position; every other line is left as-is."""
    if not new_pins:
        return text
    node = _known_versions_node(text, source)
    lines = _split(text)
    ordered = canonical_order(new_pins, platform_mapping)

    if not node.elts:
        indent = _leading_ws(lines[node.lineno - 1]) + "    "
        rendered = [f'{indent}"{pin}",' for pin in ordered]
        if node.lineno == node.end_lineno:
            return "\n".join(_expand_empty_inline(lines, node, "[", "]", rendered))
        close_index = (node.end_lineno or node.lineno) - 1
        return "\n".join(lines[:close_index] + rendered + lines[close_index:])

    existing = check_pins_layout(text, source)
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


def edit_remove_pins(text: str, version: str, source: str = DEFAULT_SOURCE) -> str:
    """Delete only the lines holding `version`'s pins."""
    node = _known_versions_node(text, source)
    lines = _split(text)
    doomed: set[int] = set()
    for element in node.elts:
        value = ast.literal_eval(element)
        if not isinstance(value, str) or pin_version(value) != version:
            continue
        _check_alone_on_lines(lines, element, f"the pin {value!r}", source)
        first, last = element.lineno - 1, (element.end_lineno or element.lineno) - 1
        doomed.update(range(first, last + 1))
    return "\n".join(line for index, line in enumerate(lines) if index not in doomed)


def edit_add_denylist_entry(
    text: str, version: str, reason: str, source: str = DEFAULT_SOURCE
) -> str:
    """Add one `DENYLISTED_VERSIONS` entry (newest first); existing lines are left as-is."""
    module_values, _ = _locate(text, source)
    node = module_values[DENYLIST_CONSTANT]
    if not isinstance(node, ast.Dict):
        raise LayoutError(
            f"{source} line {node.lineno}: {DENYLIST_CONSTANT} must be a {{...}} dict literal"
        )
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
    _check_one_item_per_line(lines, node, [*keys, *node.values], DENYLIST_CONSTANT, source)
    for key in keys:
        if _byte_slice(lines[key.lineno - 1], 0, key.col_offset).strip():
            raise LayoutError(
                f"{source} line {key.lineno}: the {DENYLIST_CONSTANT} entry "
                f"{ast.literal_eval(key)!r} shares a line with other code; put one entry per line "
                "(the layout `pants fmt` produces) or edit it by hand"
            )
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
        except (OSError, http.client.HTTPException) as error:
            # urllib.error.URLError/HTTPError, socket timeouts, and connection resets are OSErrors;
            # a body cut short is an http.client.HTTPException. Anything else is a bug: let it out.
            transient = _is_transient(error)
            if not transient or attempt == MAX_ATTEMPTS:
                status = error.code if isinstance(error, urllib.error.HTTPError) else None
                tries = f" after {attempt} attempts" if transient else ""
                raise NetworkError(f"GET {url} failed{tries}: {error}", status=status) from error
            delay = BACKOFF_SECONDS[attempt - 1]
            print(f"warning: {url}: {error}; retrying in {delay}s", file=sys.stderr)
            _sleep(delay)
    raise NetworkError(f"GET {url} failed")


def list_stable_releases(token: str | None) -> dict[str, dict]:
    """Every stable (non-draft, non-prerelease, `X.Y.Z`-tagged) release, keyed by tag.

    Walks the paginated releases API until an empty page comes back.
    """
    releases: dict[str, dict] = {}
    page = 1
    while True:
        url = RELEASES_API.format(page=page)
        try:
            batch = json.loads(_get(url, token))
        except ValueError:
            raise NetworkError(f"GET {url} returned a response that is not JSON") from None
        if not isinstance(batch, list):
            raise NetworkError(f"GET {url} returned an unexpected response (not a release list)")
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
        raise GeneratorError(
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
            raise GeneratorError(f"release {version} has no asset named `{filename}`")
        asset_url = downloads[filename]
        try:
            sidecar = _get(f"{asset_url}.sha256", token)
        except NetworkError as error:
            if error.status != 404:
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


def _error(message: str) -> None:
    """Print one `error: ...` line to stderr (any embedded newlines are folded)."""
    print("error: " + " ".join(str(message).splitlines()), file=sys.stderr)


def run_check(config: PluginConfig, token: str | None) -> int:
    releases = list_stable_releases(token)
    problems, _ = verify_pins(config, releases, token)
    if problems:
        _error(f"Pyrefly pins are invalid ({len(problems)} problem(s)):")
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
    _error("stable Pyrefly releases are neither pinned nor denylisted:")
    if newer:
        print(f"  newer than the default {config.version}: {', '.join(newer)}", file=sys.stderr)
    if in_range:
        print(f"  at or below the default (backports): {', '.join(in_range)}", file=sys.stderr)
    # `--write` pins every missing release in [minimum, target], so moving the default to the
    # newest release also pins the backports, and plain `--write` pins just the backports.
    hints: list[tuple[str, str]] = []
    if newer:
        newest = max(newer, key=_version_key)
        also = " (backports included)" if in_range else ""
        hints.append(
            (
                f"To make {newest} the default and pin every missing release{also}:",
                f"--version {newest} --write",
            )
        )
    if in_range:
        what = "only the backports, keeping" if newer else "the backports, keeping"
        hints.append((f"To pin {what} the default {config.version}:", "--write"))
    hints.append(("To skip a release instead (denylists it):", '--remove X.Y.Z --reason "..."'))
    for text, args in hints:
        print(f"  {text}", file=sys.stderr)
        print(f"    {COMMAND} {args}", file=sys.stderr)
    return 1


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise ConfigError(f"cannot read {path}: {error.strerror or error}") from None
    except UnicodeDecodeError as error:
        raise ConfigError(f"{path} is not valid UTF-8 ({error.reason})") from None


def _require(condition: bool, path: Path, problem: str) -> None:
    """An explicit pre-write check (never an `assert`, which `python -O` would strip)."""
    if not condition:
        raise EditCheckError(
            f"refusing to write {path}: {problem}; the file was not changed (this is a bug in the "
            "generator, please report it)"
        )


def _write(path: Path, text: str) -> None:
    try:
        path.write_text(text, encoding="utf-8")
    except OSError as error:
        raise GeneratorError(f"cannot write {path}: {error.strerror or error}") from None


def run_write(path: Path, config: PluginConfig, version: str, token: str | None) -> int:
    source = str(path)
    target = _require_stable(version, "--version")
    if target < _version_key(config.minimum_version):
        raise GeneratorError(
            f"{version} is older than MINIMUM_PINNED_VERSION {config.minimum_version}"
        )
    if version in config.denylist:
        raise GeneratorError(f"{version} is denylisted: {config.denylist[version]}")
    # Refuse layouts the line editor can't handle before doing anything else, even when there
    # turns out to be nothing to add.
    check_pins_layout(_read(path), source)
    if config.known_versions != canonical_order(config.known_versions, config.platform_mapping):
        raise GeneratorError(
            f"{source}: default_known_versions is not in canonical order (newest version first, "
            "platforms in default_url_platform_mapping order); fix the order by hand, then rerun"
        )
    releases = list_stable_releases(token)
    if version not in releases:
        raise GeneratorError(f"{version} is not a published stable Pyrefly release")

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
    text = _read(path)
    if version != config.version:
        text = edit_default_version(text, version, source)
    text = edit_insert_pins(text, new_pins, config.platform_mapping, source)

    # Pre-write check: the default moved, exactly the new pins were added, existing pins kept
    # their order, and the result is in canonical order.
    updated = parse_plugin_config(text, source)
    _require(updated.version == version, path, f"default_version is not {version}")
    _require(
        sorted(updated.known_versions) == sorted([*config.known_versions, *new_pins]),
        path,
        "the pins are not exactly the existing pins plus the new ones",
    )
    _require(
        [pin for pin in updated.known_versions if pin not in new_pins] == config.known_versions,
        path,
        "existing pins were reordered",
    )
    _require(
        updated.known_versions == canonical_order(updated.known_versions, config.platform_mapping),
        path,
        "the pins would not be in canonical order",
    )
    _write(path, text)

    added = ", ".join(to_add) if to_add else "none"
    print(f"Set default_version = {version}; added pins for: {added}.")
    if mismatches:
        _error("existing pins disagree with the release and were NOT changed:")
        for problem in mismatches:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    return 0


def run_remove(path: Path, config: PluginConfig, version: str, reason: str) -> int:
    source = str(path)
    _require_stable(version, "--remove")
    validate_reason(reason)
    if version == config.version:
        raise GeneratorError(
            f"refusing to remove the default version {version}; move the default first "
            "(`--version X --write`)"
        )
    if version == config.minimum_version:
        raise GeneratorError(
            f"refusing to remove MINIMUM_PINNED_VERSION {version}; raise "
            "MINIMUM_PINNED_VERSION in subsystems.py deliberately instead (with a CHANGELOG note)"
        )
    if version in config.denylist:
        raise GeneratorError(f"{version} is already denylisted: {config.denylist[version]}")

    # Edit lines in place: delete only this version's pin lines and add only the new entry.
    text = _read(path)
    text = edit_remove_pins(text, version, source)
    text = edit_add_denylist_entry(text, version, reason, source)
    remaining = [pin for pin in config.known_versions if pin_version(pin) != version]

    updated = parse_plugin_config(text, source)
    _require(updated.known_versions == remaining, path, f"pins other than {version}'s changed")
    _require(
        updated.denylist == {**config.denylist, version: reason},
        path,
        "the denylist is not exactly the old one plus the new entry",
    )
    _require(updated.version == config.version, path, "default_version changed")
    _write(path, text)
    removed = len(config.known_versions) - len(remaining)
    print(f"Removed {removed} pin(s) for {version} and denylisted it: {reason}")
    return 0


class _ArgumentParser(argparse.ArgumentParser):
    """Report bad command-line input as a single `error:` line (exit 1), not usage + exit 2."""

    def error(self, message: str):  # type: ignore[override]
        raise GeneratorError(f"{message} (see --help)")


def _run(argv: list[str] | None) -> int:
    parser = _ArgumentParser(
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
    mode.add_argument(
        "--default-version",
        action="store_true",
        help="Print the plugin's default Pyrefly version.",
    )
    parser.add_argument("--reason", default=None, help="Why --remove is denylisting VERSION.")
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN"), help="GitHub API token.")
    args = parser.parse_args(argv)

    if args.reason is not None and args.remove is None:
        parser.error("--reason is only valid with --remove")
    if args.version is not None and not args.write:
        parser.error("--version is only valid with --write")
    if args.remove is not None and args.reason is None:
        parser.error("--remove requires --reason")

    config = parse_plugin_config(_read(args.subsystems), str(args.subsystems))

    if args.list_versions:
        print(json.dumps(supported_versions(config)))
        return 0
    if args.default_version:
        print(config.version)
        return 0
    if args.remove is not None:
        return run_remove(args.subsystems, config, args.remove, args.reason)
    if args.check:
        return run_check(config, args.token)
    if args.check_upstream:
        return run_check_upstream(config, args.token)
    return run_write(args.subsystems, config, args.version or config.version, args.token)


def main(argv: list[str] | None = None) -> int:
    """Run the CLI. Expected failures print one `error: ...` line and return 1, never a traceback.

    Anything else is a bug: it is reported the same way (set GENERATE_KNOWN_VERSIONS_DEBUG=1 to
    get the traceback instead).
    """
    try:
        return _run(argv)
    except GeneratorError as error:
        _error(str(error))
        return 1
    except KeyboardInterrupt:
        _error("interrupted")
        return 130
    except Exception as error:
        if os.environ.get("GENERATE_KNOWN_VERSIONS_DEBUG"):
            raise
        _error(
            f"unexpected {type(error).__name__}: {error} (this is a bug in the generator; set "
            "GENERATE_KNOWN_VERSIONS_DEBUG=1 for a traceback)"
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
