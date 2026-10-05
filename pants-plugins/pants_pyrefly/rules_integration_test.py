# Copyright 2026 Tague Griffith
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest  # pants: no-infer-dep
import toml  # pants: no-infer-dep

from pants_pyrefly.goals import (
    PyreflyCoverage,
    PyreflyDumpConfig,
    PyreflyInit,
    PyreflyLspConfig,
    PyreflySuppress,
    PyreflyUpdateBaseline,
)
from pants_pyrefly.register import rules as pyrefly_register_rules
from pants_pyrefly.rules import (
    PyreflyFieldSet,
    PyreflyRequest,
    _dedupe_search_path_roots,
    _filter_baseline_entries,
    _has_flag,
    _plan_restage,
    _real_prefix,
    _remap_path,
    _remap_text,
)
from pants_pyrefly import subsystems
from pants_pyrefly.subsystems import Pyrefly

from pants.backend.python import target_types_rules
from pants.backend.python.dependency_inference import rules as dependency_inference_rules
from pants.backend.python.target_types import (
    PythonRequirementTarget,
    PythonSourcesGeneratorTarget,
    PythonSourceTarget,
)
from pants.backend.python.util_rules import pex, pex_environment, pex_from_targets
from pants.core.goals.check import CheckResult, CheckResults
from pants.core.util_rules import config_files, external_tool, source_files
from pants.engine.addresses import Address
from pants.engine.internals.scheduler import ExecutionError
from pants.engine.rules import QueryRule
from pants.engine.target import Target
from pants.testutil.python_rule_runner import PythonRuleRunner
from pants.util.frozendict import FrozenDict

# Inherited so Pants can discover system interpreters and download the Pyrefly binary.
_ENV_INHERIT = {"PATH", "PYENV_ROOT", "HOME"}


def _remove_tree(path: Path) -> None:
    """Remove `path`, including the read-only directories Pants makes for immutable inputs.

    Pants also deletes finished sandboxes asynchronously, so entries may vanish mid-walk.
    """

    def on_error(function, failed_path, error: BaseException) -> None:
        if isinstance(error, FileNotFoundError):
            return
        if isinstance(error, PermissionError):
            parent = os.path.dirname(failed_path)
            os.chmod(parent, stat.S_IRWXU)
            if os.path.isdir(failed_path) and not os.path.islink(failed_path):
                os.chmod(failed_path, stat.S_IRWXU)
                shutil.rmtree(failed_path, onexc=on_error)
            else:
                function(failed_path)
            return
        raise error

    shutil.rmtree(path, onexc=on_error)


@pytest.fixture
def rule_runner(tmp_path: Path) -> Iterator[PythonRuleRunner]:
    # The test sandbox gets no TMPDIR, so the inner Pants would default its execution root to
    # /tmp and leave its read-only `immutable_inputs*` directories (the Pyrefly binary, ~20MB
    # each) there. Keep everything it executes under pytest's tmp_path, and remove it afterwards.
    exec_root = tmp_path / "pants-exec-root"
    exec_root.mkdir()
    yield PythonRuleRunner(
        bootstrap_args=[f"--local-execution-root-dir={exec_root}"],
        rules=[
            *pyrefly_register_rules(),
            *target_types_rules.rules(),
            *dependency_inference_rules.rules(),
            *pex.rules(),
            *pex_environment.rules(),
            *pex_from_targets.rules(),
            *external_tool.rules(),
            *config_files.rules(),
            *source_files.rules(),
            QueryRule(CheckResults, (PyreflyRequest,)),
        ],
        target_types=[
            PythonSourcesGeneratorTarget,
            PythonSourceTarget,
            PythonRequirementTarget,
        ],
    )
    _remove_tree(exec_root)


def run_pyrefly(
    rule_runner: PythonRuleRunner,
    targets: list[Target],
    *,
    extra_args: list[str] | None = None,
) -> tuple[CheckResult, ...]:
    rule_runner.set_options(extra_args or (), env_inherit=_ENV_INHERIT)
    field_sets = tuple(PyreflyFieldSet.create(tgt) for tgt in targets)
    checks = rule_runner.request(CheckResults, [PyreflyRequest(field_sets)])
    return checks.results


# ---
# Unit tests for `_dedupe_search_path_roots` (pure, no rule runner needed).
# ---


def test_dedupe_drops_redundant_ancestor_root() -> None:
    # `src` is an ancestor of `src/python` and no file resolves to it directly, so it is dropped.
    result = _dedupe_search_path_roots(
        source_roots=("src", "src/python"),
        source_files=("src/python/pkg/mod.py", "src/python/pkg/other.py"),
    )
    assert result == ("src/python",)


def test_dedupe_keeps_ancestor_a_file_needs_and_warns(caplog: pytest.LogCaptureFixture) -> None:
    # A file lives directly under `src` (nothing more specific covers it), so `src` must stay —
    # but it still shadows `src/python`, which we surface as a warning rather than hide code.
    with caplog.at_level(logging.WARNING):
        result = _dedupe_search_path_roots(
            source_roots=("src", "src/python"),
            source_files=("src/extra.py", "src/python/pkg/mod.py"),
        )
    assert result == ("src", "src/python")
    assert any("is an ancestor of" in record.message for record in caplog.records)


def test_dedupe_exclude_forces_a_root_out() -> None:
    # `exclude` drops `src` even though a file resolves to it (the user's explicit choice); the
    # `src/extra.py` file is simply left without a covering root.
    result = _dedupe_search_path_roots(
        source_roots=("src", "src/python"),
        source_files=("src/extra.py", "src/python/pkg/mod.py"),
        exclude=("src",),
    )
    assert result == ("src/python",)


