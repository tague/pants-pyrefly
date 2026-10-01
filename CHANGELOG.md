# Changelog

All notable changes to `pants-pyrefly` are documented here. This project adheres to
[Semantic Versioning](https://semver.org/).

## Unreleased

- **The default pinned Pyrefly is now 1.3.2** (was 1.2.0). No plugin behavior changes, but the
  newer Pyrefly changes what some builds report:
  - New diagnostics can surface errors that 1.2.0 did not report, e.g. invalid literal regular
    expressions (`regex`), descriptor-backed dataclass fields (`bad-dataclass-descriptor`), and
    open-type match exhaustiveness (`non-exhaustive-match-open-type`). Which ones fire depends on
    your config: `regex`, for example, is reported when a Pyrefly config file is in effect but not
    under the no-config `basic` preset. Invalid `mock.patch`
    targets are reported as warnings (`missing-attribute-patch-target`), which are hidden at the
    default `error` severity. The summary line now also counts hidden warnings
    (`INFO 2 errors (… 1 warning not shown)`).
  - `pants pyrefly-update-baseline` now writes a compact baseline format: entries keep `path`,
    `column`, `name`, `concise_description`, and `severity`, and drop `line`, `stop_line`,
    `stop_column`, `code`, and `description`. Regenerating a committed baseline therefore rewrites
    the whole file once. Existing 1.2.0-format baselines are still honored by 1.3.2, but a baseline
    written by 1.3.2 does **not** gate errors under 1.2.0. If you might pin back, keep the old
    baseline until you're sure.
  - `# type: ignore[<code>]` comments carrying another tool's codes (e.g. a MyPy code) still
    suppress everything on the line, as before. 1.3 adds `# type: ignore[pyrefly:<code>]` for
    targeted suppression.

  To pin back, set both the version and its hashes (the plugin only ships pins for its default
  version, so setting `version` alone fails with `UnknownVersion`):

  ```toml
  [pyrefly]
  version = "1.2.0"
  known_versions = [
    "1.2.0|macos_arm64|312ab21e60fb4385a4cd5ef68bc70e2475d7b541a5cb5a30329db726b2b16e39|12988333",
    "1.2.0|macos_x86_64|f1856386d167696af3fe05b5c2fbe807845e33da1024706cbe979c74ac7d7cdd|13661410",
    "1.2.0|linux_arm64|5b27d702c8b8463090fe19ca4e2aa241bf8f2b09daf208feff051a90e4d12cee|13457814",
    "1.2.0|linux_x86_64|18f509653a52fab1aab98d5b776486a4f278c04cc108fec8b52c131785f6d423|14080825",
  ]
  ```

## 1.0.0 (2026-08-13)

First stable release. No breaking changes from 0.5.0 — the goals, `[pyrefly]` options, and
`skip_pyrefly` field all behave as they did; the bump declares the surface stable rather than
changing it. See [Stability](README.md#stability) for what the compatibility promise now covers
and what stays an implementation detail.

- **Pants 2.33 is supported.** The consumption smoke-test matrix now covers 2.27, 2.31, 2.32, and
  2.33, so the version-conditional rules-API shim is verified on the current Pants line.
- **The default pinned Pyrefly is now 1.2.0** (was 1.1.1). Pin the old one with
  `[pyrefly].version = "1.1.1"` if a new Pyrefly release changes what your build reports.
- Documented how import resolution actually works as of 0.5.0: the README described only the 0.4.0
  nearest-root dedup, which `check` and `pyrefly-suppress` no longer use — they re-stage each source
  root into an isolated sibling directory instead. The diagnostic goals (`pyrefly-coverage`,
  `pyrefly-dump-config`, `pyrefly-lsp-config`) still pass nearest-root-deduped real source roots.

## 0.5.0 (2026-08-02)

- Nested source roots no longer produce spurious duplicate-module errors, even when the ancestor
  root is one that a file genuinely roots at (e.g. the build root `.` when a top-level `scripts/`
  or `tools/` package lives there). `check` now re-stages each source root's files into an isolated,
  non-nesting synthetic directory before invoking Pyrefly and passes `--disable-search-path-heuristics`,
  so every file is reachable under exactly one module identity regardless of how `[source] root_patterns`
  nest or how the dependency graph is shaped. This generalizes the 0.4.0 nearest-root dedup, which
  could only drop an ancestor root that *no* file needed — it could not fix a repo where first-party
  code roots at both an ancestor and a descendant (the common `.`-plus-`src/python` layout), leaving
  those errors to a baseline. Pyrefly's synthetic output paths are mapped back to real repo paths in
  diagnostics, the `pants pyrefly-update-baseline` file, and `pants pyrefly-suppress` edits.
- `pants pyrefly-update-baseline` and `pants pyrefly-suppress` re-stage the same way, so a regenerated
  baseline no longer records the spurious errors and `suppress` no longer inserts `# pyrefly: ignore`
  comments for them.

## 0.4.0 (2026-07-24)

- Nested/overlapping Pants source roots are now deduplicated before being passed to Pyrefly as
  `--search-path` (applies to `check`, the goals, and `pants pyrefly-lsp-config`). Previously, a repo
  with both `src` and `src/python` in `[source] root_patterns` made every module under the nested
  root reachable under two names (`pkg.mod` and `python.pkg.mod`), which Pyrefly reported as spurious
  duplicate-module / "not assignable to itself" errors. The plugin now emits only each file's nearest
  source root; a redundant ancestor root is dropped, and a genuine layout collision (first-party code
  living directly under both an ancestor and a nested root) is surfaced as a warning.
- New advanced option `[pyrefly].exclude_source_roots` to force-drop specific source roots from the
  search path, for the rare case the automatic dedup should keep one but you don't want it.

## 0.3.0 (2026-07-18)

- `pants pyrefly-init` bootstraps a Pyrefly config for the repo (wraps `pyrefly init`), migrating an
  existing MyPy or Pyright configuration when one is found. `--pyrefly-init-migrate-from=<auto|mypy|pyright>`
  selects the source. It refuses to overwrite an existing config.
- `pants pyrefly-dump-config` prints the effective Pyrefly configuration Pants assembles for the
  targeted sources (first-party `search-path`s, the resolved interpreter, and the config file in
  effect) by running Pyrefly's `dump-config` subcommand. Diagnostic only — it does not type-check.
- Pyrefly version pins (`default_known_versions`) are now generated by
  `build-support/bin/generate_known_versions.py` instead of being hand-edited: it fetches each
  asset's published `.sha256` and size from the GitHub release. Bump with `--version <new> --write`;
  CI runs `--check` to fail if the committed pins drift from the release.

## 0.2.0 (2026-07-10)

- `pants pyrefly-suppress` inserts inline `# pyrefly: ignore` comments for the current errors in the
  targeted sources (wraps Pyrefly's `suppress`); `--pyrefly-suppress-remove-unused` strips stale
  ones. The inline-comment alternative to the baseline for incremental adoption.
- **Lowered the published wheel's floor to `Requires-Python: >=3.11`** (was `>=3.12`), so one wheel
  installs into every supported Pants — 2.27 (CPython 3.11) through 2.32 (CPython 3.14) — and added
  per-version Python trove classifiers (3.11–3.14).
- Added a MyPy→Pyrefly migration guide (`docs/migrating-from-mypy.md`).
- CI now runs a consumption smoke-test matrix across Pants 2.27/2.31/2.32 (proving the version shim
  on every line), plus tests for lsp-config pyproject protection, `[pyrefly].only`, and the
  tool-failure exit path.

## 0.1.0 (2026-06-23)

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
