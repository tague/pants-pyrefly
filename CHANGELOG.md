# Changelog

All notable changes to `pants-pyrefly` are documented here. This project adheres to
[Semantic Versioning](https://semver.org/).

## Unreleased

- **Every stable Pyrefly from 1.1.1 up to the default is now pinned**: 1.1.1, 1.2.0, 1.2.1,
  1.3.0, 1.3.1, and 1.3.2. Select any of them with just `[pyrefly].version = "..."`; no
  `known_versions` override is needed. Existing `version = "1.1.1"` or `"1.2.0"` pins keep working
  after upgrading the plugin. (The 1.0.0 entry's advice to pin `1.1.1` did not actually work on
  1.0.0, which pinned only 1.2.0; it works as of this release.) See
  [Supported Pyrefly versions](README.md#supported-pyrefly-versions) for the policy.
- **The default pinned Pyrefly is now 1.3.2** (was 1.2.0). No plugin behavior changes, but the
  newer Pyrefly changes what some builds report:
  - **Renamed error kind.** The dataclass-field-assigned-an-inconsistent-data-descriptor error
    that 1.2.0 reported as `bad-class-definition` is `bad-dataclass-descriptor` in 1.3. Anything
    that names the old kind stops matching it under 1.3.x: 1.2.0-era baseline entries,
    `# pyrefly: ignore[bad-class-definition]` comments (including ones `pants pyrefly-suppress`
    wrote), `[pyrefly].only`, and `[errors]` settings in your Pyrefly config. Under the no-config
    `basic` preset, 1.3.2 does not report this error at all.
  - **New diagnostics** can surface errors 1.2.0 did not report, e.g. invalid literal regular
    expressions (`regex`). Which ones fire depends on your config: `regex` is reported when a
    Pyrefly config file is in effect, but not under the `basic` preset. Invalid `mock.patch`
    targets are reported as warnings (`missing-attribute-patch-target`), which the default `error`
    severity hides. Open-type match exhaustiveness (`non-exhaustive-match-open-type`) is off unless
    you enable it in `[errors]`. The summary line now also counts hidden warnings
    (`INFO 2 errors (… 1 warning not shown)`).
  - **Compact baseline format.** `pants pyrefly-update-baseline` now writes entries with `path`,
    `column`, `name`, `concise_description`, and `severity`, and drops `line`, `stop_line`,
    `stop_column`, `code`, and `description`, so regenerating a committed baseline rewrites the
    whole file once. 1.3.x still honors existing full-format baselines, except entries for the
    renamed kind above. The change is one-way: a baseline written by 1.3.x does **not** suppress
    anything under older Pyrefly, so regenerate it after pinning back.
  - `# type: ignore[<code>]` comments carrying another tool's codes (e.g. a MyPy code) still
    suppress everything on the line, as before. 1.3 adds `# type: ignore[pyrefly:<code>]` for
    targeted suppression.
- **Removed Pyrefly versions now fail with a reason.** A version is removed from the pins only
  deliberately, with the reason recorded in `DENYLISTED_VERSIONS` (in `subsystems.py`) and a
  CHANGELOG note. Selecting one with `[pyrefly].version` fails with
  `DenylistedPyreflyVersion: Pyrefly X is not supported by pants-pyrefly: <reason>. Set
  [pyrefly].version to a supported release (<default>).` instead of Pants's generic
  `UnknownVersion`. Supplying your own `[pyrefly].known_versions` entry for it still works. The
  denylist is empty in this release.
- **Fixed: Pyrefly 1.3's baseline flags in `[pyrefly].args`.**
  - **`--error-stale-baseline` now works.** Before, every partition received the whole merged
    baseline. Inside a partition's sandbox, entries for files checked by other partitions point
    at paths that do not exist, so Pyrefly reported them as stale. `check` then failed every
    partition even when nothing was stale, and also failed when checking a subset such as
    `pants check src/a::`. Each partition now gets only the entries for its own files. A truly
    stale entry fails only the partition that checks its file. Entries for deleted files are still
    reported as stale, once per run, by the first partition, so a subset run can fail on them
    too. Without the flag, the same errors are gated as before; an entry only ever matched errors
    in its own file.
  - **Known limitation:** baseline entries are keyed by path only. A file checked by several
    partitions (e.g. `parametrize` over interpreter constraints) with an error in only some of
    them keeps failing `--error-stale-baseline`. Use an inline
    `# pyrefly: ignore[<error-kind>]` for such errors. See the README.
  - **`pants pyrefly-update-baseline` ignores `--error-stale-baseline`.** Before, it failed with
    exit code 2, because Pyrefly rejects the flag alongside `--update-baseline`.
  - **`--prune-baseline` now fails fast.** `check` and `pyrefly-update-baseline` exit with a
    `PyreflyArgsError` that names `pants pyrefly-update-baseline ::`. Before, `check` passed and the
    baseline file was silently left unchanged, because Pyrefly pruned a temporary copy in the
    sandbox; `pyrefly-update-baseline` failed with exit code 2. When `check` fails on stale
    entries, Pyrefly's "rerun with `--prune-baseline`" hint now says to run
    `pants pyrefly-update-baseline ::` instead. The README now warns that update-baseline rewrites
    the whole file from only the targets given, so a subset run drops all other entries.
- Releases are now gated on a Pyrefly compatibility suite
  (`build-support/ci/compat_test.sh`, run by `.github/workflows/compat.yml` for every supported
  version). It drives real Pants runs of `check`, `pyrefly-update-baseline`, and
  `pyrefly-suppress` with only `--pyrefly-version` set, and asserts that the requested Pyrefly
  binary is the one that ran. It also runs the default Pyrefly on the latest patch of every
  supported Pants minor: 2.27.1, 2.28.1, 2.29.1, 2.30.2, 2.31.0, 2.32.1, and 2.33.1, and against
  a `CPython==3.9.*` project, asserting that Pyrefly checks it as Python 3.9. The release workflow
  publishes nothing if any of these jobs fails.
- Maintenance: `build-support/bin/generate_known_versions.py` now manages the pins.
  `--write` only adds pins (stable releases up to the default, skipping pre-releases and denylisted
  versions) and never rewrites existing ones. `--check` verifies only the shipped pins (checksums,
  default pinned, order, nothing denylisted), so new upstream releases or backports never fail it.
  `--check-upstream` lists stable releases that are neither pinned nor denylisted. `--remove` is
  the only way to drop a version, `--list-versions` prints the supported set, and
  `--default-version` prints the default. `--write` and `--remove` edit `subsystems.py` line by
  line, leaving comments and formatting untouched, and denylist reasons are written as single-line
  UTF-8 literals in `ruff format`'s quote style.
  Failures print a single `error: ...` line (with file and line where relevant), never a
  traceback. The script sends the GitHub token only to `api.github.com`, rejects malformed `.sha256` sidecars,
  and retries transient network failures, including downloads cut off mid-body.
- Maintenance: a weekly workflow (`.github/workflows/upstream.yml`) runs
  `generate_known_versions.py --check-upstream` and fails when a stable Pyrefly release is neither
  pinned nor denylisted, showing the script's output in the job summary. It only reports: it
  never writes pins or opens a PR. On failure, `--check-upstream` prints the exact commands to pin
  the new releases (or skip one).
- Maintenance: Dependabot (`.github/dependabot.yml`) now opens one grouped weekly PR for GitHub
  Actions updates. Python dependencies stay out of it, since Dependabot cannot regenerate Pants
  lockfiles.
- Maintenance: the repo now develops, tests, and builds releases on Pants 2.33.1 (was 2.32.0). PR
  CI smoke-tests the latest patch of a subset of the supported minors: 2.27.1, 2.31.0, 2.32.1, and
  2.33.1. The supported Pants range (2.27–2.33) and the published wheel's requirements are
  unchanged.

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
  rules-API change at 2.29 (`coarsened_targets` → `resolve_coarsened_targets`) and the
  `CheckSubsystem.default_process_cache_scope` addition at 2.32. Verified on 2.27 and 2.32.
- The published wheel is pure-Python (`Requires-Python: >=3.12`) and carries **no `pantsbuild.pants`
  dependency** (Pants provides itself at runtime, and is no longer on PyPI). It installs into any
  Pants on CPython 3.12+ (Pants 2.27, on CPython 3.11, uses the from-source install). Verified
  end-to-end via a `plugins=["… @ file://…whl"]` install.