def test_dedupe_keeps_sibling_roots() -> None:
    # Neither root is an ancestor of the other, so both are kept and there is no warning.
    result = _dedupe_search_path_roots(
        source_roots=("src/python", "test/python"),
        source_files=("src/python/a.py", "test/python/b.py"),
    )
    assert result == ("src/python", "test/python")


def test_dedupe_drops_buildroot_when_covered() -> None:
    # The build-root source root (".") is an ancestor of everything; drop it when a more specific
    # root already covers every file.
    result = _dedupe_search_path_roots(
        source_roots=(".", "src/python"),
        source_files=("src/python/pkg/mod.py",),
    )
    assert result == ("src/python",)


def test_dedupe_prefix_lookalike_is_not_an_ancestor() -> None:
    # `src` must not be treated as an ancestor of `srcfoo` (segment-aware matching).
    result = _dedupe_search_path_roots(
        source_roots=("src", "srcfoo"),
        source_files=("src/a.py", "srcfoo/b.py"),
    )
    assert result == ("src", "srcfoo")


# ---
# Unit tests for the re-staging planner + path remap (pure, no rule runner needed).
# ---


def test_plan_restage_one_nonnesting_root_per_file() -> None:
    # Each file lands under exactly one synthetic sibling dir; the build root ("." ) keeps the full
    # path (module `scripts.x`), a nested root has its prefix stripped (module `pkg.m`).
    root_of = {"src/python/pkg/m.py": "src/python", "scripts/x.py": "."}
    root_to_synth, real_to_synth, search_paths = _plan_restage(list(root_of), root_of)
    assert not any(a != b and b.startswith(a + "/") for a in search_paths for b in search_paths)
    assert real_to_synth["scripts/x.py"] == f"{root_to_synth['.']}/scripts/x.py"
    assert real_to_synth["src/python/pkg/m.py"] == f"{root_to_synth['src/python']}/pkg/m.py"


def test_plan_restage_deterministic_and_collision_free() -> None:
    # Mirrored relpaths under two roots must not collide, and naming is a deterministic sort index.
    root_of = {"src/python/config/s.py": "src/python", "test/python/config/s.py": "test/python"}
    root_to_synth, real_to_synth, _ = _plan_restage(list(root_of), root_of)
    assert len(set(real_to_synth.values())) == 2
    assert root_to_synth == {"src/python": "__pyrefly_root_0", "test/python": "__pyrefly_root_1"}


def test_remap_roundtrips_synthetic_paths_back_to_real() -> None:
    m = FrozenDict({"__pyrefly_root_0": ".", "__pyrefly_root_1": "src/python"})
    assert _real_prefix(".") == ""
    assert _real_prefix("src/python") == "src/python/"
    assert _remap_path("__pyrefly_root_1/pkg/m.py", m) == "src/python/pkg/m.py"
    assert (
        _remap_path("__pyrefly_root_0/scripts/x.py", m) == "scripts/x.py"
    )  # build root: no prefix
    assert _remap_path("outside/p.py", m) == "outside/p.py"  # unmatched left as-is
    assert _remap_text("--> __pyrefly_root_1/pkg/m.py:5:1", m) == "--> src/python/pkg/m.py:5:1"


def test_remap_text_rewrites_bare_roots_and_spares_lookalikes() -> None:
    m = FrozenDict(
        {"__pyrefly_root_0": ".", "__pyrefly_root_1": "lib", "__pyrefly_root_2": "src/python"}
    )
    # A missing-import hint names each root on its own, as the last segment of an absolute
    # sandbox path (Pants strips the sandbox prefix afterwards). The build root becomes `.`.
    hint = (
        'override: ["/x/pants-sandbox-a/__pyrefly_root_0", "/x/pants-sandbox-a/__pyrefly_root_2"]'
    )
    assert (
        _remap_text(hint, m)
        == 'override: ["/x/pants-sandbox-a/.", "/x/pants-sandbox-a/src/python"]'
    )
    assert _remap_text('["__pyrefly_root_1"]', m) == '["lib"]'
    # JSON-escaped and end-of-text occurrences.
    assert _remap_text('[\\"__pyrefly_root_1\\"]', m) == '[\\"lib\\"]'
    assert _remap_text("in __pyrefly_root_2", m) == "in src/python"
    # Paths under a root, in the shapes Pyrefly's output formats use.
    assert (
        _remap_text("file=__pyrefly_root_2/app/f.py,line=1", m) == "file=src/python/app/f.py,line=1"
    )
    assert _remap_text('"path": "__pyrefly_root_0/scripts/x.py"', m) == '"path": "scripts/x.py"'
    # Lookalikes are left alone: names that were not generated for this invocation, and generated
    # names that are only part of a longer word.
    for untouched in (
        "Literal['__pyrefly_root_7']",
        "__pyrefly_root_10/pkg/m.py",
        "my__pyrefly_root_1",
        "__pyrefly_root_1x",
        "__pyrefly_root_1_cfg",
        "__pyrefly_root_1-old",
        "a.__pyrefly_root_1",
    ):
        assert _remap_text(untouched, m) == untouched
    # Nothing re-staged: the text is returned unchanged.
    assert _remap_text("__pyrefly_root_0/f.py", FrozenDict({})) == "__pyrefly_root_0/f.py"


def test_passing(rule_runner: PythonRuleRunner) -> None:
    rule_runner.write_files(
        {
            "src/project/f.py": "def add(x: int, y: int) -> int:\n    return x + y\n",
            "src/project/BUILD": "python_sources()",
        }
    )
    tgt = rule_runner.get_target(Address("src/project", relative_file_path="f.py"))
    result = run_pyrefly(rule_runner, [tgt])
    assert len(result) == 1
    assert result[0].exit_code == 0
    assert result[0].partition_description is not None


