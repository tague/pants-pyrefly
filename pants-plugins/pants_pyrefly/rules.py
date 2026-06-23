# Copyright 2026 Tague Griffith
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass

from pants_pyrefly.skip_field import SkipPyreflyField
from pants_pyrefly.subsystems import Pyrefly

from pants.backend.python.subsystems.setup import PythonSetup
from pants.backend.python.target_types import (
    InterpreterConstraintsField,
    PythonResolveField,
    PythonSourceField,
)
from pants.backend.python.util_rules import pex_from_targets
from pants.backend.python.util_rules.interpreter_constraints import InterpreterConstraints
from pants.backend.python.util_rules.partition import (
    _partition_by_interpreter_constraints_and_resolve,
)
from pants.backend.python.util_rules.pex import Pex, PexRequest, create_pex, create_venv_pex
from pants.backend.python.util_rules.pex_from_targets import RequirementsPexRequest
from pants.backend.python.util_rules.pex_requirements import PexRequirements
from pants.backend.python.util_rules.python_sources import (
    PythonSourceFilesRequest,
    prepare_python_sources,
)
from pants.core.goals.check import CheckRequest, CheckResult, CheckResults, CheckSubsystem
from pants.core.util_rules import config_files
from pants.core.util_rules.config_files import find_config_file
from pants.core.util_rules.external_tool import download_external_tool
from pants.core.util_rules.source_files import SourceFilesRequest, determine_source_files
from pants.engine.collection import Collection
from pants.engine.fs import (
    EMPTY_DIGEST,
    CreateDigest,
    FileContent,
    GlobMatchErrorBehavior,
    MergeDigests,
    PathGlobs,
)
try:
    # Pants >= 2.30 renamed this call-by-name rule.
    from pants.engine.internals.graph import resolve_coarsened_targets as coarsened_targets_get
except ImportError:
    # Pants < 2.30 (e.g. 2.27) — identical call signature, earlier name.
    from pants.engine.internals.graph import coarsened_targets as coarsened_targets_get
from pants.engine.intrinsics import (
    create_digest,
    execute_process,
    merge_digests,
    path_globs_to_digest,
)
from pants.engine.platform import Platform
from pants.engine.process import Process, ProcessCacheScope
from pants.engine.rules import Rule, collect_rules, concurrently, implicitly, rule
from pants.engine.target import CoarsenedTargets, CoarsenedTargetsRequest, FieldSet, Target
from pants.engine.unions import UnionRule
from pants.util.logging import LogLevel
from pants.util.ordered_set import FrozenOrderedSet, OrderedSet
from pants.util.strutil import pluralize, softwrap

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PyreflyFieldSet(FieldSet):
    required_fields = (PythonSourceField,)

    sources: PythonSourceField
    resolve: PythonResolveField
    interpreter_constraints: InterpreterConstraintsField

    @classmethod
    def opt_out(cls, tgt: Target) -> bool:
        return tgt.get(SkipPyreflyField).value


class PyreflyRequest(CheckRequest):
    field_set_type = PyreflyFieldSet
    tool_name = Pyrefly.options_scope


@dataclass(frozen=True)
class PyreflyPartition:
    field_sets: FrozenOrderedSet[PyreflyFieldSet]
    root_targets: CoarsenedTargets
    resolve_description: str | None
    interpreter_constraints: InterpreterConstraints

    def description(self) -> str:
        ics = str(sorted(str(c) for c in self.interpreter_constraints))
        return f"{self.resolve_description}, {ics}" if self.resolve_description else ics


class PyreflyPartitions(Collection[PyreflyPartition]):
    pass


@rule(
    desc="Determine if it is necessary to partition Pyrefly's input (interpreter_constraints and resolves)",
    level=LogLevel.DEBUG,
)
async def pyrefly_determine_partitions(
    request: PyreflyRequest, pyrefly: Pyrefly, python_setup: PythonSetup
) -> PyreflyPartitions:
    resolve_and_interpreter_constraints_to_field_sets = (
        _partition_by_interpreter_constraints_and_resolve(request.field_sets, python_setup)
    )

    coarsened_targets = await coarsened_targets_get(
        CoarsenedTargetsRequest(field_set.address for field_set in request.field_sets),
        **implicitly(),
    )
    coarsened_targets_by_address = coarsened_targets.by_address()

    return PyreflyPartitions(
        PyreflyPartition(
            FrozenOrderedSet(field_sets),
            CoarsenedTargets(
                OrderedSet(
                    coarsened_targets_by_address[field_set.address] for field_set in field_sets
                )
            ),
            resolve if len(python_setup.resolves) > 1 else None,
            interpreter_constraints or pyrefly.interpreter_constraints,
        )
        for (resolve, interpreter_constraints), field_sets in sorted(
            resolve_and_interpreter_constraints_to_field_sets.items()
        )
    )


