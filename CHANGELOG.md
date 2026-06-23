# Changelog

All notable changes to `pants-pyrefly` are documented here. This project adheres to
[Semantic Versioning](https://semver.org/).

## 0.1.0 (unreleased)

Initial release.

- Run [Pyrefly](https://pyrefly.org/) (default `1.1.1`) as a Python type checker in the Pants
  `check` goal.
- Download the official prebuilt Pyrefly binary per platform (macOS arm64/x86_64, Linux
  arm64/x86_64 musl), pinned by SHA256.
- First-party import resolution via `--search-path`, third-party resolution via a materialized
  requirements venv (`--python-interpreter-path`).
- `[pyrefly]` subsystem options (`skip`, `args`, `extra_type_stubs`, `config`, `config_discovery`,
  `version`, …) and a per-target `skip_pyrefly` field. `extra_type_stubs` injects stub-only packages
  into the environment Pyrefly inspects; an explicit `[pyrefly].config` path is passed through to
  Pyrefly via `--config`.
- Supports Pants `2.27`–`2.32` from a single codebase, via version-conditional imports for the
  rules-API changes at 2.30 (`coarsened_targets` → `resolve_coarsened_targets`) and the
  `CheckSubsystem.default_process_cache_scope` addition. Verified on 2.27 and 2.32.
