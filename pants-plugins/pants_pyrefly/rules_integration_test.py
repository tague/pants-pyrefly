# Copyright 2026 Tague Griffith
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

import json

import pytest  # pants: no-infer-dep

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
            "src/project/f.py": "import yaml  # pants: no-infer-dep\n\nvalue = yaml.safe_load('a: 1')\n",
            "src/project/BUILD": "python_sources()",
        }
    )
    tgt = rule_runner.get_target(Address("src/project", relative_file_path="f.py"))
    result = run_pyrefly(
        rule_runner, [tgt], extra_args=["--pyrefly-extra-type-stubs=types-PyYAML"]
    )
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
