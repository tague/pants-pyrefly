# Copyright 2026 Tague Griffith
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

import http.client
import io
import json
import socket
import urllib.error
from pathlib import Path

import pytest  # pants: no-infer-dep

import generate_known_versions as gkv

_SUBSYSTEMS = """\
MINIMUM_PINNED_VERSION = "1.1.1"

DENYLISTED_VERSIONS: dict[str, str] = {}


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
_NAMES = {
    "macos_arm64": "pyrefly-macos-arm64.tar.gz",
    "linux_x86_64": "pyrefly-linux-x86_64-musl.tar.gz",
}


def _sha(tag: str, platform: str) -> str:
    # A deterministic, valid 64-hex digest per (tag, platform).
    return (f"{tag}{platform}".encode().hex() * 8)[:64]


def _size(tag: str, platform: str) -> int:
    return 1000 + len(tag) * 10 + len(platform)


def _release(tag: str, prerelease: bool, draft: bool) -> dict:
    return {
        "tag_name": tag,
        "prerelease": prerelease,
        "draft": draft,
        "assets": [
            {
                "name": name,
                "size": _size(tag, plat),
                "browser_download_url": f"https://dl/{tag}/{name}",
            }
            for plat, name in _NAMES.items()
        ],
    }


def _pins(tags: list[str]) -> list[str]:
    return [f"{t}|{p}|{_sha(t, p)}|{_size(t, p)}" for t in tags for p in _NAMES]


@pytest.fixture
def fake_github(monkeypatch) -> list[str]:
    """Serve `_PAGES` and sha256 sidecars via `_get`; returns the list of URLs requested."""
    requested: list[str] = []
    pages = [[_release(*entry) for entry in page] for page in _PAGES]
    platform_by_name = {name: plat for plat, name in _NAMES.items()}

    def fake_get(url: str, token: str | None) -> bytes:
        requested.append(url)
        for number in range(1, len(pages) + 2):
            if url == gkv.RELEASES_API.format(page=number):
                return json.dumps(pages[number - 1] if number <= len(pages) else []).encode()
        if url.startswith("https://dl/") and url.endswith(".sha256"):
            tag, name = url[len("https://dl/") : -len(".sha256")].split("/")
            return f"{_sha(tag, platform_by_name[name])}  {name}\n".encode()
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(gkv, "_get", fake_get)
    return requested


def _write_subsystems(
    tmp_path: Path,
    known: list[str],
    *,
    default: str = "1.9.0",
    denylist: dict[str, str] | None = None,
) -> Path:
    text = _SUBSYSTEMS.replace('default_version = "1.9.0"', f'default_version = "{default}"')
    text = text.replace(
        "DENYLISTED_VERSIONS: dict[str, str] = {}",
        "\n".join(gkv.render_denylist_block(denylist or {})),
    )
    start = text.index("    default_known_versions = [")
    end = text.index("    ]\n", start) + len("    ]")
    text = text[:start] + "\n".join(gkv.render_known_versions_block(known)) + text[end:]
    path = tmp_path / "subsystems.py"
    path.write_text(text)
    return path


def _config(path: Path) -> gkv.PluginConfig:
    return gkv.parse_plugin_config(path.read_text())


_ALL_IN_RANGE = ["1.9.0", "1.2.1", "1.2.0", "1.1.1"]


# --- parsing ---


def test_parse_plugin_config() -> None:
    config = gkv.parse_plugin_config(_SUBSYSTEMS)
    assert config.minimum_version == "1.1.1"
    assert config.denylist == {}
    assert config.version == "1.9.0"
    assert config.platform_mapping == {
        "macos_arm64": "macos-arm64",
        "linux_x86_64": "linux-x86_64-musl",
    }
    assert config.known_versions == ["1.9.0|macos_arm64|aaaa|111"]


@pytest.mark.parametrize("constant", ["MINIMUM_PINNED_VERSION", "DENYLISTED_VERSIONS"])
def test_parse_requires_module_constants(constant: str) -> None:
    text = "\n".join(line for line in _SUBSYSTEMS.splitlines() if not line.startswith(constant))
    with pytest.raises(ValueError, match=constant):
        gkv.parse_plugin_config(text)


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


def test_denylist_block_roundtrips_and_wraps_long_reasons(tmp_path) -> None:
    long_reason = "crashes on startup when the cache directory is on a network filesystem " * 3
    denylist = {"1.2.0": "bad hashes", "1.10.0": long_reason.strip()}
    block = gkv.render_denylist_block(denylist)
    assert all(len(line) <= 100 for line in block)
    # Newest first, and the rendered block parses back to the same mapping.
    assert block[1].startswith('    "1.10.0"')
    path = _write_subsystems(tmp_path, _pins(["1.9.0"]), denylist=denylist)
    assert _config(path).denylist == denylist


# --- releases ---


def test_list_stable_releases_paginates_and_excludes_prereleases(fake_github) -> None:
    releases = gkv.list_stable_releases(token=None)
    assert sorted(releases) == ["0.64.1", "1.1.0", "1.1.1", "1.11.0", "1.2.0", "1.2.1", "1.9.0"]
    # Walked both pages, then stopped at the first empty one.
    assert fake_github == [gkv.RELEASES_API.format(page=n) for n in (1, 2, 3)]


def test_canonical_order_is_numeric_newest_first() -> None:
    mapping = {"macos_arm64": "", "linux_x86_64": ""}
    pins = ["1.9.0|linux_x86_64|a|1", "1.10.0|macos_arm64|b|1", "1.9.0|macos_arm64|c|1"]
    assert gkv.canonical_order(pins, mapping) == [
        "1.10.0|macos_arm64|b|1",
        "1.9.0|macos_arm64|c|1",
        "1.9.0|linux_x86_64|a|1",
    ]


# --- --write (additive) ---


def test_write_adds_missing_range_in_canonical_order(fake_github, tmp_path) -> None:
    path = _write_subsystems(tmp_path, _pins(["1.9.0"]))
    assert gkv.main(["--subsystems", str(path), "--write"]) == 0
    # Everything stable in [1.1.1, 1.9.0]: not 1.11.0 (above), 1.1.0/0.64.1 (below), pre-releases,
    # drafts, or the odd `1.3.0rc1` tag.
    assert _config(path).known_versions == _pins(_ALL_IN_RANGE)
    assert 'skip = SkipOption("check")' in path.read_text()


def test_write_with_version_moves_default_and_keeps_existing_lines(fake_github, tmp_path) -> None:
    # An existing pin is kept verbatim (here, a bogus one): --write never rewrites it, reports it,
    # and exits non-zero so the disagreement is not missed.
    bogus = "1.2.0|macos_arm64|" + "0" * 64 + "|5"
    known = _pins(["1.2.1"]) + [bogus] + _pins(["1.2.0"])[1:]
    path = _write_subsystems(tmp_path, known, default="1.2.1")
    assert gkv.main(["--subsystems", str(path), "--version", "1.11.0", "--write"]) == 1
    config = _config(path)
    assert config.version == "1.11.0"
    assert bogus in config.known_versions
    assert config.known_versions == _pins(["1.11.0", "1.9.0", "1.2.1"]) + [bogus] + _pins(
        ["1.2.0"]
    )[1:] + _pins(["1.1.1"])


def test_write_lowering_the_default_keeps_higher_pins(fake_github, tmp_path) -> None:
    path = _write_subsystems(tmp_path, _pins(["1.11.0", "1.9.0"]), default="1.11.0")
    assert gkv.main(["--subsystems", str(path), "--version", "1.2.0", "--write"]) == 0
    config = _config(path)
    assert config.version == "1.2.0"
    assert config.known_versions == _pins(["1.11.0", "1.9.0", "1.2.0", "1.1.1"])


def test_write_never_readds_denylisted(fake_github, tmp_path) -> None:
    path = _write_subsystems(tmp_path, _pins(["1.9.0"]), denylist={"1.2.1": "broken"})
    assert gkv.main(["--subsystems", str(path), "--write"]) == 0
    assert _config(path).known_versions == _pins(["1.9.0", "1.2.0", "1.1.1"])


def test_write_refuses_denylisted_default(fake_github, tmp_path) -> None:
    path = _write_subsystems(tmp_path, _pins(["1.9.0"]), denylist={"1.11.0": "broken"})
    with pytest.raises(SystemExit, match="denylisted"):
        gkv.main(["--subsystems", str(path), "--version", "1.11.0", "--write"])


# --- --remove ---


def test_remove_deletes_pins_and_denylists(tmp_path) -> None:
    path = _write_subsystems(tmp_path, _pins(_ALL_IN_RANGE))
    assert gkv.main(["--subsystems", str(path), "--remove", "1.2.1", "--reason", "bad build"]) == 0
    config = _config(path)
    assert config.known_versions == _pins(["1.9.0", "1.2.0", "1.1.1"])
    assert config.denylist == {"1.2.1": "bad build"}
    assert config.version == "1.9.0"


def test_remove_can_denylist_a_never_pinned_version(tmp_path) -> None:
    path = _write_subsystems(tmp_path, _pins(["1.9.0"]))
    assert (
        gkv.main(["--subsystems", str(path), "--remove", "1.11.0", "--reason", "regression"]) == 0
    )
    assert _config(path).denylist == {"1.11.0": "regression"}
    assert _config(path).known_versions == _pins(["1.9.0"])


@pytest.mark.parametrize(
    ("version", "message"),
    [("1.9.0", "default version"), ("1.1.1", "raise MINIMUM_PINNED_VERSION")],
)
def test_remove_refuses_default_and_minimum(tmp_path, version, message) -> None:
    path = _write_subsystems(tmp_path, _pins(_ALL_IN_RANGE))
    with pytest.raises(SystemExit, match=message):
        gkv.main(["--subsystems", str(path), "--remove", version, "--reason", "x"])
    assert _config(path).known_versions == _pins(_ALL_IN_RANGE)


def test_remove_requires_reason(tmp_path) -> None:
    path = _write_subsystems(tmp_path, _pins(_ALL_IN_RANGE))
    with pytest.raises(SystemExit):
        gkv.main(["--subsystems", str(path), "--remove", "1.2.1"])


# --- --check (only what ships) ---


@pytest.mark.parametrize(
    "known",
    [
        _pins(_ALL_IN_RANGE),
        # An unpinned in-range backport (1.2.1) and a newer release (1.11.0) never fail --check.
        _pins(["1.9.0", "1.2.0", "1.1.1"]),
        _pins(["1.9.0"]),
        # Pins above the default are fine.
        _pins(["1.11.0", "1.9.0"]),
    ],
)
def test_check_passes_on_valid_pins(fake_github, tmp_path, known) -> None:
    path = _write_subsystems(tmp_path, known)
    assert gkv.main(["--subsystems", str(path), "--check"]) == 0


def _tamper(pins: list[str], version: str) -> list[str]:
    out = []
    for pin in pins:
        if pin.startswith(f"{version}|macos_arm64|"):
            ver, plat, _sha, size = pin.split("|")
            pin = f"{ver}|{plat}|{'f' * 64}|{size}"
        out.append(pin)
    return out


@pytest.mark.parametrize(
    ("known", "kwargs", "reason"),
    [
        (_tamper(_pins(_ALL_IN_RANGE), "1.2.0"), {}, "pin does not match the release"),
        (_pins(["1.2.1", "1.1.1"]), {}, "default version 1.9.0 is not pinned"),
        (_pins(["1.1.1", "1.2.0", "1.9.0"]), {}, "canonical order"),
        (_pins(["1.9.0", "1.1.0"]), {}, "older than MINIMUM_PINNED_VERSION"),
        (_pins(["1.9.0"])[:1], {}, "exactly one pin per platform"),
        (
            _pins(["1.9.0", "1.2.1"]),
            {"denylist": {"1.2.1": "bad"}},
            "1.2.1 is pinned but denylisted",
        ),
        (_pins(["1.9.0"]), {"denylist": {"1.9.0": "bad"}}, "default version 1.9.0 is denylisted"),
        (_pins(["1.9.0", "1.8.0"]), {}, "1.8.0 is pinned but is not a published stable"),
    ],
)
def test_check_fails_on_bad_pins(fake_github, tmp_path, capsys, known, kwargs, reason) -> None:
    path = _write_subsystems(tmp_path, known, **kwargs)
    assert gkv.main(["--subsystems", str(path), "--check"]) == 1
    assert reason in capsys.readouterr().err


# --- --check-upstream ---


def test_check_upstream_lists_newer_and_backports(fake_github, tmp_path, capsys) -> None:
    path = _write_subsystems(tmp_path, _pins(["1.9.0", "1.2.0", "1.1.1"]))
    assert gkv.main(["--subsystems", str(path), "--check-upstream"]) == 1
    err = capsys.readouterr().err
    assert "newer than the default 1.9.0: 1.11.0" in err
    assert "at or below the default (backports): 1.2.1" in err


def test_check_upstream_ignores_denylisted(fake_github, tmp_path) -> None:
    path = _write_subsystems(tmp_path, _pins(_ALL_IN_RANGE), denylist={"1.11.0": "regression"})
    assert gkv.main(["--subsystems", str(path), "--check-upstream"]) == 0


# --- --list-versions ---


def test_list_versions_is_pinned_minus_denylisted(tmp_path, capsys) -> None:
    path = _write_subsystems(tmp_path, _pins(_ALL_IN_RANGE))
    assert gkv.main(["--subsystems", str(path), "--list-versions"]) == 0
    assert json.loads(capsys.readouterr().out) == _ALL_IN_RANGE


# --- network robustness ---


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _http_error(url: str, code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, code, f"HTTP {code}", None, None)  # type: ignore[arg-type]


@pytest.fixture
def sleeps(monkeypatch) -> list[float]:
    calls: list[float] = []
    monkeypatch.setattr(gkv, "_sleep", calls.append)
    return calls


def test_token_is_sent_only_to_the_api_and_never_redirected(monkeypatch) -> None:
    seen = []

    def fake_urlopen(request, timeout):
        seen.append(request)
        return _Response(b"[]")

    monkeypatch.setattr(gkv, "_urlopen", fake_urlopen)
    gkv._get("https://api.github.com/repos/facebook/pyrefly/releases", "tok")
    gkv._get("https://github.com/facebook/pyrefly/releases/download/1.0.0/x.tar.gz.sha256", "tok")
    api, asset = seen
    # On the API request the header is "unredirected": urllib won't copy it onto a redirect.
    assert api.unredirected_hdrs.get("Authorization") == "Bearer tok"
    assert "Authorization" not in api.headers
    assert not asset.has_header("Authorization")


@pytest.mark.parametrize(
    "failure",
    [
        lambda url: _http_error(url, 503),
        lambda url: _http_error(url, 429),
        lambda url: urllib.error.URLError(ConnectionRefusedError("refused")),
        lambda url: socket.timeout("timed out"),
    ],
)
def test_transient_failures_are_retried_with_backoff(monkeypatch, sleeps, failure) -> None:
    attempts = []

    def fake_urlopen(request, timeout):
        attempts.append(request.full_url)
        if len(attempts) < 3:
            raise failure(request.full_url)
        return _Response(b"ok")

    monkeypatch.setattr(gkv, "_urlopen", fake_urlopen)
    assert gkv._get("https://api.github.com/x", None) == b"ok"
    assert len(attempts) == 3
    assert sleeps == [1, 2]


def test_retries_give_up_after_three_attempts(monkeypatch, sleeps) -> None:
    attempts = []

    def fake_urlopen(request, timeout):
        attempts.append(1)
        raise _http_error(request.full_url, 502)

    monkeypatch.setattr(gkv, "_urlopen", fake_urlopen)
    with pytest.raises(urllib.error.HTTPError):
        gkv._get("https://api.github.com/x", None)
    assert len(attempts) == 3
    assert sleeps == [1, 2]


@pytest.mark.parametrize("code", [404, 403, 401, 400])
def test_client_errors_are_not_retried(monkeypatch, sleeps, code) -> None:
    attempts = []

    def fake_urlopen(request, timeout):
        attempts.append(1)
        raise _http_error(request.full_url, code)

    monkeypatch.setattr(gkv, "_urlopen", fake_urlopen)
    with pytest.raises(urllib.error.HTTPError):
        gkv._get("https://api.github.com/x", None)
    assert len(attempts) == 1
    assert sleeps == []


@pytest.mark.parametrize(
    "content",
    [b"", b"deadbeef  pyrefly.tar.gz\n", b"z" * 64 + b"  x\n", b"a" * 63, b"a" * 65 + b"  x\n"],
)
def test_sidecar_must_be_a_64_hex_digest(content: bytes) -> None:
    with pytest.raises(ValueError, match="64-character hex sha256"):
        gkv.parse_sha256_sidecar(content, "pyrefly-macos-arm64.tar.gz")


def test_sidecar_accepts_digest_with_filename() -> None:
    digest = "ab" * 32
    assert gkv.parse_sha256_sidecar(f"{digest}  pyrefly.tar.gz\n".encode(), "x") == digest


def test_bad_sidecar_fails_pin_generation(monkeypatch, tmp_path) -> None:
    release = _release("1.9.0", False, False)
    monkeypatch.setattr(gkv, "_get", lambda url, token: b"not-a-digest  file\n")
    with pytest.raises(ValueError, match="64-character hex sha256"):
        gkv.pins_for_release(gkv.parse_plugin_config(_SUBSYSTEMS), release, None)


# --- reasons: encoding, quoting, validation, wrapping ---


def _remove(path: Path, version: str, reason: str) -> int:
    return gkv.main(["--subsystems", str(path), "--remove", version, "--reason", reason])


def test_non_bmp_reason_is_written_as_utf8_and_round_trips(tmp_path) -> None:
    reason = "crashes 💥 on startup (see 🐛 tracker)"
    path = _write_subsystems(tmp_path, _pins(_ALL_IN_RANGE))
    assert _remove(path, "1.2.1", reason) == 0
    text = path.read_text(encoding="utf-8")
    # Written as the real characters, never as `💥` surrogate escapes, which Python
    # would read back as two lone surrogates.
    assert "💥" in text and "\\ud83d" not in text and "\\U" not in text
    assert _config(path).denylist == {"1.2.1": reason}


@pytest.mark.parametrize(
    ("reason", "literal"),
    [
        ("plain reason", '"plain reason"'),
        ('says "boom" on start', "'says \"boom\" on start'"),
        ("it's broken", '"it\'s broken"'),
        # Both kinds: ruff keeps double quotes unless there are more double quotes than single.
        ('it\'s "really" broken', "'it\\'s \"really\" broken'"),
        ("it's \"x\" and isn't ok", '"it\'s \\"x\\" and isn\'t ok"'),
        ("back\\slash", '"back\\\\slash"'),
    ],
)
def test_reason_literal_uses_ruff_preferred_quotes(tmp_path, reason, literal) -> None:
    assert gkv.py_literal(reason) == literal
    path = _write_subsystems(tmp_path, _pins(_ALL_IN_RANGE))
    assert _remove(path, "1.2.1", reason) == 0
    assert f'    "1.2.1": {literal},' in path.read_text(encoding="utf-8").split("\n")
    assert _config(path).denylist == {"1.2.1": reason}


@pytest.mark.parametrize("bad", ["two\nlines", "tab\there", "nul\x00", "cr\rhere", "sep x"])
def test_reason_must_be_a_single_line(tmp_path, bad) -> None:
    path = _write_subsystems(tmp_path, _pins(_ALL_IN_RANGE))
    before = path.read_text(encoding="utf-8")
    with pytest.raises(SystemExit, match="reason must be a single line"):
        _remove(path, "1.2.1", bad)
    assert path.read_text(encoding="utf-8") == before


def test_long_reason_is_wrapped_but_stored_exactly(tmp_path) -> None:
    # Double spaces, a trailing space, quotes, an emoji, and an unbreakable long word.
    reason = (
        'Pyrefly 1.2.1 reports  "bad-override" on every   dataclass that inherits a slotted '
        "base, 💥 which breaks incremental adoption; " + "x" * 120 + " end "
    )
    path = _write_subsystems(tmp_path, _pins(_ALL_IN_RANGE))
    assert _remove(path, "1.2.1", reason) == 0
    lines = path.read_text(encoding="utf-8").split("\n")
    assert all(gkv._width(line) <= 100 for line in lines)
    assert '    "1.2.1": (' in lines
    assert _config(path).denylist == {"1.2.1": reason}


# --- line-level editing: everything not being changed stays byte-identical ---

_W6 = """\
MINIMUM_PINNED_VERSION = "1.1.1"

