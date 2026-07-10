# Migrating from MyPy to Pyrefly

`pants-pyrefly` runs [Pyrefly](https://pyrefly.org/) as a checker in the Pants `check` goal. This
guide walks a Pants repo from MyPy (`pants.backend.python.typecheck.mypy`) to Pyrefly with minimal
disruption.

## 1. Install the plugin (alongside MyPy at first)

See [Installation](../README.md#installation). Keep MyPy enabled during the transition so you can
run both and compare:

```toml
[GLOBAL]
backend_packages.add = [
    "pants.backend.python",
    "pants.backend.python.typecheck.mypy",   # keep during transition
    "pants_pyrefly",
]
```

`pants check ::` now runs both checkers. Scope to one with `--only`:

```bash
pants --only=pyrefly check ::     # just Pyrefly
pants --only=mypy    check ::     # just MyPy
```

## 2. Convert your MyPy config

Pyrefly can import an existing MyPy (or Pyright) configuration:

```bash
pyrefly init --migrate-from mypy
```

This generates a `pyrefly.toml` (or a `pyproject.toml [tool.pyrefly]` table) from your `[mypy]` /
`mypy.ini` settings. Review the result — not everything maps 1:1:

- `ignore_missing_imports`, per-module overrides, and strictness flags migrate.
- **MyPy *plugins* do not** — Pyrefly has no plugin system (see the gap note below).

The plugin auto-discovers `pyrefly.toml` or `pyproject.toml [tool.pyrefly]`; point it at a
non-standard path with `[pyrefly].config`.

## 3. Adopt incrementally with a baseline

A large codebase will have pre-existing errors under a stricter preset. Record them so `check`
only fails on *new* errors:

```bash
pants pyrefly-update-baseline ::   # writes the file named by [pyrefly].baseline
pants check ::                     # reports only errors introduced since the baseline
```

See [Incremental adoption (baseline)](../README.md#incremental-adoption-baseline). Commit the
baseline file, and re-run `pyrefly-update-baseline` as you fix errors to ratchet it down.

## 4. Track progress with coverage

```bash
pants pyrefly-coverage ::                                    # overall % of typable symbols typed
pants pyrefly-coverage --pyrefly-coverage-fail-under=80 ::   # gate in CI
```

## 5. Wire up the editor / LSP

```bash
pants pyrefly-lsp-config    # writes source roots into pyrefly.toml (or prints [tool.pyrefly] keys)
```

Point your editor's interpreter at an exported venv (`pants export --resolve=<name>`) so the LSP
resolves third-party imports the way Pants does.

## Known gap: MyPy plugins (e.g. SQLAlchemy)

MyPy's plugin API — used by `sqlalchemy.ext.mypy.plugin`, Pydantic v1, Django stubs, and others —
has **no Pyrefly equivalent**. ORM declarative-attribute magic that a MyPy plugin resolves
dynamically will surface as errors under Pyrefly, and static stub packages (e.g.
`sqlalchemy2-stubs`) do **not** fully bridge the gap. Your options for those packages:

- suppress them (per-path config, inline `# type: ignore`, or fold them into the baseline), or
- keep MyPy enabled for just those packages during the transition.

## Troubleshooting import resolution

- **False "missing import" for a first-party module** → it isn't reachable from a source root.
  `pants roots` lists your roots; the plugin passes each to Pyrefly as `--search-path`.
- **False "missing import" for a third-party package** → the requirement isn't in the target's
  resolve, or it ships no types. Add it as a dependency; add *stub-only* packages via
  `[pyrefly].extra_type_stubs` (e.g. `types-requests`).
- **A whole partition fails with "could not find a compatible interpreter"** → no interpreter
  matching that target's `interpreter_constraints` is installed. Install it (or narrow the
  constraints).