# Fixed sandbox path where `--update-baseline` writes the baseline; the update-baseline goal
# relocates it to the user's configured `[pyrefly].baseline` path on write-back.
_BASELINE_OUTPUT = "__pyrefly_baseline_out.json"


async def _setup_pyrefly_process(
    partition: PyreflyPartition,
    pyrefly: Pyrefly,
    platform: Platform,
    python_setup: PythonSetup,
    *,
    update_baseline: bool,
    cache_scope: ProcessCacheScope,
) -> Process:
    # Gather, concurrently:
    #   - the Pyrefly binary itself,
    #   - the root source files we are reporting on,
    #   - the full first-party dependency closure on disk (+ its source roots),
    #   - a PEX of the third-party requirements, and
    #   - any discovered Pyrefly config file.
    (
        downloaded_pyrefly,
        root_sources,
        transitive_sources,
        requirements_pex,
        config_file_snapshot,
    ) = await concurrently(
        download_external_tool(pyrefly.get_request(platform)),
        determine_source_files(SourceFilesRequest(fs.sources for fs in partition.field_sets)),
        prepare_python_sources(
            PythonSourceFilesRequest(partition.root_targets.closure()), **implicitly()
        ),
        create_pex(
            **implicitly(
                RequirementsPexRequest(
                    (fs.address for fs in partition.field_sets),
                    hardcoded_interpreter_constraints=partition.interpreter_constraints,
                )
            )
        ),
        find_config_file(pyrefly.config_request()),
    )

    # Optionally resolve extra type-stub packages and merge them into the same venv, so Pyrefly
    # sees their types without them becoming runtime dependencies of the checked code.
    extra_stub_pexes: list[Pex] = []
    if pyrefly.extra_type_stubs:
        extra_stubs_pex = await create_pex(
            **implicitly(
                PexRequest(
                    output_filename="pyrefly_extra_type_stubs.pex",
                    internal_only=True,
                    requirements=PexRequirements(
                        pyrefly.extra_type_stubs,
                        description_of_origin="the option `[pyrefly].extra_type_stubs`",
                    ),
                    interpreter_constraints=partition.interpreter_constraints,
                )
            )
        )
        extra_stub_pexes = [extra_stubs_pex]

    # Wrap the third-party requirements (plus any extra type stubs) in a venv PEX. We point
    # Pyrefly's `--python-interpreter-path` at this venv's Python so it can discover the third-party
    # `site-packages` (and the target Python version) exactly the way `import` would at runtime.
    requirements_venv_pex = await create_venv_pex(
        **implicitly(
            PexRequest(
                output_filename="requirements_venv.pex",
                internal_only=True,
                pex_path=[requirements_pex, *extra_stub_pexes],
                interpreter_constraints=partition.interpreter_constraints,
            )
        )
    )

    # Baseline handling. In `update` mode we (re)write a baseline to a fixed sandbox path and
    # capture it; otherwise we materialize the user's baseline (if any) and gate against it.
    baseline_args: list[str] = []
    baseline_digest = EMPTY_DIGEST
    output_files: tuple[str, ...] = ()
    if update_baseline:
        baseline_args = [f"--baseline={_BASELINE_OUTPUT}", "--update-baseline"]
        output_files = (_BASELINE_OUTPUT,)
    elif pyrefly.baseline:
        baseline_digest = await path_globs_to_digest(
            PathGlobs(
                [pyrefly.baseline],
                glob_match_error_behavior=GlobMatchErrorBehavior.ignore,
            )
        )
        if baseline_digest != EMPTY_DIGEST:
            baseline_args = [f"--baseline={pyrefly.baseline}"]
        else:
            logger.warning(
                softwrap(
                    f"""
                    `[pyrefly].baseline` is set to `{pyrefly.baseline}`, but that file does not
                    exist. Run `pants pyrefly-update-baseline` to create it; checking without a
                    baseline for now.
                    """
                )
            )

    # Pass the files to check via an argfile rather than argv, so we never hit OS command-line
    # length limits on targets with many files (Pyrefly reads `@<file>`, like other clap CLIs).
    file_list_path = "__pyrefly_files.txt"
    file_list_digest = await create_digest(
        CreateDigest([FileContent(file_list_path, "\n".join(root_sources.snapshot.files).encode())])
    )

    input_digest = await merge_digests(
        MergeDigests(
            (
                root_sources.snapshot.digest,
                transitive_sources.source_files.snapshot.digest,
                config_file_snapshot.snapshot.digest,
                requirements_venv_pex.digest,
                file_list_digest,
                baseline_digest,
            )
        )
    )

    tool_key = "__pyrefly_tool"
    exe_path = os.path.normpath(os.path.join(tool_key, downloaded_pyrefly.exe))

    python_version = partition.interpreter_constraints.minimum_python_version(
        python_setup.interpreter_versions_universe
    )

    argv: list[str] = [exe_path, "check"]
    # First-party import roots (the analogue of MYPYPATH / sys.path).
    argv.extend(f"--search-path={source_root}" for source_root in transitive_sources.source_roots)
    # Third-party deps + interpreter introspection.
    argv.append(f"--python-interpreter-path={requirements_venv_pex.python.argv0}")
    if python_version:
        argv.append(f"--python-version={python_version}")
    if pyrefly.output_format:
        argv.append(f"--output-format={pyrefly.output_format}")
    # An explicitly-configured config file. Discovered configs are found by Pyrefly itself
    # relative to the sandbox cwd; both are materialized into the input digest above.
    if pyrefly.config:
        argv.append(f"--config={pyrefly.config}")
    argv.extend(baseline_args)
    # User-provided args (can override any of the above).
    argv.extend(pyrefly.args)
    # The files to report on, passed via the argfile created above.
    argv.append(f"@{file_list_path}")

    return Process(
        argv=tuple(argv),
        input_digest=input_digest,
        immutable_input_digests={tool_key: downloaded_pyrefly.digest},
        append_only_caches=requirements_venv_pex.append_only_caches or {},
        output_files=output_files,
        description=f"Run Pyrefly on {pluralize(len(root_sources.snapshot.files), 'file')}.",
        level=LogLevel.DEBUG,
        cache_scope=cache_scope,
    )