def test_failing(rule_runner: PythonRuleRunner) -> None:
    # An unresolvable import is reported even under Pyrefly's default `basic` preset.
    rule_runner.write_files(
        {
            "src/project/f.py": "import a_module_that_truly_does_not_exist_pyrefly\n",
            "src/project/BUILD": "python_sources()",
        }
    )
    tgt = rule_runner.get_target(Address("src/project", relative_file_path="f.py"))
    result = run_pyrefly(rule_runner, [tgt])
    assert len(result) == 1
    assert result[0].exit_code == 1
    combined = result[0].stdout + result[0].stderr
    assert "f.py" in combined


def test_skip_via_subsystem(rule_runner: PythonRuleRunner) -> None:
    rule_runner.write_files(
        {
            "src/project/f.py": "import a_module_that_truly_does_not_exist_pyrefly\n",
            "src/project/BUILD": "python_sources()",
        }
    )
    tgt = rule_runner.get_target(Address("src/project", relative_file_path="f.py"))
    result = run_pyrefly(rule_runner, [tgt], extra_args=["--pyrefly-skip"])
    assert not result


def test_skip_field_opts_out(rule_runner: PythonRuleRunner) -> None:
    rule_runner.write_files(
        {
            "src/project/f.py": "x = 1\n",
            "src/project/BUILD": "python_sources(skip_pyrefly=True)",
        }
    )
    tgt = rule_runner.get_target(Address("src/project", relative_file_path="f.py"))
    assert PyreflyFieldSet.opt_out(tgt) is True


def test_third_party_import_resolves(rule_runner: PythonRuleRunner) -> None:
    # If Pyrefly could not see the resolved third-party requirements, it would report a
    # missing-import error for `typing_extensions` and fail.
    rule_runner.write_files(
        {
            "src/project/f.py": (
                "from typing_extensions import assert_type\n"
                "\n"
                "def double(x: int) -> int:\n"
                "    return x * 2\n"
                "\n"
                "assert_type(double(21), int)\n"
            ),
            "src/project/BUILD": "python_sources()",
            "BUILD": (
                "python_requirement(name='typing-extensions', "
                "requirements=['typing-extensions>=4.0'])"
            ),
        }
    )
    tgt = rule_runner.get_target(Address("src/project", relative_file_path="f.py"))
    result = run_pyrefly(rule_runner, [tgt])
    assert len(result) == 1
    assert result[0].exit_code == 0


def test_extra_type_stubs(rule_runner: PythonRuleRunner) -> None:
    # `extra_type_stubs` resolves stub-only packages and merges them into the environment Pyrefly
    # inspects, without them becoming runtime dependencies. Assert the option is wired end to end:
    # the stub requirement resolves, the venv is built, and the run succeeds. (We don't assert a
    # missing-import contrast, because Pyrefly bundles typeshed's third-party stubs for many common
    # packages — e.g. PyYAML resolves even with no stubs provided.)
    rule_runner.write_files(
        {
            "src/project/f.py": (
                "import yaml  # pants: no-infer-dep\n\nvalue = yaml.safe_load('a: 1')\n"
            ),
            "src/project/BUILD": "python_sources()",
        }
    )
    tgt = rule_runner.get_target(Address("src/project", relative_file_path="f.py"))
    result = run_pyrefly(rule_runner, [tgt], extra_args=["--pyrefly-extra-type-stubs=types-PyYAML"])
    assert len(result) == 1
    assert result[0].exit_code == 0


def test_config_discovery(rule_runner: PythonRuleRunner) -> None:
    rule_runner.write_files(
        {
            "src/project/f.py": 'x: int = "not an int"\n',
            "src/project/BUILD": "python_sources()",
        }
    )
    tgt = rule_runner.get_target(Address("src/project", relative_file_path="f.py"))
    # The default `basic` preset does not flag this assignment.
    assert run_pyrefly(rule_runner, [tgt])[0].exit_code == 0
    # A discovered `pyrefly.toml` that raises strictness does.
    rule_runner.write_files({"pyrefly.toml": 'preset = "legacy"\n'})
    assert run_pyrefly(rule_runner, [tgt])[0].exit_code == 1


def test_explicit_config_option(rule_runner: PythonRuleRunner) -> None:
    # A config in a non-standard location is only honored if `[pyrefly].config` is passed through
    # to Pyrefly as `--config` (the bug this guards against).
    rule_runner.write_files(
        {
            "src/project/f.py": 'x: int = "not an int"\n',
            "src/project/BUILD": "python_sources()",
            "build-support/pyrefly.toml": 'preset = "legacy"\n',
        }
    )
    tgt = rule_runner.get_target(Address("src/project", relative_file_path="f.py"))
    result = run_pyrefly(
        rule_runner, [tgt], extra_args=["--pyrefly-config=build-support/pyrefly.toml"]
    )
    assert result[0].exit_code == 1


def test_args_passthrough(rule_runner: PythonRuleRunner) -> None:
    rule_runner.write_files(
        {
            "src/project/f.py": "import totally_fake_xyz_123  # pants: no-infer-dep\n",
            "src/project/BUILD": "python_sources()",
        }
    )
    tgt = rule_runner.get_target(Address("src/project", relative_file_path="f.py"))
    # The missing import fails by default...
    assert run_pyrefly(rule_runner, [tgt])[0].exit_code == 1
    # ...but a forwarded Pyrefly arg suppresses it.
    result = run_pyrefly(
        rule_runner, [tgt], extra_args=["--pyrefly-args=--ignore-missing-imports=*"]
    )
    assert result[0].exit_code == 0


