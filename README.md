# pants-pyrefly

A [Pants](https://www.pantsbuild.org/) plugin that runs [Pyrefly](https://pyrefly.org/) —
Meta's fast, Rust-based Python type checker — as part of the Pants `check` goal.

Pants downloads the official prebuilt Pyrefly binary (pinned by SHA256) and runs it hermetically
in a sandbox, wiring up your first-party source roots and the resolved third-party dependencies so
that imports resolve correctly.

## Requirements

- **Pants 2.32.x** — this release targets the Pants 2.32 line (which runs on CPython 3.14).
  See the [compatibility table](#pants-compatibility) for other versions.

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
| `0.1.0` | `2.32.x` | `1.1.1` |

A plugin release targets a single Pants minor version, because the Pants plugin API is not stable
across minor versions.

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
