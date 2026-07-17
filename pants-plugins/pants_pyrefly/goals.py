# Copyright 2026 Tague Griffith
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable

import toml  # pants: no-infer-dep  (provided by the Pants runtime)

from pants_pyrefly.rules import (
    _BASELINE_OUTPUT,
    PyreflyFieldSet,
    PyreflyRequest,
    _setup_pyrefly_process,
    pyrefly_determine_partitions,
)
from pants_pyrefly.subsystems import Pyrefly

from pants.backend.python.subsystems.setup import PythonSetup
from pants.backend.python.util_rules.interpreter_constraints import InterpreterConstraints
from pants.backend.python.util_rules.python_sources import (
    PythonSourceFilesRequest,
    prepare_python_sources,
)
from pants.engine.fs import (
    EMPTY_DIGEST,
    CreateDigest,
    FileContent,
    GlobMatchErrorBehavior,
    MergeDigests,
    PathGlobs,
    Workspace,
)
from pants.engine.console import Console
from pants.engine.goal import Goal, GoalSubsystem
from pants.engine.intrinsics import (
    create_digest,
    execute_process,
    get_digest_contents,
    merge_digests,
    path_globs_to_digest,
)
from pants.core.util_rules.external_tool import download_external_tool
from pants.engine.platform import Platform
from pants.engine.process import Process, ProcessCacheScope
from pants.engine.rules import Rule, collect_rules, concurrently, goal_rule, implicitly
from pants.engine.target import AllTargets, Targets
from pants.engine.unions import UnionRule
from pants.option.option_types import BoolOption, FloatOption, StrOption
from pants.util.logging import LogLevel
from pants.util.strutil import softwrap

logger = logging.getLogger(__name__)


# ---
# `pyrefly-update-baseline`
# ---


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


# ---
# `pyrefly-lsp-config`
# ---


class PyreflyLspConfigSubsystem(GoalSubsystem):
    name = "pyrefly-lsp-config"
    help = softwrap(
        """
        Write Pants's source roots (and target Python version) into `pyrefly.toml` as `search-path`,
        so the Pyrefly IDE/LSP resolves first-party imports the way Pants does. Point your editor's
        interpreter at a venv (e.g. `pants export`) for third-party imports.
        """
    )


class PyreflyLspConfig(Goal):
    subsystem_cls = PyreflyLspConfigSubsystem
    environment_behavior = Goal.EnvironmentBehavior.LOCAL_ONLY


@goal_rule
async def pyrefly_lsp_config(
    all_targets: AllTargets,
    python_setup: PythonSetup,
    workspace: Workspace,
) -> PyreflyLspConfig:
    python_targets = [tgt for tgt in all_targets if PyreflyFieldSet.is_applicable(tgt)]
    if not python_targets:
        logger.warning("No Python source targets found; not writing a Pyrefly LSP config.")
        return PyreflyLspConfig(exit_code=0)

    sources = await prepare_python_sources(PythonSourceFilesRequest(python_targets), **implicitly())
    python_version = InterpreterConstraints(
        python_setup.interpreter_constraints
    ).minimum_python_version(python_setup.interpreter_versions_universe)

    settings: dict = {"search-path": sorted(sources.source_roots)}
    if python_version:
        settings["python-version"] = python_version

    pyrefly_toml_digest, pyproject_digest = await concurrently(
        path_globs_to_digest(
            PathGlobs(["pyrefly.toml"], glob_match_error_behavior=GlobMatchErrorBehavior.ignore)
        ),
        path_globs_to_digest(
            PathGlobs(["pyproject.toml"], glob_match_error_behavior=GlobMatchErrorBehavior.ignore)
        ),
    )

    pyproject_has_pyrefly = False
    if pyproject_digest != EMPTY_DIGEST:
        pyproject_text = (await get_digest_contents(pyproject_digest))[0].content.decode()
        pyproject_has_pyrefly = "pyrefly" in toml.loads(pyproject_text).get("tool", {})

    # Don't create a `pyrefly.toml` that would shadow an existing `pyproject.toml [tool.pyrefly]`.
    if pyrefly_toml_digest == EMPTY_DIGEST and pyproject_has_pyrefly:
        logger.warning(
            softwrap(
                """
                Your Pyrefly config lives in `pyproject.toml` under `[tool.pyrefly]`. A separate
                `pyrefly.toml` would take precedence over it, so it was not written. Add these keys
                to your `[tool.pyrefly]` table instead:
                """
            )
            + f"\n\n{toml.dumps(settings).strip()}\n"
        )
        return PyreflyLspConfig(exit_code=0)

    # Merge into an existing `pyrefly.toml` (preserving other keys) or create a new one.
    existing: dict = {}
    if pyrefly_toml_digest != EMPTY_DIGEST:
        existing = toml.loads((await get_digest_contents(pyrefly_toml_digest))[0].content.decode())
    existing.update(settings)

    output_digest = await create_digest(
        CreateDigest([FileContent("pyrefly.toml", toml.dumps(existing).encode())])
    )
    workspace.write_digest(output_digest)
    logger.info(
        f"Wrote {len(settings['search-path'])} source root(s) to `pyrefly.toml` as Pyrefly "
        f"`search-path`. Point your editor's interpreter at a venv (e.g. `pants export`) for "
        f"third-party imports."
    )
    return PyreflyLspConfig(exit_code=0)