def test_baseline_gating(rule_runner: PythonRuleRunner) -> None:
    # A baseline that records the (only) error in f.py; `--baseline` should then report 0 new.
    baseline = json.dumps(
        {
            "errors": [
                {
                    "line": 1,
                    "column": 10,
                    "stop_line": 1,
                    "stop_column": 15,
                    "path": "src/project/f.py",
                    "code": -2,
                    "name": "bad-assignment",
                    "description": "`Literal['bad']` is not assignable to `int`",
                    "concise_description": "`Literal['bad']` is not assignable to `int`",
                    "severity": "error",
                }
            ]
        }
    )
    rule_runner.write_files(
        {
            "src/project/f.py": 'x: int = "bad"\n',
            "src/project/BUILD": "python_sources()",
            # The legacy preset is needed for a bad-assignment to be flagged at all.
            "pyrefly.toml": 'preset = "legacy"\n',
            "pyrefly-baseline.json": baseline,
        }
    )
    tgt = rule_runner.get_target(Address("src/project", relative_file_path="f.py"))
    # Without a baseline, the error is reported.
    assert run_pyrefly(rule_runner, [tgt])[0].exit_code == 1
    # With a baseline that covers it, the error is gated.
    gated = run_pyrefly(rule_runner, [tgt], extra_args=["--pyrefly-baseline=pyrefly-baseline.json"])
    assert gated[0].exit_code == 0


def test_update_baseline_roundtrip(rule_runner: PythonRuleRunner) -> None:
    rule_runner.write_files(
        {
            "src/project/f.py": 'x: int = "bad"\n',
            "src/project/BUILD": "python_sources()",
            "pyrefly.toml": 'preset = "legacy"\n',
        }
    )
    # 1) The goal generates the baseline file (recording the existing error).
    result = rule_runner.run_goal_rule(
        PyreflyUpdateBaseline,
        args=["--pyrefly-baseline=pyrefly-baseline.json", "src/project::"],
        env_inherit=_ENV_INHERIT,
    )
    assert result.exit_code == 0
    baseline_path = os.path.join(rule_runner.build_root, "pyrefly-baseline.json")
    assert os.path.exists(baseline_path)
    with open(baseline_path) as fh:
        assert len(json.load(fh)["errors"]) >= 1

    # 2) With that baseline, `check` reports 0 new errors.
    tgt = rule_runner.get_target(Address("src/project", relative_file_path="f.py"))
    gated = run_pyrefly(rule_runner, [tgt], extra_args=["--pyrefly-baseline=pyrefly-baseline.json"])
    assert gated[0].exit_code == 0


def test_init_creates_config(rule_runner: PythonRuleRunner) -> None:
    # A repo with a MyPy config and no Pyrefly config: `pyrefly-init` migrates it into pyrefly.toml.
    rule_runner.write_files({"mypy.ini": "[mypy]\nstrict = True\npython_version = 3.11\n"})
    result = rule_runner.run_goal_rule(
        PyreflyInit, args=["--pyrefly-init-migrate-from=mypy"], env_inherit=_ENV_INHERIT
    )
    assert result.exit_code == 0
    config_path = os.path.join(rule_runner.build_root, "pyrefly.toml")
    assert os.path.exists(config_path)
    with open(config_path) as fh:
        assert fh.read().strip()  # non-empty config was written


def test_init_refuses_existing_config(rule_runner: PythonRuleRunner) -> None:
    original = 'preset = "legacy"\n# hand-tuned\n'
    rule_runner.write_files({"pyrefly.toml": original})
    result = rule_runner.run_goal_rule(PyreflyInit, env_inherit=_ENV_INHERIT)
    assert result.exit_code == 1
    # The existing config must be left untouched.
    with open(os.path.join(rule_runner.build_root, "pyrefly.toml")) as fh:
        assert fh.read() == original


def test_lsp_config_writes_search_path(rule_runner: PythonRuleRunner) -> None:
    rule_runner.write_files(
        {
            "src/project/f.py": "x = 1\n",
            "src/project/BUILD": "python_sources()",
        }
    )
    result = rule_runner.run_goal_rule(PyreflyLspConfig, env_inherit=_ENV_INHERIT)
    assert result.exit_code == 0
    config_path = os.path.join(rule_runner.build_root, "pyrefly.toml")
    assert os.path.exists(config_path)
    with open(config_path) as fh:
        content = fh.read()
    assert "search-path" in content
    assert "python-version" in content


def test_lsp_config_dedupes_nested_source_roots(rule_runner: PythonRuleRunner) -> None:
    # With both `src` and `src/python` as source roots, a target whose BUILD lives at `src` but
    # whose files live under `src/python` makes Pants surface BOTH roots. Since no file actually
    # resolves to bare `src`, the written search-path must contain `src/python` but not `src`
    # (otherwise every module under `src/python` would be reachable under two names).
    rule_runner.write_files(
        {
            "src/python/pkg/mod.py": "x = 1\n",
            "src/python/pkg/BUILD": "python_sources()",
            "src/python/extra/thing.py": "y = 1\n",
            # BUILD at `src` (source root `src`) globbing a file that lives under `src/python`.
            "src/BUILD": "python_sources(sources=['python/extra/thing.py'])",
        }
    )
    result = rule_runner.run_goal_rule(
        PyreflyLspConfig,
        global_args=['--source-root-patterns=["/src", "/src/python"]'],
        env_inherit=_ENV_INHERIT,
    )
    assert result.exit_code == 0
    with open(os.path.join(rule_runner.build_root, "pyrefly.toml")) as fh:
        roots = toml.loads(fh.read())["search-path"]
    assert "src/python" in roots
    assert "src" not in roots


def test_lsp_config_exclude_source_roots(rule_runner: PythonRuleRunner) -> None:
    # Here a file genuinely lives directly under `src`, so auto-dedup keeps `src`;
    # `[pyrefly].exclude_source_roots` force-drops it anyway.
    rule_runner.write_files(
        {
            "src/toplevel.py": "a = 1\n",
            "src/BUILD": "python_sources()",
            "src/python/pkg/mod.py": "b = 1\n",
            "src/python/pkg/BUILD": "python_sources()",
        }
    )
    result = rule_runner.run_goal_rule(
        PyreflyLspConfig,
        global_args=['--source-root-patterns=["/src", "/src/python"]'],
        args=["--pyrefly-exclude-source-roots=['src']"],
        env_inherit=_ENV_INHERIT,
    )
    assert result.exit_code == 0
    with open(os.path.join(rule_runner.build_root, "pyrefly.toml")) as fh:
        roots = toml.loads(fh.read())["search-path"]
    assert "src/python" in roots
    assert "src" not in roots


