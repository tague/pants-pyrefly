# Copyright 2026 Tague Griffith
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

import json
import logging
from collections.abc import Iterable

from pants_pyrefly.rules import (
    _BASELINE_OUTPUT,
    PyreflyFieldSet,
    PyreflyRequest,
    _setup_pyrefly_process,
    pyrefly_determine_partitions,
)
from pants_pyrefly.subsystems import Pyrefly

from pants.backend.python.subsystems.setup import PythonSetup
from pants.engine.fs import CreateDigest, FileContent, Workspace
from pants.engine.goal import Goal, GoalSubsystem
from pants.engine.intrinsics import create_digest, execute_process, get_digest_contents
from pants.engine.platform import Platform
from pants.engine.process import ProcessCacheScope
from pants.engine.rules import Rule, collect_rules, concurrently, goal_rule, implicitly
from pants.engine.target import Targets
from pants.engine.unions import UnionRule
from pants.util.strutil import softwrap

logger = logging.getLogger(__name__)


class PyreflyUpdateBaselineSubsystem(GoalSubsystem):
    name = "pyrefly-update-baseline"
    help = softwrap(
        """
        Run Pyrefly and (re)write the baseline file configured by `[pyrefly].baseline`, recording
        the current type errors so that a subsequent `pants check` reports only NEW ones.
        """
    )


class PyreflyUpdateBaseline(Goal):
    subsystem_cls = PyreflyUpdateBaselineSubsystem
    environment_behavior = Goal.EnvironmentBehavior.LOCAL_ONLY


@goal_rule
async def pyrefly_update_baseline(
    targets: Targets,
    pyrefly: Pyrefly,
    workspace: Workspace,
    platform: Platform,
    python_setup: PythonSetup,
) -> PyreflyUpdateBaseline:
    if not pyrefly.baseline:
        logger.error(
            softwrap(
                """
                Set `[pyrefly].baseline` to the path where the baseline file should be written,
                then re-run `pants pyrefly-update-baseline`.
                """
            )
        )
        return PyreflyUpdateBaseline(exit_code=1)

    field_sets = tuple(
        PyreflyFieldSet.create(tgt)
        for tgt in targets
        if PyreflyFieldSet.is_applicable(tgt) and not PyreflyFieldSet.opt_out(tgt)
    )
    if not field_sets:
        logger.warning("No Pyrefly-applicable targets in scope; the baseline was not changed.")
        return PyreflyUpdateBaseline(exit_code=0)

    partitions = await pyrefly_determine_partitions(PyreflyRequest(field_sets), **implicitly())
    processes = await concurrently(
        _setup_pyrefly_process(
            partition,
            pyrefly,
            platform,
            python_setup,
            update_baseline=True,
            cache_scope=ProcessCacheScope.PER_SESSION,
        )
        for partition in partitions
    )
    results = await concurrently(execute_process(process, **implicitly()) for process in processes)

    for result in results:
        # 0 == no errors, 1 == errors found (both expected); anything else is a tool failure, and
        # we must not clobber a good baseline with a partial/empty one.
        if result.exit_code not in (0, 1):
            logger.error(
                f"Pyrefly exited with code {result.exit_code} while updating the baseline; "
                f"leaving the existing baseline unchanged.\n"
                f"{result.stderr.decode(errors='replace')}"
            )
            return PyreflyUpdateBaseline(exit_code=result.exit_code)

    # Merge the per-partition baselines (each is `{"errors": [...]}`) into a single file.
    all_errors: list = []
    for result in results:
        for file_content in await get_digest_contents(result.output_digest):
            if file_content.path == _BASELINE_OUTPUT and file_content.content.strip():
                all_errors.extend(json.loads(file_content.content).get("errors", []))

    merged = json.dumps({"errors": all_errors}, indent=2).encode()
    output_digest = await create_digest(CreateDigest([FileContent(pyrefly.baseline, merged)]))
    workspace.write_digest(output_digest)
    logger.info(f"Wrote Pyrefly baseline with {len(all_errors)} error(s) to `{pyrefly.baseline}`.")
    return PyreflyUpdateBaseline(exit_code=0)


def rules() -> Iterable[Rule | UnionRule]:
    return (*collect_rules(),)