# ---
# `pyrefly-coverage`
# ---


class PyreflyCoverageSubsystem(GoalSubsystem):
    name = "pyrefly-coverage"
    help = softwrap(
        """
        Report Pyrefly type coverage — the share of typable symbols that have a non-`Any` type —
        across the targeted Python sources.
        """
    )

    fail_under = FloatOption(
        default=None,
        help="If set, exit non-zero when overall type coverage is below this percentage (0-100).",
    )


class PyreflyCoverage(Goal):
    subsystem_cls = PyreflyCoverageSubsystem
    environment_behavior = Goal.EnvironmentBehavior.LOCAL_ONLY


@goal_rule
async def pyrefly_coverage(
    targets: Targets,
    pyrefly: Pyrefly,
    coverage_subsystem: PyreflyCoverageSubsystem,
    console: Console,
    platform: Platform,
    python_setup: PythonSetup,
) -> PyreflyCoverage:
    field_sets = tuple(
        PyreflyFieldSet.create(tgt)
        for tgt in targets
        if PyreflyFieldSet.is_applicable(tgt) and not PyreflyFieldSet.opt_out(tgt)
    )
    if not field_sets:
        logger.warning("No Pyrefly-applicable targets in scope.")
        return PyreflyCoverage(exit_code=0)

    partitions = await pyrefly_determine_partitions(PyreflyRequest(field_sets), **implicitly())
    processes = await concurrently(
        _setup_pyrefly_process(
            partition,
            pyrefly,
            platform,
            python_setup,
            subcommand=("coverage", "report"),
            cache_scope=ProcessCacheScope.SUCCESSFUL,
        )
        for partition in partitions
    )
    results = await concurrently(execute_process(process, **implicitly()) for process in processes)

    total_typable = 0
    total_typed = 0
    for result in results:
        if result.exit_code != 0:
            logger.error(
                f"Pyrefly coverage failed (exit {result.exit_code}):\n"
                f"{result.stderr.decode(errors='replace')}"
            )
            return PyreflyCoverage(exit_code=result.exit_code)
        try:
            report = json.loads(result.stdout)
        except json.JSONDecodeError:
            logger.error(
                "Could not parse Pyrefly coverage output as JSON:\n"
                f"{result.stdout.decode(errors='replace')[:500]}"
            )
            return PyreflyCoverage(exit_code=1)
        for module_report in report.get("module_reports", []):
            for symbol in module_report.get("symbol_reports", []):
                total_typable += symbol.get("n_typable", 0)
                total_typed += symbol.get("n_typed", 0)

    pct = (100.0 * total_typed / total_typable) if total_typable else 100.0
    console.print_stdout(
        f"Pyrefly type coverage: {pct:.1f}% ({total_typed}/{total_typable} typable symbols)"
    )
    if coverage_subsystem.fail_under is not None and pct < coverage_subsystem.fail_under:
        console.print_stderr(
            f"Type coverage {pct:.1f}% is below the required {coverage_subsystem.fail_under}%."
        )
        return PyreflyCoverage(exit_code=1)
    return PyreflyCoverage(exit_code=0)


# ---
# `pyrefly-suppress`
# ---


class PyreflySuppressSubsystem(GoalSubsystem):
    name = "pyrefly-suppress"
    help = softwrap(
        """
        Insert inline `# pyrefly: ignore` comments to silence the current Pyrefly errors in the
        targeted sources (wraps Pyrefly's `suppress`). Useful for adopting Pyrefly on code with
        pre-existing errors: suppress them now, then remove the comments as you fix them. Edits
        files in place, so run on a clean working tree.
        """
    )

    remove_unused = BoolOption(
        default=False,
        help=softwrap(
            """
            Instead of adding suppressions, remove `# pyrefly: ignore` comments that are no longer
            needed (`pyrefly suppress --remove-unused`).
            """
        ),
    )


class PyreflySuppress(Goal):
    subsystem_cls = PyreflySuppressSubsystem
    environment_behavior = Goal.EnvironmentBehavior.LOCAL_ONLY


