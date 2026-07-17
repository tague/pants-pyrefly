# Copyright 2026 Tague Griffith
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

import json
import os

import pytest  # pants: no-infer-dep

from pants_pyrefly.goals import (
    PyreflyCoverage,
    PyreflyInit,
    PyreflyLspConfig,
    PyreflySuppress,
    PyreflyUpdateBaseline,
)
from pants_pyrefly.register import rules as pyrefly_register_rules
from pants_pyrefly.rules import PyreflyFieldSet, PyreflyRequest

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
from pants.engine.rules import QueryRule
from pants.engine.target import Target
from pants.testutil.python_rule_runner import PythonRuleRunner

# Inherited so Pants can discover system interpreters and download the Pyrefly binary.
_ENV_INHERIT = {"PATH", "PYENV_ROOT", "HOME"}


@pytest.fixture
def rule_runner() -> PythonRuleRunner:
    return PythonRuleRunner(
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
        env_inherit={"PATH", "PYENV_ROOT", "HOME"},
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
    result = rule_runner.run_goal_rule(PyreflyLspConfig, env_inherit={"PATH", "PYENV_ROOT", "HOME"})
    assert result.exit_code == 0
    config_path = os.path.join(rule_runner.build_root, "pyrefly.toml")
    assert os.path.exists(config_path)
    with open(config_path) as fh:
        content = fh.read()
    assert "search-path" in content
    assert "python-version" in content


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