def test_coverage_goal(rule_runner: PythonRuleRunner) -> None:
    rule_runner.write_files(
        {
            "src/project/f.py": (
                "def typed(x: int) -> int:\n    return x\n\ndef untyped(x):\n    return x\n"
            ),
            "src/project/BUILD": "python_sources()",
        }
    )
    reported = rule_runner.run_goal_rule(
        PyreflyCoverage, args=["src/project::"], env_inherit=_ENV_INHERIT
    )
    assert reported.exit_code == 0
    assert "coverage" in reported.stdout.lower()
    # The untyped function keeps coverage below 100%, so a 100% floor must fail.
    gated = rule_runner.run_goal_rule(
        PyreflyCoverage,
        args=["--pyrefly-coverage-fail-under=100", "src/project::"],
        env_inherit=_ENV_INHERIT,
    )
    assert gated.exit_code == 1


def test_dump_config_goal(rule_runner: PythonRuleRunner) -> None:
    rule_runner.write_files(
        {
            "src/project/f.py": "x: int = 1\n",
            "src/project/BUILD": "python_sources()",
        }
    )
    reported = rule_runner.run_goal_rule(
        PyreflyDumpConfig, args=["src/project::"], env_inherit=_ENV_INHERIT
    )
    assert reported.exit_code == 0
    # `dump-config` prints the resolved interpreter and import-resolution search paths.
    assert "interpreter" in reported.stdout.lower()


def test_update_baseline_merges_partitions(rule_runner: PythonRuleRunner) -> None:
    # Two targets with different interpreter constraints produce two partitions; the merged
    # baseline must contain errors from both.
    rule_runner.write_files(
        {
            "src/a/f.py": 'x: int = "bad"\n',
            "src/a/BUILD": "python_sources(interpreter_constraints=['==3.11.*'])",
            "src/b/g.py": 'y: int = "bad"\n',
            "src/b/BUILD": "python_sources(interpreter_constraints=['==3.12.*'])",
            "pyrefly.toml": 'preset = "legacy"\n',
        }
    )
    result = rule_runner.run_goal_rule(
        PyreflyUpdateBaseline,
        args=["--pyrefly-baseline=bl.json", "src::"],
        env_inherit=_ENV_INHERIT,
    )
    assert result.exit_code == 0
    with open(os.path.join(rule_runner.build_root, "bl.json")) as fh:
        paths = {error["path"] for error in json.load(fh)["errors"]}
    assert "src/a/f.py" in paths
    assert "src/b/g.py" in paths


def test_baseline_gating_compact_multi_partition(rule_runner: PythonRuleRunner) -> None:
    # A merged two-partition baseline in the compact format Pyrefly >= 1.3 writes (what
    # `pyrefly-update-baseline` now produces): real repo paths, no `line`/`code`/`description`.
    # `check` must remap each entry onto that partition's staged path and gate both partitions.
    # (`test_baseline_gating` keeps covering the older full-entry format.)
    def entry(path: str) -> dict:
        return {
            "column": 10,
            "path": path,
            "name": "bad-assignment",
            "concise_description": "`Literal['bad']` is not assignable to `int`",
            "severity": "error",
        }

    rule_runner.write_files(
        {
            "src/a/f.py": 'x: int = "bad"\n',
            "src/a/BUILD": "python_sources(interpreter_constraints=['==3.11.*'])",
            "src/b/g.py": 'y: int = "bad"\n',
            "src/b/BUILD": "python_sources(interpreter_constraints=['==3.12.*'])",
            "pyrefly.toml": 'preset = "legacy"\n',
            "bl.json": json.dumps({"errors": [entry("src/a/f.py"), entry("src/b/g.py")]}),
        }
    )
    targets = [
        rule_runner.get_target(Address("src/a", relative_file_path="f.py")),
        rule_runner.get_target(Address("src/b", relative_file_path="g.py")),
    ]
    ungated = run_pyrefly(rule_runner, targets)
    assert len(ungated) == 2
    assert all(result.exit_code == 1 for result in ungated)

    gated = run_pyrefly(rule_runner, targets, extra_args=["--pyrefly-baseline=bl.json"])
    assert len(gated) == 2
    assert all(result.exit_code == 0 for result in gated)


def test_filter_baseline_entries_scopes_to_partition() -> None:
    # Pure: only this partition's entries survive (remapped to staged paths), orphans (files that no
    # longer exist) pass through at their real path, and other partitions' entries are dropped.
    # Works the same for the compact (>= 1.3) and full entry formats, which both carry `path`.
    compact = {"column": 10, "path": "src/a/f.py", "name": "bad-assignment"}
    full = {"line": 1, "column": 10, "path": "src/a/h.py", "code": -2, "name": "bad-assignment"}
    other = {"column": 10, "path": "src/b/g.py", "name": "bad-assignment"}
    orphan = {"column": 10, "path": "src/gone.py", "name": "bad-assignment"}
    pathless = {"name": "bad-assignment"}
    kept = _filter_baseline_entries(
        [compact, full, other, orphan, pathless],
        {"src/a/f.py": "__pyrefly_root_0/a/f.py", "src/a/h.py": "__pyrefly_root_0/a/h.py"},
        frozenset({"src/gone.py"}),
    )
    assert kept == [
        {**compact, "path": "__pyrefly_root_0/a/f.py"},
        {**full, "path": "__pyrefly_root_0/a/h.py"},
        orphan,
        pathless,
    ]
    # Without orphans (every partition but one, or no `--error-stale-baseline`), they are dropped.
    assert orphan not in _filter_baseline_entries([orphan], {}, frozenset())


