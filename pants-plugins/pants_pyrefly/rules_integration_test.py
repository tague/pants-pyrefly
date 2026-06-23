# Copyright 2026 Tague Griffith
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

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
