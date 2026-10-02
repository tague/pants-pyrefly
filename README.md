# pants-pyrefly

A [Pants](https://www.pantsbuild.org/) plugin that runs [Pyrefly](https://pyrefly.org/) —
Meta's fast, Rust-based Python type checker — as part of the Pants `check` goal.

Pants downloads the official prebuilt Pyrefly binary (pinned by SHA256) and runs it hermetically
in a sandbox, wiring up your first-party source roots and the resolved third-party dependencies so
that imports resolve correctly.

## Requirements

- **Pants 2.27–2.33.** A single codebase supports both the legacy (`Get`/`MultiGet`-era) and modern
  (call-by-name) rules APIs via a small version-conditional import; verified on 2.27 and 2.33.
- The **published wheel** is pure-Python — `Requires-Python: >=3.11`, with **no `pantsbuild.pants`
  dependency** (Pants provides itself at runtime) — so a single release installs into any supported
  Pants, from 2.27 (CPython 3.11) through 2.33 (CPython 3.14).

## Installation

Add the plugin and enable its backend in `pants.toml`:

```toml
[GLOBAL]
plugins = ["pants-pyrefly==1.0.0"]
backend_packages.add = [
    "pants.backend.python",
    "pants_pyrefly",
]
```

### From source (in-repo)

Prefer to vendor the plugin — for rapid iteration, or to pin to an exact source state? Consume it
the way in-repo plugins are normally loaded: copy `pants-plugins/pants_pyrefly/` into your repo and:

```toml
[GLOBAL]
pythonpath = ["%(buildroot)s/pants-plugins"]
backend_packages.add = ["pants.backend.python", "pants_pyrefly"]
```

If you keep plugin code in a dedicated `pants-plugins` resolve, add it there and run
`pants generate-lockfiles`.

## Getting started

Bootstrap a Pyrefly config for the repo (wraps `pyrefly init`). If you already have a MyPy or
Pyright configuration, it is migrated into the new `pyrefly.toml`:

```bash
pants pyrefly-init                              # create pyrefly.toml (auto-migrates mypy/pyright)
pants pyrefly-init --pyrefly-init-migrate-from=mypy   # force migrating from a MyPy config
```

It refuses to overwrite an existing `pyrefly.toml` (or a `[tool.pyrefly]` table in
`pyproject.toml`) — remove it first to regenerate. Then run `pants pyrefly-lsp-config` (see
[Editor / IDE](#editor--ide-lsp)) so your editor resolves first-party imports the way Pants does.

## Usage

```bash
pants check ::                 # type-check everything
pants check path/to/dir::      # type-check a subtree
```

## Configuration

`[pyrefly]` subsystem options:

| Option | Env / flag | Description |
| --- | --- | --- |
| `skip` | `--pyrefly-skip` / `PANTS_PYREFLY_SKIP` | Don't run Pyrefly during `check`. |
| `args` | `--pyrefly-args` | Extra args passed to Pyrefly, e.g. `--pyrefly-args='--python-version 3.12'`. |
| `extra_type_stubs` | `--pyrefly-extra-type-stubs` | Stub-only packages to add to the type-check environment without making them runtime deps, e.g. `types-requests`, `sqlalchemy2-stubs==0.0.2a38`. Resolved directly, so pin versions for reproducibility. |
| `output_format` | `--pyrefly-output-format` | Override Pyrefly's output format: `min-text`, `full-text`, `json`, `github`, `junit-xml`, `omit-errors`. |
| `min_severity` | `--pyrefly-min-severity` | Only show errors at/above this severity (`ignore`/`info`/`warn`/`error`). |
| `only` | `--pyrefly-only` | Only report these error kinds (e.g. `bad-assignment`); handy for triage. |
| `config` | `--pyrefly-config` | Path to a `pyrefly.toml` / `pyproject.toml` (disables discovery). |
| `config_discovery` | `--[no-]pyrefly-config-discovery` | Auto-discover `pyrefly.toml` / `[tool.pyrefly]`. |
| `baseline` | `--pyrefly-baseline` | Path to a Pyrefly baseline JSON; `check` then reports only errors *new* since the baseline. Generate it with `pants pyrefly-update-baseline`. |
| `exclude_source_roots` | `--pyrefly-exclude-source-roots` (advanced) | Source roots to omit from `--search-path`. Rarely needed — nested roots are deduped automatically (see below); use this only to force-drop a root the automatic logic keeps. |
| `version` / `known_versions` / `url_template` | (advanced) | Pin or override the downloaded Pyrefly binary. Any [supported Pyrefly version](#supported-pyrefly-versions) needs only `version`. |

Opt a target out of Pyrefly:

```python
python_sources(skip_pyrefly=True)
```

## Incremental adoption (baseline)

Adopting Pyrefly on a codebase that already has type errors? Record them in a baseline so `check`
only fails on *new* errors:

```bash
pants pyrefly-update-baseline ::   # writes the file named by [pyrefly].baseline
pants check ::                     # now reports only errors introduced since the baseline
```

Configure the path (and commit the baseline file):

```toml
[pyrefly]
baseline = "build-support/pyrefly-baseline.json"
```

Re-run `pants pyrefly-update-baseline` after fixing errors, or to refresh it. Baseline matching is
Pyrefly's own (lenient by design, so it survives code churn).

**Prefer inline suppressions?** `pants pyrefly-suppress ::` instead rewrites the targeted files in
place, adding `# pyrefly: ignore` on each current error (Pyrefly's `suppress`); delete them as you
fix, or run `pants pyrefly-suppress --pyrefly-suppress-remove-unused ::` to strip stale ones. An
external baseline (JSON) and inline suppressions are two strategies for the same goal — pick one.

## Migrating from MyPy

Moving a Pants repo off MyPy? See **[docs/migrating-from-mypy.md](docs/migrating-from-mypy.md)** —
config conversion (`pyrefly init --migrate-from mypy`), running both checkers during the transition,
baseline-based incremental adoption, and the MyPy-plugin gap (SQLAlchemy et al.).

## Editor / IDE (LSP)

Pyrefly ships an LSP server, but in a Pants repo your editor doesn't know the source roots. Generate
a `pyrefly.toml` with them:

```bash
pants pyrefly-lsp-config        # writes search-path (= your source roots) + python-version
```

For third-party imports, point your editor's interpreter at a venv (e.g.
`pants export --resolve=python-default`). If your Pyrefly config lives in `pyproject.toml`
`[tool.pyrefly]`, the goal prints the keys to add instead of writing a shadowing `pyrefly.toml`.

## Type coverage

Track typing progress — useful as a migration ratchet:

```bash
pants pyrefly-coverage ::                                    # prints overall % typed
pants pyrefly-coverage --pyrefly-coverage-fail-under=80 ::   # also fails if below 80%
```

## How import resolution works

- **First-party code:** your source roots are passed to Pyrefly via `--search-path` (the analogue of
  `MYPYPATH` / `sys.path`). Pants gives every file exactly one source root, but Pyrefly makes a file
  importable under *every* search path that physically contains it — so when source roots nest (the
  common case: the build root `.` above `src/python`), a file gets two module identities (`pkg.mod`
  and `src.python.pkg.mod`) and Pyrefly reports spurious errors where one flows into the other.

  For `check` and `pyrefly-suppress`, the plugin removes the nesting structurally: each source root's
  files are re-staged in the sandbox under its own sibling directory (`__pyrefly_root_<n>`) with the
  root prefix stripped, and each of those is passed as a single `--search-path` alongside
  `--disable-search-path-heuristics`. Sibling directories can't nest, so every file is reachable
  under exactly one module identity no matter how `root_patterns` overlap. Pyrefly's synthetic paths
  are mapped back to real repo paths in diagnostics, baseline files, and `suppress` edits, so this is
  invisible in output.

  The diagnostic goals (`pyrefly-coverage`, `pyrefly-dump-config`, `pyrefly-lsp-config`) don't
  re-stage — they pass your real source roots, deduplicated to each file's *nearest* root. If
  first-party code genuinely roots at both an ancestor and a nested root, both are kept and the
  plugin warns; `[pyrefly].exclude_source_roots` force-drops one.
- **Third-party deps:** Pants materializes the target's resolved requirements into a venv and points
  Pyrefly's `--python-interpreter-path` at it, so Pyrefly discovers `site-packages` and the target
  Python version exactly as `import` would at runtime.

## Diagnostics

When Pyrefly resolves imports or the interpreter differently than you expect, dump the effective
configuration Pants assembles — the first-party `search-path`s, the interpreter used for third-party
resolution, and the config file in effect:

```bash
pants pyrefly-dump-config ::                    # whole repo
pants pyrefly-dump-config src/project::         # a subtree
```

This runs Pyrefly's `dump-config` subcommand with exactly the arguments Pants passes to `check`, so
what you see is what `pants check` sees. It does not type-check. When targets span multiple resolves
or interpreter constraints, each partition's config is printed under its own heading.

## Pants compatibility

| Plugin version | Pants | Pyrefly (default) |
| --- | --- | --- |
| `1.0.0` | `2.27`–`2.33` | `1.2.0` |
| `0.5.0` | `2.27`–`2.32` | `1.1.1` |
| `0.4.0` | `2.27`–`2.32` | `1.1.1` |
| `0.3.0` | `2.27`–`2.32` | `1.1.1` |
| `0.2.0` | `2.27`–`2.32` | `1.1.1` |
| `0.1.0` | `2.27`–`2.32` | `1.1.1` |

The plugin supports both the legacy (`Get`/`MultiGet`) and modern (call-by-name) rules APIs through
a small version-conditional import (the rules API changed at Pants 2.30, and again removed `Get`
by 2.32). CI smoke-tests consumption on 2.27, 2.31, 2.32, and 2.33; in-between versions use the
same modern API.

## Supported Pyrefly versions

The plugin ships checksums for a set of stable Pyrefly releases, from **1.1.1** (the plugin's
first default) upward. Every version listed in its `default_known_versions` is pinned and tested,
and any of them can be selected with `version` alone:

```toml
[pyrefly]
version = "1.2.0"
```

Pre-releases (`X.Y.Z-dev.N`) are never pinned; to run one, set `known_versions` yourself. The policy:

- New Pyrefly releases are considered as they come out, including backports to older lines.
- A pinned version is removed only deliberately, with the reason recorded in the plugin's
  `DENYLISTED_VERSIONS` (in `subsystems.py`) and a CHANGELOG note.
- Versions older than 1.1.1 are added on request: [open an issue](https://github.com/tague/pants-pyrefly/issues).

Selecting a denylisted version fails with the recorded reason rather than a generic
`UnknownVersion`, and suggests the plugin's default:

```
DenylistedPyreflyVersion: Pyrefly 1.2.1 is not supported by pants-pyrefly: <reason>. Set [pyrefly].version to a supported release (1.3.2).
```

To use a denylisted version anyway, supply your own `[pyrefly].known_versions` entry for it on
your platform. That is a deliberate choice, and the plugin does not block it.

Every supported version is exercised by the [compatibility suite](#pyrefly-compatibility-suite)
before each release.

## Stability

From 1.0.0 on, this project follows [Semantic Versioning](https://semver.org/). Covered by the
compatibility promise — a breaking change to any of these requires a major bump:

- The goal names (`pyrefly-init`, `pyrefly-lsp-config`, `pyrefly-coverage`, `pyrefly-suppress`,
  `pyrefly-update-baseline`, `pyrefly-dump-config`) and Pyrefly's participation in `check`.
- The `[pyrefly]` option names documented under [Configuration](#configuration), and the
  `skip_pyrefly` field.
- The backend name `pants_pyrefly`, and the published wheel carrying no `pantsbuild.pants`
  dependency.

Not covered: the plugin's Python API (every module is an implementation detail — import nothing
from `pants_pyrefly` directly), the default pinned Pyrefly version, the exact wording and layout of
Pyrefly's own diagnostic output, the contents and format of the baseline file (Pyrefly writes it,
and its format can change between Pyrefly releases), and the sandbox staging mechanics described under
[How import resolution works](#how-import-resolution-works). Dropping a Pants version that has
reached end of life is a minor bump, not a major one.

## Development

This repo dogfoods its own tooling: `ruff` (lint + format) and Pyrefly itself (`check`) run on the
plugin's sources.

```bash
pants generate-lockfiles          # pants-plugins + python-default resolves
pants fmt lint ::                 # ruff format + check
pants check ::                    # Pyrefly type-checks the plugin (dogfood) + testprojects/
pants test ::                     # run the integration tests
pants package pants-plugins/pants_pyrefly:dist   # build the wheel + sdist into dist/
```

### Bumping the pinned Pyrefly version

The `default_known_versions` pins in `subsystems.py` (`<version>|<platform>|<sha256>|<size>`) are
managed by `build-support/bin/generate_known_versions.py`, not hand-edited. They are ordered newest
version first, platforms in a fixed order. The script reads everything it needs from
`subsystems.py`: `MINIMUM_PINNED_VERSION` and `DENYLISTED_VERSIONS` (module-level constants, the
single source of truth for each), plus the default, URL template, and platform mapping.

```bash
GEN=build-support/bin/generate_known_versions.py
python3 $GEN --check-upstream                 # stable releases not yet pinned (or denylisted)?
python3 $GEN --version <new> --write          # move the default and add the missing pins
python3 $GEN --write                          # add missing pins (e.g. a backport) only
python3 $GEN --check                          # verify the shipped pins (what CI runs)
python3 $GEN --remove <ver> --reason "<why>"  # drop a version's pins and denylist it
python3 $GEN --list-versions                  # supported versions as JSON (the compat matrix)
```

- `--write` only adds. It inserts pins for every stable release in `[minimum, default]` that isn't
  already pinned or denylisted, and edits `default_version`. It never removes or rewrites an
  existing pin: lowering the default keeps the higher pins, and a pin that disagrees with its
  release is reported (exit 1), not overwritten.
- `--check` verifies only what ships. Each pin's sha256 and size must match its release, the
  default must be pinned, the order must be canonical, nothing may be older than the minimum, and
  no denylisted version may be pinned or be the default. An unpinned upstream release, whether
  newer than the default or an in-range backport, never fails `--check`, so a new Pyrefly release
  never turns unrelated CI red.
- `--check-upstream` lists every stable release at or above the minimum that is neither pinned
  nor denylisted, and exits 1 if there are any.
- `--remove` is the only removal path: it deletes the version's pins (if any) and records the
  reason in `DENYLISTED_VERSIONS`, so `--write` never re-adds it and `--check-upstream` stops
  reporting it. It also works for a never-pinned release you want to skip. It refuses the
  default, and it refuses the minimum: raise `MINIMUM_PINNED_VERSION` deliberately instead. The
  reason must be a single line; it is stored exactly as typed, in the quote style `ruff format`
  prefers (long reasons are split across adjacent string literals), so no `pants fmt` is needed.
- `--write` and `--remove` edit `subsystems.py` line by line: they change only the
  `default_version` literal, insert or delete pin lines, and add a denylist entry. Every other line,
  including comments and quoting, is left byte-identical.

If Pyrefly withdraws a pinned release (deletes it, or re-labels it as a pre-release), `--check`
fails, and the fix is `--remove <version> --reason "..."`.

The script fetches each asset's published `.sha256` sidecar (rejecting anything that isn't a
64-character hex digest) and size from the GitHub release, retrying transient failures up to twice.
Set `GITHUB_TOKEN` to avoid API rate limits; the token is only sent to `api.github.com`.

### Pyrefly compatibility suite

`build-support/ci/compat_test.sh <version>` builds a throwaway project that loads the plugin from
source and drives real Pants runs with only `--pyrefly-version=<version>` set, so the shipped pins
resolve the download. It checks that a clean file passes, that a missing import fails with
`missing-import`, that `pyrefly-update-baseline` followed by a gated `check` passes, and that
`pyrefly-suppress` followed by `check` passes. For every Pyrefly process it asserts, from the
binary in that process's preserved sandbox, that `<version>` is what actually ran. Each Pants run
uses an execution root inside the script's own work directory, so the script leaves nothing behind
in `$TMPDIR`.

The [compatibility workflow](.github/workflows/compat.yml) runs the script for every supported
version (`--list-versions`), one job each. It runs on demand and as a required step of the release
workflow, so if any supported version fails, nothing is published. It does not run on pull
requests.

## Releasing

Push a `vX.Y.Z` tag. The [release workflow](.github/workflows/release.yml) first runs the
[Pyrefly compatibility suite](#pyrefly-compatibility-suite) for every supported version, then
builds the wheel and publishes it to PyPI using [Trusted Publishing](https://docs.pypi.org/trusted-publishers/) (OIDC,
no API tokens). Configure a PyPI trusted publisher for this repo + the `release.yml` workflow first.

## License

[Apache-2.0](LICENSE).