def test_has_flag() -> None:
    assert _has_flag(("--a", "--prune-baseline"), "--prune-baseline")
    assert _has_flag(("--prune-baseline=true",), "--prune-baseline")
    assert not _has_flag(("--prune-baseline-x",), "--prune-baseline")


def _write_two_partition_project(
    rule_runner: PythonRuleRunner, baseline_paths: list[str]
) -> list[Target]:
    """Two files with one `bad-assignment` each, in two partitions (3.11 and 3.12), plus a compact
    baseline `bl.json` with an entry for each path in `baseline_paths`. Returns the targets."""

    def entry(path: str) -> dict:
        return {
            "column": 10,
            "path": path,
            "name": "bad-assignment",
            "concise_description": "`Literal['bad']` is not assignable to `int`",
            "severity": "error",
        }

    rule_runner.write_files(
        {
            "src/a/f.py": 'x: int = "bad"\n',
            "src/a/BUILD": "python_sources(interpreter_constraints=['==3.11.*'])",
            "src/b/g.py": 'y: int = "bad"\n',
            "src/b/BUILD": "python_sources(interpreter_constraints=['==3.12.*'])",
            "pyrefly.toml": 'preset = "legacy"\n',
            "bl.json": json.dumps({"errors": [entry(p) for p in baseline_paths]}),
        }
    )
    return [
        rule_runner.get_target(Address("src/a", relative_file_path="f.py")),
        rule_runner.get_target(Address("src/b", relative_file_path="g.py")),
    ]


def _exit_codes_by_python(results: tuple[CheckResult, ...]) -> dict[str, int]:
    """Map each partition's Python minor ("3.11"/"3.12") to its exit code."""
    codes = {}
    for result in results:
        description = result.partition_description or ""
        minor = "3.11" if "3.11" in description else "3.12" if "3.12" in description else "?"
        codes[minor] = result.exit_code
    return codes


_STALE_ARGS = ["--pyrefly-baseline=bl.json", "--pyrefly-args=--error-stale-baseline"]


def test_error_stale_baseline_multi_partition(rule_runner: PythonRuleRunner) -> None:
    # Each partition must see only its own baseline entries: before the fix, every partition got the
    # whole merged baseline, and Pyrefly reported the other partition's entries as stale (their
    # real paths do not exist in the sandbox), so this failed with nothing stale at all.
    targets = _write_two_partition_project(rule_runner, ["src/a/f.py", "src/b/g.py"])
    assert _exit_codes_by_python(run_pyrefly(rule_runner, targets, extra_args=_STALE_ARGS)) == {
        "3.11": 0,
        "3.12": 0,
    }
    # Checking one partition alone is not stale either (the other entry is out of scope).
    assert run_pyrefly(rule_runner, targets[:1], extra_args=_STALE_ARGS)[0].exit_code == 0

    # Fix the error in `src/b/g.py`: its entry is now truly stale, and only that partition fails.
    rule_runner.write_files({"src/b/g.py": "y: int = 1\n"})
    stale = run_pyrefly(rule_runner, targets, extra_args=_STALE_ARGS)
    assert _exit_codes_by_python(stale) == {"3.11": 0, "3.12": 1}
    failed = next(result for result in stale if result.exit_code == 1)
    output = failed.stdout + failed.stderr
    assert "unused suppression" in output
    assert "run `pants pyrefly-update-baseline ::` to update it" in output
    # Without the flag, the stale entry is harmless and both partitions pass, as before.
    gated = run_pyrefly(rule_runner, targets, extra_args=["--pyrefly-baseline=bl.json"])
    assert all(result.exit_code == 0 for result in gated)


def test_error_stale_baseline_deleted_file_reported_once(rule_runner: PythonRuleRunner) -> None:
    # An entry for a file that no longer exists belongs to no partition; it is still stale (as in
    # plain Pyrefly), and is reported by exactly one partition rather than by every partition.
    targets = _write_two_partition_project(
        rule_runner, ["src/a/f.py", "src/b/g.py", "src/deleted.py"]
    )
    results = run_pyrefly(rule_runner, targets, extra_args=_STALE_ARGS)
    assert sorted(result.exit_code for result in results) == [0, 1]
    gated = run_pyrefly(rule_runner, targets, extra_args=["--pyrefly-baseline=bl.json"])
    assert all(result.exit_code == 0 for result in gated)


def test_update_baseline_ignores_error_stale_baseline(rule_runner: PythonRuleRunner) -> None:
    # `--error-stale-baseline` set in `[pyrefly].args` for `check` must not break regeneration
    # (Pyrefly rejects it alongside `--update-baseline`). Regenerating drops the stale entry.
    _write_two_partition_project(rule_runner, ["src/a/f.py", "src/b/g.py"])
    rule_runner.write_files({"src/b/g.py": "y: int = 1\n"})
    result = rule_runner.run_goal_rule(
        PyreflyUpdateBaseline,
        args=[*_STALE_ARGS, "src::"],
        env_inherit=_ENV_INHERIT,
    )
    assert result.exit_code == 0
    with open(os.path.join(rule_runner.build_root, "bl.json")) as fh:
        assert {error["path"] for error in json.load(fh)["errors"]} == {"src/a/f.py"}


_PRUNE_ERROR = "`--prune-baseline` in `[pyrefly].args` is not supported"


