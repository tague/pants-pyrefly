# Copyright 2026 Tague Griffith
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

from collections.abc import Iterable

from pants.backend.python.util_rules.interpreter_constraints import InterpreterConstraints
from pants.core.goals.resolves import ExportableTool
from pants.core.util_rules.config_files import ConfigFilesRequest
from pants.core.util_rules.external_tool import TemplatedExternalTool
from pants.engine.platform import Platform
from pants.engine.rules import Rule, collect_rules
from pants.engine.unions import UnionRule
from pants.option.option_types import (
    ArgsListOption,
    BoolOption,
    FileOption,
    SkipOption,
    StrListOption,
    StrOption,
)
from pants.util.strutil import help_text


class Pyrefly(TemplatedExternalTool):
    options_scope = "pyrefly"
    name = "Pyrefly"
    help = help_text(
        """
        Pyrefly, a fast Python type checker written in Rust (https://pyrefly.org).

        Pants downloads the official prebuilt Pyrefly binary from the project's GitHub
        releases and runs it as part of the `check` goal.
        """
    )

    default_version = "1.1.1"
    default_url_template = (
        "https://github.com/facebook/pyrefly/releases/download/{version}/pyrefly-{platform}.tar.gz"
    )
    # Linux uses the statically-linked musl builds so the binary runs on any distro
    # regardless of the host glibc version.
    default_url_platform_mapping = {
        "macos_arm64": "macos-arm64",
        "macos_x86_64": "macos-x86_64",
        "linux_arm64": "linux-arm64-musl",
        "linux_x86_64": "linux-x86_64-musl",
    }
    default_known_versions = [
        "1.1.1|macos_arm64|022a989d2af4748e4d75a48fed7dbb0cc49f30a4b83745d4e4f742d0920ada70|12621775",
        "1.1.1|macos_x86_64|191c7ee2891d2ab55a05b078c94832266e1dda78a9a0381a95fde13a2a27a38b|13278762",
        "1.1.1|linux_arm64|f55454ac41ed1c086af1bd3cfbe2c2a25b960e46551df50ce047bc1ccb11fb35|13028969",
        "1.1.1|linux_x86_64|fc591b4b283ceddb81116a8dd5c0e70d4f1a7dd291521c4debe0cd588c7fd74c|13660591",
    ]

    skip = SkipOption("check")
    args = ArgsListOption(example="--python-version 3.12")

    output_format = StrOption(
        default=None,
        help=help_text(
            """
            Override Pyrefly's error output format: one of `min-text`, `full-text`, `json`,
            `github` (GitHub Actions annotations), `junit-xml`, or `omit-errors`. Defaults to
            Pyrefly's own default.
            """
        ),
    )

    min_severity = StrOption(
        default=None,
        help=help_text(
            """
            Only display errors at or above this severity: one of `ignore`, `info`, `warn`, or
            `error`.
            """
        ),
    )

    only = StrListOption(
        default=[],
        help=help_text(
            """
            Only report these Pyrefly error kinds (e.g. `bad-assignment`, `missing-attribute`),
            filtering out all others. Useful for triaging one category at a time.
            """
        ),
    )

    extra_type_stubs = StrListOption(
        advanced=True,
        default=[],
        help=help_text(
            """
            Extra type-stub requirements to make available to Pyrefly without adding them as
            runtime dependencies of your code, e.g.
            `["types-requests", "sqlalchemy2-stubs==0.0.2a38"]`.

            They are resolved and merged into the third-party environment Pyrefly inspects. Pin
            versions in the requirement strings for reproducible results, since they are resolved
            directly rather than from a lockfile.
            """
        ),
    )

    config = FileOption(
        default=None,
        advanced=True,
        help=help_text(
            """
            Path to a Pyrefly config file (a `pyrefly.toml`, or a `pyproject.toml` with a
            `[tool.pyrefly]` table).

            Setting this option disables config discovery; use it only when the config lives in a
            non-standard location.
            """
        ),
    )
    config_discovery = BoolOption(
        default=True,
        advanced=True,
        help=help_text(
            """
            If true, Pants will include any relevant config files during runs (`pyrefly.toml` and
            `pyproject.toml` files with a `[tool.pyrefly]` table).

            Use `[pyrefly].config` instead if your config is in a non-standard location.
            """
        ),
    )

    # Deliberately a StrOption, not a FileOption: the path need not exist yet (it is created by
    # `pants pyrefly-update-baseline`), and FileOption validates existence at option-parse time.
    baseline = StrOption(
        default=None,
        help=help_text(
            """
            Path to a Pyrefly baseline JSON file. When set, `pants check` reports only type errors
            introduced *after* the baseline was taken — handy for adopting Pyrefly on code that
            already has errors. Create or refresh it with `pants pyrefly-update-baseline`.
            """
        ),
    )

    _interpreter_constraints = StrListOption(
        advanced=True,
        default=["CPython>=3.9,<3.15"],
        help="Fallback interpreter constraints to use when a target has none of its own.",
    )

    @property
    def interpreter_constraints(self) -> InterpreterConstraints:
        return InterpreterConstraints(self._interpreter_constraints)

    def generate_exe(self, plat: Platform) -> str:
        # Every release archive unpacks to a single `pyrefly` binary at the root.
        return "./pyrefly"

    def config_request(self) -> ConfigFilesRequest:
        return ConfigFilesRequest(
            specified=self.config,
            specified_option_name=f"[{self.options_scope}].config",
            discovery=self.config_discovery,
            check_existence=["pyrefly.toml"],
            check_content={"pyproject.toml": b"[tool.pyrefly"},
        )


def rules() -> Iterable[Rule | UnionRule]:
    return (
        *collect_rules(),
        UnionRule(ExportableTool, Pyrefly),
    )
