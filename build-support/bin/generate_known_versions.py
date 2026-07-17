#!/usr/bin/env python3
# Copyright 2026 Tague Griffith
# Licensed under the Apache License, Version 2.0 (see LICENSE).

"""Generate or verify the Pyrefly `default_known_versions` pins in `subsystems.py`.

Each pin is `"<version>|<pants_platform>|<sha256>|<size_bytes>"`. Rather than hand-computing
four SHA256/size pairs on every Pyrefly bump, this reads the plugin's own `subsystems.py`
(via `ast`, so the script and the plugin can never disagree on the version, URL template, or
platform mapping), fetches the published `.sha256` sidecars and asset sizes from the
facebook/pyrefly GitHub release, and emits the pins.

Usage (run directly; pure stdlib, no Pants required):

    GEN=build-support/bin/generate_known_versions.py
    python3 $GEN                    # print pins for the current version
    python3 $GEN --version 1.2.0    # print pins for a specific version
    python3 $GEN --write            # rewrite subsystems.py in place
    python3 $GEN --check            # CI: fail if the committed pins are stale

Set `GITHUB_TOKEN` (or pass `--token`) to raise the GitHub API rate limit.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SUBSYSTEMS = REPO_ROOT / "pants-plugins" / "pants_pyrefly" / "subsystems.py"
RELEASE_API = "https://api.github.com/repos/facebook/pyrefly/releases/tags/{version}"


@dataclass
class PluginConfig:
    """The Pyrefly download config parsed out of `subsystems.py`."""

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


def _assignments(class_node: ast.ClassDef) -> dict[str, ast.Assign]:
    out: dict[str, ast.Assign] = {}
    for node in class_node.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                out[target.id] = node
    return out


def parse_plugin_config(text: str) -> PluginConfig:
    tree = ast.parse(text)
    assigns = _assignments(_find_class(tree, "Pyrefly"))
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


def _asset_filename(url_template: str, version: str, url_platform: str) -> str:
    return url_template.format(version=version, platform=url_platform).rsplit("/", 1)[-1]


def compute_known_versions(config: PluginConfig, version: str, token: str | None) -> list[str]:
    """Fetch sha256 + size for each mapped platform and return the pin lines, in mapping order."""
    release = json.loads(_get(RELEASE_API.format(version=version), token))
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--subsystems",
        type=Path,
        default=DEFAULT_SUBSYSTEMS,
        help="Path to subsystems.py (default: the plugin's).",
    )
    parser.add_argument(
        "--version",
        default=None,
        help="Pyrefly version to pin (default: the current `default_version`).",
    )
    parser.add_argument("--write", action="store_true", help="Rewrite subsystems.py in place.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if the current pins are stale (for CI). Ignores --version.",
    )
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN"), help="GitHub API token.")
    args = parser.parse_args(argv)

    config = parse_plugin_config(args.subsystems.read_text())

    if args.check:
        pins = compute_known_versions(config, config.version, args.token)
        if pins != config.known_versions:
            print(
                f"Pyrefly pins are stale for version {config.version}.\n"
                "Regenerate with `python3 build-support/bin/generate_known_versions.py --write`.\n",
                file=sys.stderr,
            )
            print("expected:", file=sys.stderr)
            for pin in pins:
                print(f"  {pin}", file=sys.stderr)
            print("found:", file=sys.stderr)
            for pin in config.known_versions:
                print(f"  {pin}", file=sys.stderr)
            return 1
        print(f"Pyrefly pins are current for version {config.version}.")
        return 0

    version = args.version or config.version
    pins = compute_known_versions(config, version, args.token)

    if args.write:
        rewrite_subsystems(args.subsystems, config, version, pins)
        print(f"Wrote {len(pins)} Pyrefly {version} pin(s) to {args.subsystems}.")
        return 0

    print("\n".join(render_known_versions_block(pins)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