def test_prune_baseline_rejected_by_check(rule_runner: PythonRuleRunner) -> None:
    # `--prune-baseline` would prune a sandbox copy and silently leave the user's file unchanged.
    targets = _write_two_partition_project(rule_runner, ["src/a/f.py", "src/b/g.py"])
    with pytest.raises(ExecutionError) as excinfo:
        run_pyrefly(
            rule_runner,
            targets,
            extra_args=["--pyrefly-baseline=bl.json", "--pyrefly-args=--prune-baseline"],
        )
    message = str(excinfo.value)
    assert "PyreflyArgsError" in message
    assert _PRUNE_ERROR in message
    assert "`pants pyrefly-update-baseline ::`" in message


def test_prune_baseline_rejected_by_update_baseline(rule_runner: PythonRuleRunner) -> None:
    _write_two_partition_project(rule_runner, ["src/a/f.py", "src/b/g.py"])
    with pytest.raises(ExecutionError) as excinfo:
        rule_runner.run_goal_rule(
            PyreflyUpdateBaseline,
            args=["--pyrefly-baseline=bl.json", "--pyrefly-args=--prune-baseline", "src::"],
            env_inherit=_ENV_INHERIT,
        )
    assert _PRUNE_ERROR in str(excinfo.value)


def test_lsp_config_respects_pyproject(rule_runner: PythonRuleRunner) -> None:
    # If Pyrefly config already lives in `pyproject.toml [tool.pyrefly]`, the goal must NOT write a
    # shadowing `pyrefly.toml` (a standalone file takes precedence and would silently override it).
    rule_runner.write_files(
        {
            "src/project/f.py": "x = 1\n",
            "src/project/BUILD": "python_sources()",
            "pyproject.toml": '[tool.pyrefly]\npython-version = "3.12"\n',
        }
    )
    result = rule_runner.run_goal_rule(PyreflyLspConfig, env_inherit=_ENV_INHERIT)
    assert result.exit_code == 0
    assert not os.path.exists(os.path.join(rule_runner.build_root, "pyrefly.toml"))


def test_only_filters_error_kinds(rule_runner: PythonRuleRunner) -> None:
    # `[pyrefly].only` restricts reporting to the given error kind(s). A file whose only error is a
    # missing import passes once we ask Pyrefly to report only an unrelated kind.
    rule_runner.write_files(
        {
            "src/project/f.py": "import totally_fake_xyz_123  # pants: no-infer-dep\n",
            "src/project/BUILD": "python_sources()",
        }
    )
    tgt = rule_runner.get_target(Address("src/project", relative_file_path="f.py"))
    assert run_pyrefly(rule_runner, [tgt])[0].exit_code == 1
    filtered = run_pyrefly(rule_runner, [tgt], extra_args=["--pyrefly-only=bad-assignment"])
    assert filtered[0].exit_code == 0


def _denylist(monkeypatch: pytest.MonkeyPatch, version: str, reason: str) -> list[str]:
    """Denylist `version` as `--remove` would: drop its default pins. Returns the dropped pins."""
    dropped = [kv for kv in Pyrefly.default_known_versions if kv.startswith(f"{version}|")]
    assert dropped, f"{version} is not pinned"
    monkeypatch.setattr(subsystems, "DENYLISTED_VERSIONS", {version: reason})
    monkeypatch.setattr(
        Pyrefly,
        "default_known_versions",
        [kv for kv in Pyrefly.default_known_versions if kv not in dropped],
    )
    return dropped


def _clean_target(rule_runner: PythonRuleRunner) -> Target:
    rule_runner.write_files(
        {"src/project/f.py": "x: int = 1\n", "src/project/BUILD": "python_sources()"}
    )
    return rule_runner.get_target(Address("src/project", relative_file_path="f.py"))


