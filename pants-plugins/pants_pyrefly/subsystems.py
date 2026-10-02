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

# The oldest Pyrefly release the plugin ships pins for. `Pyrefly.default_known_versions` carries
# every stable Pyrefly release from this version up to `Pyrefly.default_version`, so any of them
# can be selected with `[pyrefly].version` alone. 1.1.1 was the plugin's first default. During
# plugin 1.x this only moves down (older releases are added on request), never up.
# `build-support/bin/generate_known_versions.py` reads this value; it is the single source of truth.
MINIMUM_PINNED_VERSION = "1.1.1"


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

    default_version = "1.3.2"
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
        "1.3.2|macos_arm64|7c0b2109a00ccca83e22daa3724d4a091a1b673823175004eb449ba88ab2adb7|13908867",
        "1.3.2|macos_x86_64|5ceb539168b2cd681d032f6e2b6d95571fd92e6cad70d11acd9284660d56e88e|14694123",
        "1.3.2|linux_arm64|3d0ec81c08dbb4a5251dd15800131e8d82b3f09c118f6c669ccbf1bf486db65e|14382737",
        "1.3.2|linux_x86_64|11bc0951e77e5fe2eb84b3e297bc2af598e6008b2f2788336cbc610a254a303b|15096616",
        "1.3.1|macos_arm64|3c7294e86efb53d6bc46619fd17002266731a79894a668b8e9db2ec189e746d2|13908242",
        "1.3.1|macos_x86_64|497d5743ff54f1b4a444f72a4147d7b85fa8cf20bf87123304b00b6173254545|14693601",
        "1.3.1|linux_arm64|c7770869c96bb3dfa4c39c30b644e1d4c61e8f8645069ebf2dac117c34cb9cd1|14386211",
        "1.3.1|linux_x86_64|4859097cc9b00b8719993dc8228d2bffcc8659414d32494bb752ccf9d535dc21|15101817",
        "1.3.0|macos_arm64|c15ad88f494c03e434290a7a22ada4028a184f5f9140e2e869b6cd6eea3637c8|13910637",
        "1.3.0|macos_x86_64|f82c8dae0975ef3929636df20cff02294b0be80bda9825910f1e3164954b9aef|14693107",
        "1.3.0|linux_arm64|954833c74958d844139e02bf0d240aa05cd96c231086069d00658a3aebad44bf|14389737",
        "1.3.0|linux_x86_64|97586de84f392a9c2220fdded4ae58a1528e939bff237a0693be140843e6969f|15100958",
        "1.2.1|macos_arm64|b9ff6b22c1cce276d2ce2d84ee7770f2794f4e1efdea239def046264ee1b5197|12966687",
        "1.2.1|macos_x86_64|2b891db343c111c48d8be32d9b93ac0adf7bd9a93d6bdeb5303e770592a091cd|13638465",
        "1.2.1|linux_arm64|b2d4653ee1b64cc3eec3a0b21d9fd03715dc33c33e0bc78e7804d5468cd03545|13434357",
        "1.2.1|linux_x86_64|ed118dde5b160ad98b5260a5511dcfc5bb8d4b7e04f483c985d2df6ddaf0bbad|14068238",
        "1.2.0|macos_arm64|312ab21e60fb4385a4cd5ef68bc70e2475d7b541a5cb5a30329db726b2b16e39|12988333",
        "1.2.0|macos_x86_64|f1856386d167696af3fe05b5c2fbe807845e33da1024706cbe979c74ac7d7cdd|13661410",
        "1.2.0|linux_arm64|5b27d702c8b8463090fe19ca4e2aa241bf8f2b09daf208feff051a90e4d12cee|13457814",
        "1.2.0|linux_x86_64|18f509653a52fab1aab98d5b776486a4f278c04cc108fec8b52c131785f6d423|14080825",
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

    exclude_source_roots = StrListOption(
        advanced=True,
        default=[],
        help=help_text(
            """
            Pants source roots to omit from the `--search-path` Pyrefly resolves first-party
            imports against.

            The plugin already drops a source root that is redundant with a more specific one
            (e.g. `src` when `src/python` already covers every module beneath it), so you rarely
            need this. Use it to force-drop a root the automatic logic keeps — for example when
            first-party code lives directly under a parent root that also shadows a nested one.
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
