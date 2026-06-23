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
- `[pyrefly]` subsystem options (`skip`, `args`, `output_format`, `extra_type_stubs`, `config`,
  `config_discovery`, `version`, …) and a per-target `skip_pyrefly` field. `extra_type_stubs` injects
  stub-only packages into the environment Pyrefly inspects; an explicit `[pyrefly].config` path is
  passed through to Pyrefly via `--config`.
- The list of files to check is passed to Pyrefly via an argfile, so large targets never hit OS
  command-line length limits.
- Incremental adoption: `[pyrefly].baseline` makes `check` report only errors new since the
  baseline, and the `pants pyrefly-update-baseline` goal records/refreshes that baseline file.
- `pants pyrefly-lsp-config` writes Pants's source roots into `pyrefly.toml` as `search-path`, so
  the Pyrefly editor/LSP resolves first-party imports the way Pants does.
- Triage controls `[pyrefly].min_severity` and `[pyrefly].only`; and `check` now flags a Pyrefly
  tool failure (an exit code other than 0/1) distinctly from ordinary type errors.
- `pants pyrefly-coverage` reports overall type coverage (% of typable symbols typed), with an
  optional `--pyrefly-coverage-fail-under` threshold to ratchet/gate it.
- Supports Pants `2.27`–`2.32` from a single codebase, via version-conditional imports for the
  rules-API changes at 2.30 (`coarsened_targets` → `resolve_coarsened_targets`) and the
  `CheckSubsystem.default_process_cache_scope` addition. Verified on 2.27 and 2.32.
- The published wheel is pure-Python (`Requires-Python: >=3.12`) and carries **no `pantsbuild.pants`
  dependency** (Pants provides itself at runtime, and is no longer on PyPI). It installs into any
  Pants on CPython 3.12+ (Pants 2.27, on CPython 3.11, uses the from-source install). Verified
  end-to-end via a `plugins=["… @ file://…whl"]` install.