DENYLISTED_VERSIONS: dict[str, str] = {}  # nothing denylisted yet


class Pyrefly(TemplatedExternalTool):
    options_scope = "pyrefly"

    default_version = '1.2.1'  # bumped by hand
    default_url_template = (
        "https://github.com/facebook/pyrefly/releases/download/{version}/pyrefly-{platform}.tar.gz"
    )
    default_url_platform_mapping = {
        "macos_arm64": "macos-arm64",
        "linux_x86_64": "linux-x86_64-musl",
    }
    default_known_versions = [
        # The current default.
        "{p121_mac}",
        '{p121_linux}',  # single-quoted on purpose
        # Oldest supported.
        "{p111_mac}",
        "{p111_linux}",
    ]
"""


def _w6_text() -> str:
    p121_mac, p121_linux = _pins(["1.2.1"])
    p111_mac, p111_linux = _pins(["1.1.1"])
    return (
        _W6.replace("{p121_mac}", p121_mac)
        .replace("{p121_linux}", p121_linux)
        .replace("{p111_mac}", p111_mac)
        .replace("{p111_linux}", p111_linux)
    )


def test_write_inserts_missing_version_and_leaves_every_other_line(fake_github, tmp_path) -> None:
    # The reviewer's W6 scenario: a comment between pins, a single-quoted pin, a trailing comment
    # on `default_version`, and a missing in-range version (1.2.0).
    original = _w6_text()
    path = tmp_path / "subsystems.py"
    path.write_text(original, encoding="utf-8")
    assert gkv.main(["--subsystems", str(path), "--write"]) == 0

    lines = original.split("\n")
    at = lines.index("        # Oldest supported.")
    expected = lines[:at] + [f'        "{pin}",' for pin in _pins(["1.2.0"])] + lines[at:]
    assert path.read_text(encoding="utf-8") == "\n".join(expected)


def test_write_with_version_changes_only_the_version_literal(fake_github, tmp_path) -> None:
    original = _w6_text()
    path = tmp_path / "subsystems.py"
    path.write_text(original, encoding="utf-8")
    assert gkv.main(["--subsystems", str(path), "--version", "1.9.0", "--write"]) == 0

    lines = original.split("\n")
    lines[lines.index("    default_version = '1.2.1'  # bumped by hand")] = (
        "    default_version = '1.9.0'  # bumped by hand"
    )
    # 1.9.0 goes first (above the comment attached to the old first pin); 1.2.0 fills the gap.
    top = lines.index("        # The current default.")
    lines[top:top] = [f'        "{pin}",' for pin in _pins(["1.9.0"])]
    gap = lines.index("        # Oldest supported.")
    lines[gap:gap] = [f'        "{pin}",' for pin in _pins(["1.2.0"])]
    assert path.read_text(encoding="utf-8") == "\n".join(lines)


def test_write_appends_after_a_last_pin_without_trailing_comma(fake_github, tmp_path) -> None:
    p121_mac, p121_linux = _pins(["1.2.1"])
    original = _w6_text().replace(
        f'        "{_pins(["1.1.1"])[1]}",\n', f'        "{_pins(["1.1.1"])[1]}"  # last\n'
    )
    original = original.replace(
        'MINIMUM_PINNED_VERSION = "1.1.1"', 'MINIMUM_PINNED_VERSION = "1.1.0"'
    )
    path = tmp_path / "subsystems.py"
    path.write_text(original, encoding="utf-8")
    assert gkv.main(["--subsystems", str(path), "--write"]) == 0
    text = path.read_text(encoding="utf-8")
    # Only the comma is added to the old last line; 1.1.0 is appended after it.
    assert f'        "{_pins(["1.1.1"])[1]}",  # last' in text
    assert _config(path).known_versions == _pins(["1.2.1", "1.2.0", "1.1.1", "1.1.0"])


def test_remove_deletes_only_pin_lines_and_adds_only_the_entry(tmp_path) -> None:
    # In W6, 1.2.1 is the default and 1.1.1 the minimum; shift both so 1.2.1 can be removed.
    original = _w6_text().replace("default_version = '1.2.1'", "default_version = '1.1.1'")
    original = original.replace(
        'MINIMUM_PINNED_VERSION = "1.1.1"', 'MINIMUM_PINNED_VERSION = "1.0.0"'
    )
    path = tmp_path / "subsystems.py"
    path.write_text(original, encoding="utf-8")
    assert _remove(path, "1.2.1", "bad build") == 0

    lines = original.split("\n")
    lines = [line for line in lines if "1.2.1|" not in line]
    at = lines.index("DENYLISTED_VERSIONS: dict[str, str] = {}  # nothing denylisted yet")
    lines[at : at + 1] = [
        "DENYLISTED_VERSIONS: dict[str, str] = {",
        '    "1.2.1": "bad build",',
        "}  # nothing denylisted yet",
    ]
    assert path.read_text(encoding="utf-8") == "\n".join(lines)


def test_remove_inserts_entry_newest_first_into_existing_denylist(tmp_path) -> None:
    text = _SUBSYSTEMS.replace(
        "DENYLISTED_VERSIONS: dict[str, str] = {}",
        "DENYLISTED_VERSIONS: dict[str, str] = {\n"
        "    # Newer problem.\n"
        "    '1.10.0': 'bad',\n"
        '    "1.0.5": "old"  # no trailing comma\n'
        "}",
    )
    path = tmp_path / "subsystems.py"
    path.write_text(text, encoding="utf-8")
    assert _remove(path, "1.2.0", "mid") == 0
    lines = path.read_text(encoding="utf-8").split("\n")
    start = lines.index("DENYLISTED_VERSIONS: dict[str, str] = {")
    assert lines[start : start + 5] == [
        "DENYLISTED_VERSIONS: dict[str, str] = {",
        "    # Newer problem.",
        "    '1.10.0': 'bad',",
        '    "1.2.0": "mid",',
        '    "1.0.5": "old"  # no trailing comma',
    ]
    assert _config(path).denylist == {"1.10.0": "bad", "1.2.0": "mid", "1.0.5": "old"}


def test_remove_refuses_already_denylisted(tmp_path) -> None:
    path = _write_subsystems(tmp_path, _pins(["1.9.0"]), denylist={"1.2.1": "bad"})
    with pytest.raises(SystemExit, match="already denylisted"):
        _remove(path, "1.2.1", "again")


# --- cut-off downloads ---


class _CutOff(_Response):
    def __init__(self, error: Exception) -> None:
        super().__init__(b"")
        self._error = error

    def read(self, *args):  # type: ignore[override]
        raise self._error


@pytest.mark.parametrize(
    "make_error",
    [
        lambda: http.client.IncompleteRead(b"partial", 100),
        lambda: ConnectionResetError("reset by peer"),
    ],
    ids=["IncompleteRead", "ConnectionResetError"],
)
def test_errors_while_reading_the_body_are_retried(monkeypatch, sleeps, make_error) -> None:
    attempts = []

    def fake_urlopen(request, timeout):
        attempts.append(1)
        return _CutOff(make_error()) if len(attempts) < 3 else _Response(b"ok")

    monkeypatch.setattr(gkv, "_urlopen", fake_urlopen)
    assert gkv._get("https://github.com/x.tar.gz", None) == b"ok"
    assert len(attempts) == 3
    assert sleeps == [1, 2]