@goal_rule
async def pyrefly_suppress(
    targets: Targets,
    pyrefly: Pyrefly,
    suppress_subsystem: PyreflySuppressSubsystem,
    workspace: Workspace,
    platform: Platform,
    python_setup: PythonSetup,
) -> PyreflySuppress:
    field_sets = tuple(
        PyreflyFieldSet.create(tgt)
        for tgt in targets
        if PyreflyFieldSet.is_applicable(tgt) and not PyreflyFieldSet.opt_out(tgt)
    )
    if not field_sets:
        logger.warning("No Pyrefly-applicable targets in scope; nothing to suppress.")
        return PyreflySuppress(exit_code=0)

    partitions = await pyrefly_determine_partitions(PyreflyRequest(field_sets), **implicitly())
    subcommand_args = ("--remove-unused",) if suppress_subsystem.remove_unused else ()
    processes = await concurrently(
        _setup_pyrefly_process(
            partition,
            pyrefly,
            platform,
            python_setup,
            subcommand=("suppress",),
            subcommand_args=subcommand_args,
            capture_root_sources=True,
            cache_scope=ProcessCacheScope.PER_SESSION,
        )
        for partition in partitions
    )
    results = await concurrently(execute_process(process, **implicitly()) for process in processes)

    for result in results:
        # 0 == nothing to do, 1 == errors were suppressed (both expected). Anything else is a tool
        # failure; don't write back a partial/garbled edit.
        if result.exit_code not in (0, 1):
            logger.error(
                f"Pyrefly `suppress` exited with code {result.exit_code}; files left unchanged.\n"
                f"{result.stderr.decode(errors='replace')}"
            )
            return PyreflySuppress(exit_code=result.exit_code)

    output_digest = await merge_digests(MergeDigests(result.output_digest for result in results))
    workspace.write_digest(output_digest)
    action = (
        "Removed unused Pyrefly suppressions"
        if suppress_subsystem.remove_unused
        else "Applied Pyrefly suppressions"
    )
    logger.info(f"{action} across {len(field_sets)} target(s).")
    return PyreflySuppress(exit_code=0)


# ---
# `pyrefly-init`
# ---

_MIGRATE_FROM_CHOICES = ("auto", "mypy", "pyright")


class PyreflyInitSubsystem(GoalSubsystem):
    name = "pyrefly-init"
    help = softwrap(
        """
        Bootstrap a Pyrefly config for this repo by running `pyrefly init`. Writes a `pyrefly.toml`
        in the build root, migrating an existing MyPy or Pyright configuration when one is found.
        Run once when adopting Pyrefly. After it writes the config, run `pants pyrefly-lsp-config`
        to add Pants's source roots so the editor/LSP resolves first-party imports.
        """
    )

    migrate_from = StrOption(
        default=None,
        help=softwrap(
            """
            Which existing type-checker config to migrate from: `auto` (try MyPy, then Pyright),
            `mypy`, or `pyright`. When unset, Pyrefly's own default (`auto`) is used.
            """
        ),
    )


class PyreflyInit(Goal):
    subsystem_cls = PyreflyInitSubsystem
    environment_behavior = Goal.EnvironmentBehavior.LOCAL_ONLY