@rule(
    desc="Pyrefly typecheck each partition based on its interpreter_constraints",
    level=LogLevel.DEBUG,
)
async def pyrefly_typecheck_partition(
    partition: PyreflyPartition,
    pyrefly: Pyrefly,
    check_subsystem: CheckSubsystem,
    platform: Platform,
    python_setup: PythonSetup,
) -> CheckResult:
    process = await _setup_pyrefly_process(
        partition,
        pyrefly,
        platform,
        python_setup,
        update_baseline=False,
        # `default_process_cache_scope` (which honors `--force`) exists on Pants >= 2.30;
        # on 2.27 fall back to the normal "cache successful runs" scope.
        cache_scope=getattr(
            check_subsystem, "default_process_cache_scope", ProcessCacheScope.SUCCESSFUL
        ),
    )
    process_result = await execute_process(process, **implicitly())
    return CheckResult.from_fallible_process_result(
        process_result,
        partition_description=partition.description(),
    )


@rule(desc="Typecheck using Pyrefly", level=LogLevel.DEBUG)
async def pyrefly_typecheck(request: PyreflyRequest, pyrefly: Pyrefly) -> CheckResults:
    if pyrefly.skip:
        return CheckResults([], checker_name=request.tool_name)

    partitions = await pyrefly_determine_partitions(request, **implicitly())
    partitioned_results = await concurrently(
        pyrefly_typecheck_partition(partition, **implicitly()) for partition in partitions
    )
    return CheckResults(partitioned_results, checker_name=request.tool_name)


def rules() -> Iterable[Rule | UnionRule]:
    return (
        *collect_rules(),
        *config_files.rules(),
        *pex_from_targets.rules(),
        UnionRule(CheckRequest, PyreflyRequest),
    )