@pytest.mark.parametrize(
    "reason",
    ["it miscompiles widgets", "crashes 💥 on startup"],
    ids=["ascii", "non-bmp-emoji"],
)
def test_denylisted_version_fails_with_reason(
    rule_runner: PythonRuleRunner, monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    _denylist(monkeypatch, "1.2.1", reason)
    tgt = _clean_target(rule_runner)
    with pytest.raises(ExecutionError) as excinfo:
        run_pyrefly(rule_runner, [tgt], extra_args=["--pyrefly-version=1.2.1"])
    message = str(excinfo.value)
    assert "DenylistedPyreflyVersion" in message
    # The exact wording, suggesting only the default; the reason (emoji included) is intact.
    assert (
        f"Pyrefly 1.2.1 is not supported by pants-pyrefly: {reason}. "
        f"Set [pyrefly].version to a supported release ({Pyrefly.default_version})."
    ) in message
    assert "known_versions" not in message
    assert "UnknownVersion" not in message


def test_denylisted_version_allowed_with_user_pins(
    rule_runner: PythonRuleRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A user who deliberately supplies their own pins for a denylisted version is not blocked.
    dropped = _denylist(monkeypatch, "1.2.1", "it miscompiles widgets")
    tgt = _clean_target(rule_runner)
    result = run_pyrefly(
        rule_runner,
        [tgt],
        extra_args=["--pyrefly-version=1.2.1", f"--pyrefly-known-versions={json.dumps(dropped)}"],
    )
    assert result[0].exit_code == 0


def test_tool_failure_distinct_from_type_errors(rule_runner: PythonRuleRunner) -> None:
    # A Pyrefly invocation error (an unknown flag) exits with a code other than 0/1; the plugin
    # surfaces that exit code as-is rather than masking it as ordinary type errors.
    rule_runner.write_files(
        {
            "src/project/f.py": "def f(x: int) -> int:\n    return x\n",
            "src/project/BUILD": "python_sources()",
        }
    )
    tgt = rule_runner.get_target(Address("src/project", relative_file_path="f.py"))
    result = run_pyrefly(
        rule_runner, [tgt], extra_args=["--pyrefly-args=--definitely-not-a-real-flag"]
    )
    assert result[0].exit_code not in (0, 1)


def test_suppress_inserts_ignore_comments(rule_runner: PythonRuleRunner) -> None:
    # `pyrefly-suppress` rewrites the targeted files in place, adding a `# pyrefly: ignore` for
    # each current error, and writes them back to the workspace.
    rule_runner.write_files(
        {
            "src/project/f.py": "import totally_fake_suppress_xyz  # pants: no-infer-dep\n",
            "src/project/BUILD": "python_sources()",
        }
    )
    result = rule_runner.run_goal_rule(
        PyreflySuppress, args=["src/project::"], env_inherit=_ENV_INHERIT
    )
    assert result.exit_code == 0
    with open(os.path.join(rule_runner.build_root, "src/project/f.py")) as fh:
        assert "pyrefly: ignore" in fh.read()


# ---
# Re-staged source roots never leak into what the user sees.
# ---

# A synthetic root name as Pyrefly would print it: a whole path segment, not part of a longer word.
_SYNTHETIC_ROOT = re.compile(r"(?<![\w.-])__pyrefly_root_\d+(?![\w-])")

# Three source roots: the build root (`scripts/`), `lib`, and `src/python`. `app` imports `util`
# from `lib`, so both of those roots are staged even when only `app` is checked.
_THREE_ROOT_FILES = {
    "src/python/app/f.py": (
        "import a_module_that_truly_does_not_exist_pyrefly  # pants: no-infer-dep\n"
        "from util.helpers import helper\n"
        # User content that merely contains a synthetic name must survive the rewrite.
        'x: int = "my__pyrefly_root_0"\n'
    ),
    "src/python/app/BUILD": "python_sources()",
    "lib/util/helpers.py": "def helper() -> int:\n    return 1\n",
    "lib/util/BUILD": "python_sources()",
    "scripts/tool.py": "import another_module_that_does_not_exist_pyrefly  # pants: no-infer-dep\n",
    "scripts/BUILD": "python_sources()",
    # The legacy preset flags the bad-assignment that carries the user's string.
    "pyrefly.toml": 'preset = "legacy"\n',
}
_THREE_ROOT_ARGS = ["--source-root-patterns=['/', '/src/python', '/lib']"]


def _three_root_targets(rule_runner: PythonRuleRunner) -> list[Target]:
    rule_runner.write_files(_THREE_ROOT_FILES)
    return [
        rule_runner.get_target(Address("src/python/app", relative_file_path="f.py")),
        rule_runner.get_target(Address("scripts", relative_file_path="tool.py")),
    ]


def _search_path_overrides(text: str) -> list[list[str]]:
    """Every `Search path override` list in a missing-import hint, parsed."""
    return [
        json.loads(match)
        for match in re.findall(r"Search path override \(from command line\): (\[[^\]]*\])", text)
    ]


def test_missing_import_hint_names_real_source_roots(rule_runner: PythonRuleRunner) -> None:
    # Pyrefly's missing-import hint lists the `--search-path`s it was given, which are the
    # plugin's re-staged roots; the user must see the real source roots instead.
    targets = _three_root_targets(rule_runner)
    result = run_pyrefly(rule_runner, targets, extra_args=_THREE_ROOT_ARGS)
    assert len(result) == 1
    assert result[0].exit_code == 1
    combined = result[0].stdout + result[0].stderr
    assert _SYNTHETIC_ROOT.search(combined) is None, combined
    assert _search_path_overrides(combined) == [[".", "lib", "src/python"]] * 2, combined
    assert "src/python/app/f.py" in combined
    assert "scripts/tool.py" in combined
    assert "Literal['my__pyrefly_root_0']" in combined


def test_missing_import_hint_names_real_source_roots_json(rule_runner: PythonRuleRunner) -> None:
    targets = _three_root_targets(rule_runner)
    result = run_pyrefly(
        rule_runner, targets, extra_args=[*_THREE_ROOT_ARGS, "--pyrefly-output-format=json"]
    )
    assert result[0].exit_code == 1
    assert _SYNTHETIC_ROOT.search(result[0].stdout + result[0].stderr) is None
    errors = json.loads(result[0].stdout[result[0].stdout.index("{") :])["errors"]
    hints = [e["description"] for e in errors if e["name"] == "missing-import"]
    assert len(hints) == 2
    assert all(_search_path_overrides(h) == [[".", "lib", "src/python"]] for h in hints), hints
    assert {e["path"] for e in errors} == {"src/python/app/f.py", "scripts/tool.py"}


def test_update_baseline_full_format_description_names_real_source_roots(
    rule_runner: PythonRuleRunner,
) -> None:
    # Pyrefly < 1.3 writes the full-format baseline, whose `description` is the error's whole text,
    # hints included. It must name the real source roots, not the sandbox or the re-staged roots,
    # and gating on it must still work.
    targets = _three_root_targets(rule_runner)
    version_args = [*_THREE_ROOT_ARGS, "--pyrefly-version=1.2.0"]
    result = rule_runner.run_goal_rule(
        PyreflyUpdateBaseline,
        args=[*version_args, "--pyrefly-baseline=bl.json", "src/python/app:", "scripts:"],
        env_inherit=_ENV_INHERIT,
    )
    assert result.exit_code == 0
    with open(os.path.join(rule_runner.build_root, "bl.json")) as fh:
        raw = fh.read()
    assert _SYNTHETIC_ROOT.search(raw) is None, raw
    assert "pants-sandbox-" not in raw, raw
    hints = [e["description"] for e in json.loads(raw)["errors"] if e["name"] == "missing-import"]
    assert len(hints) == 2
    assert all(_search_path_overrides(h) == [[".", "lib", "src/python"]] for h in hints), hints

    gated = run_pyrefly(
        rule_runner, targets, extra_args=[*version_args, "--pyrefly-baseline=bl.json"]
    )
    assert gated[0].exit_code == 0
