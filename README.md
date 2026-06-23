# pants-pyrefly

A [Pants](https://www.pantsbuild.org/) plugin that runs [Pyrefly](https://pyrefly.org/) —
Meta's fast, Rust-based Python type checker — as part of the Pants `check` goal.

Pants downloads the official prebuilt Pyrefly binary (pinned by SHA256) and runs it hermetically
in a sandbox, wiring up your first-party source roots and the resolved third-party dependencies so
that imports resolve correctly.

## Requirements

- **Pants 2.27–2.32.** A single codebase supports both the legacy (`Get`/`MultiGet`-era) and modern
  (call-by-name) rules APIs via a small version-conditional import; verified on 2.27 and 2.32.
- The **published wheel** targets the 2.32 line (CPython 3.14). On **Pants 2.27** (e.g. a repo not
  yet upgraded), install the plugin **from source** — see [Installation](#installation).

## Installation

Add the plugin and enable its backend in `pants.toml`:

```toml
[GLOBAL]
plugins = ["pants-pyrefly==0.1.0"]
backend_packages.add = [
    "pants.backend.python",
    "pants_pyrefly",
]
```

### From source (in-repo) — works on Pants 2.27+

For an existing repo (e.g. on Pants 2.27), consume the plugin from source instead of a wheel — the
same way in-repo plugins are normally loaded. Copy `pants-plugins/pants_pyrefly/` into your repo and:

```toml
[GLOBAL]
pythonpath = ["%(buildroot)s/pants-plugins"]
backend_packages.add = ["pants.backend.python", "pants_pyrefly"]
```

If you keep plugin code in a dedicated `pants-plugins` resolve, add it there and run
`pants generate-lockfiles`.

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
| `config` | `--pyrefly-config` | Path to a `pyrefly.toml` / `pyproject.toml` (disables discovery). |
| `config_discovery` | `--[no-]pyrefly-config-discovery` | Auto-discover `pyrefly.toml` / `[tool.pyrefly]`. |
| `version` / `known_versions` / `url_template` | (advanced) | Pin or override the downloaded Pyrefly binary. |

Opt a target out of Pyrefly:

```python
python_sources(skip_pyrefly=True)
```

## How import resolution works

- **First-party code:** every source root is passed to Pyrefly via `--search-path` (the analogue of
  `MYPYPATH` / `sys.path`).
- **Third-party deps:** Pants materializes the target's resolved requirements into a venv and points
  Pyrefly's `--python-interpreter-path` at it, so Pyrefly discovers `site-packages` and the target
  Python version exactly as `import` would at runtime.

## Pants compatibility

| Plugin version | Pants | Pyrefly (default) |
| --- | --- | --- |
| `0.1.0` | `2.27`–`2.32` | `1.1.1` |

The plugin supports both the legacy (`Get`/`MultiGet`) and modern (call-by-name) rules APIs through
a small version-conditional import (the rules API changed at Pants 2.30, and again removed `Get`
by 2.32). Verified on 2.27 and 2.32; in-between versions use the same modern API.

## Development

```bash
pants generate-lockfiles          # pants-plugins + python-default resolves
pants check ::                    # run the plugin against testprojects/
pants test ::                     # run the integration tests
pants package pants-plugins/pants_pyrefly:dist   # build the wheel + sdist into dist/
```

## Releasing

Push a `vX.Y.Z` tag. The [release workflow](.github/workflows/release.yml) builds the wheel and
publishes it to PyPI using [Trusted Publishing](https://docs.pypi.org/trusted-publishers/) (OIDC,
no API tokens). Configure a PyPI trusted publisher for this repo + the `release.yml` workflow first.

## License

[Apache-2.0](LICENSE).