@goal_rule
async def pyrefly_init(
    init_subsystem: PyreflyInitSubsystem,
    pyrefly: Pyrefly,
    workspace: Workspace,
    platform: Platform,
) -> PyreflyInit:
    if (
        init_subsystem.migrate_from is not None
        and init_subsystem.migrate_from not in _MIGRATE_FROM_CHOICES
    ):
        logger.error(
            f"`--pyrefly-init-migrate-from` must be one of {', '.join(_MIGRATE_FROM_CHOICES)}; "
            f"got `{init_subsystem.migrate_from}`."
        )
        return PyreflyInit(exit_code=1)

    # `pyrefly init` reads any existing type-checker config from the working directory; materialize
    # the candidates into the sandbox (no source files are needed — init does not type-check).
    downloaded_pyrefly, config_digest = await concurrently(
        download_external_tool(pyrefly.get_request(platform)),
        path_globs_to_digest(
            PathGlobs(
                ["pyproject.toml", "mypy.ini", "setup.cfg", "pyrefly.toml"],
                glob_match_error_behavior=GlobMatchErrorBehavior.ignore,
            )
        ),
    )
    input_files = {fc.path: fc.content for fc in await get_digest_contents(config_digest)}

    # `pyrefly init` refuses to overwrite an existing config; surface that as a clear message
    # rather than a bare nonzero exit. The `b"[tool.pyrefly"` sentinel matches `config_request()`.
    already_configured = "pyrefly.toml" in input_files or (
        b"[tool.pyrefly" in input_files.get("pyproject.toml", b"")
    )
    if already_configured:
        logger.error(
            softwrap(
                """
                A Pyrefly config already exists (`pyrefly.toml`, or `[tool.pyrefly]` in
                `pyproject.toml`). Remove it and re-run `pants pyrefly-init` to regenerate.
                """
            )
        )
        return PyreflyInit(exit_code=1)

    tool_key = "__pyrefly_tool"
    exe_path = os.path.normpath(os.path.join(tool_key, downloaded_pyrefly.exe))
    argv = [exe_path, "init", "--non-interactive"]
    if init_subsystem.migrate_from is not None:
        argv += ["--migrate-from", init_subsystem.migrate_from]
    argv.append(".")

    result = await execute_process(
        Process(
            argv=tuple(argv),
            input_digest=config_digest,
            immutable_input_digests={tool_key: downloaded_pyrefly.digest},
            # Capture both possible targets; a nonexistent output file is simply omitted, so this
            # grabs whichever file `init` writes without us needing to predict it.
            output_files=("pyrefly.toml", "pyproject.toml"),
            description="Bootstrap a Pyrefly config (`pyrefly init`).",
            level=LogLevel.DEBUG,
            cache_scope=ProcessCacheScope.PER_SESSION,
        ),
        **implicitly(),
    )
    if result.exit_code != 0:
        logger.error(
            f"`pyrefly init` failed (exit {result.exit_code}):\n"
            f"{result.stderr.decode(errors='replace')}"
        )
        return PyreflyInit(exit_code=result.exit_code)

    # Write back only the files `init` actually created or changed (it also captures an untouched
    # pyproject.toml as a maybe-target — don't rewrite it needlessly).
    changed = [
        fc
        for fc in await get_digest_contents(result.output_digest)
        if input_files.get(fc.path) != fc.content
    ]
    if not changed:
        logger.warning("`pyrefly init` produced no config changes.")
        return PyreflyInit(exit_code=0)

    output_digest = await create_digest(CreateDigest(changed))
    workspace.write_digest(output_digest)
    logger.info(f"Wrote Pyrefly config: {', '.join(sorted(fc.path for fc in changed))}.")
    return PyreflyInit(exit_code=0)


# ---
# `pyrefly-dump-config`
# ---


class PyreflyDumpConfigSubsystem(GoalSubsystem):
    name = "pyrefly-dump-config"
    help = softwrap(
        """
        Print the effective Pyrefly configuration Pants assembles for the targeted sources — the
        first-party `search-path`s, the interpreter Pyrefly resolves third-party imports from, and
        the config file in effect — by running Pyrefly's `dump-config` subcommand. Diagnostic only;
        it does not type-check. Use it to debug import or interpreter resolution.
        """
    )


class PyreflyDumpConfig(Goal):
    subsystem_cls = PyreflyDumpConfigSubsystem
    environment_behavior = Goal.EnvironmentBehavior.LOCAL_ONLY


@goal_rule
async def pyrefly_dump_config(
    targets: Targets,
    pyrefly: Pyrefly,
    console: Console,
    platform: Platform,
    python_setup: PythonSetup,
) -> PyreflyDumpConfig:
    field_sets = tuple(
        PyreflyFieldSet.create(tgt)
        for tgt in targets
        if PyreflyFieldSet.is_applicable(tgt) and not PyreflyFieldSet.opt_out(tgt)
    )
    if not field_sets:
        logger.warning("No Pyrefly-applicable targets in scope.")
        return PyreflyDumpConfig(exit_code=0)

    partitions = list(
        await pyrefly_determine_partitions(PyreflyRequest(field_sets), **implicitly())
    )
    processes = await concurrently(
        _setup_pyrefly_process(
            partition,
            pyrefly,
            platform,
            python_setup,
            # `dump-config` reports the config it would apply to the given files, mirroring what
            # `check` sees; the argfile of files is passed just as it is for `check`.
            subcommand=("dump-config",),
            cache_scope=ProcessCacheScope.PER_SESSION,
        )
        for partition in partitions
    )
    results = await concurrently(execute_process(process, **implicitly()) for process in processes)

    label_partitions = len(partitions) > 1
    exit_code = 0
    for partition, result in zip(partitions, results):
        if result.exit_code != 0:
            # `dump-config` is expected to exit 0; a nonzero code is a Pyrefly tool failure.
            logger.error(
                f"Pyrefly `dump-config` failed (exit {result.exit_code}) on partition "
                f"({partition.description()}):\n{result.stderr.decode(errors='replace')}"
            )
            exit_code = result.exit_code
            continue
        if label_partitions:
            console.print_stdout(f"# {partition.description()}")
        console.print_stdout(result.stdout.decode(errors="replace").rstrip())
    return PyreflyDumpConfig(exit_code=exit_code)


def rules() -> Iterable[Rule | UnionRule]:
    return (*collect_rules(),)
