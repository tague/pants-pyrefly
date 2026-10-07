# Copyright 2026 Tague Griffith
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import PurePath

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
from pants.backend.python.util_rules.pex_environment import find_pex_python
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
    AddPrefix,
    CreateDigest,
    Digest,
    DigestSubset,
    FileContent,
    GlobMatchErrorBehavior,
    MergeDigests,
    PathGlobs,
    RemovePrefix,
    Snapshot,
)

from pants_pyrefly.skip_field import SkipPyreflyField
from pants_pyrefly.subsystems import Pyrefly

try:
    # Pants >= 2.29 renamed this call-by-name rule.
    from pants.engine.internals.graph import resolve_coarsened_targets as coarsened_targets_get
except ImportError:
    # Pants < 2.29 (2.27, 2.28) — identical call signature, earlier name.
    from pants.engine.internals.graph import coarsened_targets as coarsened_targets_get
from pants.engine.intrinsics import (
    add_prefix,
    create_digest,
    digest_subset_to_digest,
    execute_process,
    get_digest_contents,
    merge_digests,
    path_globs_to_digest,
    path_globs_to_paths,
    remove_prefix,
)
from pants.engine.platform import Platform
from pants.engine.process import Process, ProcessCacheScope
from pants.engine.rules import Rule, collect_rules, concurrently, implicitly, rule
from pants.engine.target import CoarsenedTargets, CoarsenedTargetsRequest, FieldSet, Target
from pants.engine.unions import UnionRule
from pants.source.source_root import SourceRootsRequest, get_source_roots
from pants.util.frozendict import FrozenDict
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
    # True for exactly one partition per run (the first). Under `--error-stale-baseline`, baseline
    # entries for files that no longer exist belong to no partition; this one carries them so
    # Pyrefly reports them as stale once. See `_partition_baseline`.
    carries_orphan_baseline_entries: bool = False

    def description(self) -> str:
        ics = str(sorted(str(c) for c in self.interpreter_constraints))
        return f"{self.resolve_description}, {ics}" if self.resolve_description else ics


class PyreflyPartitions(Collection[PyreflyPartition]):
    pass


@rule(
    desc="Partition Pyrefly's input by resolve and interpreter constraints",
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
            carries_orphan_baseline_entries=(index == 0),
        )
        for index, ((resolve, interpreter_constraints), field_sets) in enumerate(
            sorted(resolve_and_interpreter_constraints_to_field_sets.items())
        )
    )


# The config the plugin writes into the sandbox when the project gives it none to pass: what Pyrefly
# itself falls back to when it finds no config (the `basic` preset). Any name but `pyproject.toml`
# is parsed as a `pyrefly.toml`.
_GENERATED_CONFIG = "__pyrefly_config.toml"
_GENERATED_CONFIG_CONTENT = b'preset = "basic"\n'

# Fixed sandbox path where `--update-baseline` writes the baseline; the update-baseline goal
# relocates it to the user's configured `[pyrefly].baseline` path on write-back.
_BASELINE_OUTPUT = "__pyrefly_baseline_out.json"

# Baseline-maintenance flags Pyrefly 1.3 added to `check`. Users pass them via `[pyrefly].args`.
_ERROR_STALE_BASELINE = "--error-stale-baseline"
_PRUNE_BASELINE = "--prune-baseline"


def _is_flag(arg: str, flag: str) -> bool:
    return arg == flag or arg.startswith(f"{flag}=")


def _has_flag(args: Iterable[str], flag: str) -> bool:
    return any(_is_flag(arg, flag) for arg in args)


# Pyrefly's `--error-stale-baseline` failure says to rerun with `--prune-baseline`, which Pants
# rejects; rewrite that advice in `check` output.
_PYREFLY_PRUNE_HINT = f"rerun with `{_PRUNE_BASELINE}` to update it"
_PANTS_PRUNE_HINT = "run `pants pyrefly-update-baseline ::` to update it"


class PyreflyArgsError(Exception):
    """`[pyrefly].args` contains a Pyrefly flag that cannot work under Pants."""


def validate_pyrefly_args(pyrefly: Pyrefly) -> None:
    """Fail fast on `[pyrefly].args` that would silently do nothing under Pants.

    `--prune-baseline` rewrites the `--baseline` file in place. Pyrefly runs in a sandbox on a
    per-partition copy of the baseline, so the pruned copy would be discarded and the user's file
    would never change. (`check` never writes to the workspace anyway, by Pants convention.)
    """
    if _has_flag(pyrefly.args, _PRUNE_BASELINE):
        raise PyreflyArgsError(
            softwrap(
                f"""
                `{_PRUNE_BASELINE}` in `[pyrefly].args` is not supported: Pyrefly would prune a
                temporary copy of the baseline inside the Pants sandbox, and `[pyrefly].baseline`
                would never change. Remove it from `[pyrefly].args`, then run
                `pants pyrefly-update-baseline ::` to regenerate the baseline from every target,
                which drops stale entries (it also records any current errors).
                """
            )
        )


def _root_covers(root: str, dir_path: str) -> bool:
    """Whether `root` is `dir_path` or an ancestor of it, matching on path-segment boundaries."""
    if root in (".", ""):
        return True
    return dir_path == root or dir_path.startswith(root + "/")


def _dedupe_search_path_roots(
    source_roots: Iterable[str],
    source_files: Iterable[str],
    exclude: Iterable[str] = (),
) -> tuple[str, ...]:
    """Reduce Pants' source roots to the minimal set to hand Pyrefly as `--search-path`.

    Pants aggregates one source root per *target* (its BUILD-file directory), so a repo whose
    `[source] root_patterns` nests roots — e.g. both `src` and `src/python` — can surface an
    ancestor root (`src`) alongside a descendant (`src/python`). Emitting both makes every module
    under `src/python` reachable twice — as `pkg.mod` via `src/python` and as `python.pkg.mod` via
    `src` — which Pyrefly treats as two distinct types, producing spurious "not assignable to
    itself" errors.

    Mirror Pants' own one-root-per-file model: keep only the roots that are the nearest
    (longest-matching) root of an actual source file. An ancestor root that no file resolves to is
    dropped; one that a file genuinely needs is kept. If a kept root still shadows a nested kept
    root (a real layout collision — first-party code lives directly under the ancestor as well as
    under the descendant), warn rather than silently hide code; `[pyrefly].exclude_source_roots`
    can force-drop a root in that case.
    """
    excluded = set(exclude)
    candidates = sorted({root for root in source_roots if root not in excluded})

    needed: set[str] = set()
    for source_file in source_files:
        dir_path = os.path.dirname(source_file)
        nearest: str | None = None
        for root in candidates:
            if _root_covers(root, dir_path) and (nearest is None or len(root) > len(nearest)):
                nearest = root
        if nearest is not None:
            needed.add(nearest)

    emitted = sorted(needed)

    # Surface any remaining ancestor/descendant shadowing (a genuine layout collision we cannot
    # resolve without hiding code).
    for root in emitted:
        shadowed = [other for other in emitted if other != root and _root_covers(root, other)]
        if shadowed:
            logger.warning(
                softwrap(
                    f"""
                    Pyrefly source root `{root}` is an ancestor of
                    {", ".join(f"`{s}`" for s in shadowed)}; modules under the nested root(s) are
                    importable under two names and may produce spurious duplicate-module errors.
                    This happens when first-party code lives directly under `{root}` as well as
                    under a nested source root. Consolidate the layout, or drop a root with
                    `[pyrefly].exclude_source_roots`.
                    """
                )
            )

    return tuple(emitted)


@dataclass(frozen=True)
class _RestagedSources:
    """Sources re-laid-out so that no source root nests inside another.

    See `_restage_sources_by_root`.
    """

    digest: Digest
    search_paths: tuple[str, ...]
    real_to_synth: FrozenDict[str, str]
    synth_root_to_real: FrozenDict[str, str]


def _plan_restage(
    files: Iterable[str], root_of: dict[str, str]
) -> tuple[dict[str, str], dict[str, str], tuple[str, ...]]:
    """Pure planning for `_restage_sources_by_root` (extracted so it is unit-testable).

    Given each file's source root, assign every distinct root a unique sibling directory
    `__pyrefly_root_<n>` — indexed by sorted root path, so naming is deterministic and can never
    collide across roots — and compute each file's staged path. The build root (`.`/``) contributes
    no prefix, so its files keep their full repo path (e.g. `scripts/x.py`); other roots have their
    prefix stripped (e.g. `src/python/pkg/m.py` -> `pkg/m.py`). Because the synthetic dirs are
    siblings, no file is reachable under more than one of them — the property that gives each file a
    single module identity. Returns `(root_to_synth, real_to_synth, search_paths)`.
    """
    distinct_roots = sorted(set(root_of.values()))
    root_to_synth = {root: f"__pyrefly_root_{i}" for i, root in enumerate(distinct_roots)}
    real_to_synth: dict[str, str] = {}
    for f in files:
        root = root_of[f]
        rel = f if root in (".", "") else f[len(root) + 1 :]
        real_to_synth[f] = f"{root_to_synth[root]}/{rel}"
    search_paths = tuple(root_to_synth[root] for root in distinct_roots)
    return root_to_synth, real_to_synth, search_paths


async def _restage_sources_by_root(sources: Snapshot) -> _RestagedSources:
    """Stage each Pants source root's files under its own isolated, non-nesting synthetic directory.

    Pants gives every file exactly one source root (its nearest/longest-matching `root_pattern`),
    but Pyrefly makes a file reachable as a module under *every* `--search-path` that physically
    contains it. When source roots nest — the repo default has `.` as an ancestor of `src` and
    `src/python` — a file under the descendant is also reachable via the ancestor, so Pyrefly mints
    two module identities for it (`pkg.mod` and `src.python.pkg.mod`) and reports a spurious
    `bad-argument-type` where a value of one identity flows into a parameter typed with the other.
    `_dedupe_search_path_roots` cannot fix this: it cannot drop a root that a file genuinely roots
    at (e.g. the build root when `scripts/` roots there).

    Fix it structurally: subset each source root's files, strip the root prefix, and re-place them
    under a unique sibling dir `__pyrefly_root_<n>` handed to Pyrefly as a single `--search-path`.
    Sibling dirs never nest, so every file is reachable under exactly one identity, regardless of
    dependency topology. Callers must also pass `--disable-search-path-heuristics` so Pyrefly does
    not append the sandbox cwd as an implicit root and re-introduce nesting.
    """
    files = sources.files
    source_roots = await get_source_roots(SourceRootsRequest.for_files(files))
    root_of: dict[str, str] = {}
    for f in files:
        sr = source_roots.path_to_root.get(PurePath(f))
        # Every file has at least the build root ("." via "/"); default defensively.
        root_of[f] = sr.path if sr is not None else "."

    root_to_synth, real_to_synth, search_paths = _plan_restage(files, root_of)

    staged_digests: list[Digest] = []
    for root, synth in root_to_synth.items():
        root_files = [f for f in files if root_of[f] == root]
        subset = await digest_subset_to_digest(DigestSubset(sources.digest, PathGlobs(root_files)))
        # The build root ("."/"") has no prefix to strip; its files keep their full repo path.
        stripped = subset if root in (".", "") else await remove_prefix(RemovePrefix(subset, root))
        staged_digests.append(await add_prefix(AddPrefix(stripped, synth)))

    merged = await merge_digests(MergeDigests(tuple(staged_digests)))
    return _RestagedSources(
        digest=merged,
        search_paths=search_paths,
        real_to_synth=FrozenDict(real_to_synth),
        synth_root_to_real=FrozenDict({synth: root for root, synth in root_to_synth.items()}),
    )


@dataclass(frozen=True)
class PyreflyProcess:
    """A Pyrefly `Process` plus the mapping needed to translate its output back to real paths.

    When sources are re-staged (see `_restage_sources_by_root`), Pyrefly reports files under
    synthetic `__pyrefly_root_<n>` dirs. `synth_root_to_real` maps each synthetic root back to
    its real Pants source root so callers can un-map diagnostics, baseline entries, or edited-source
    digests. It is empty when sources were not re-staged.
    """

    process: Process
    synth_root_to_real: FrozenDict[str, str]


def _real_prefix(root: str) -> str:
    """The path prefix a real source root contributes to a module file ("" for the build root)."""
    return "" if root in (".", "") else f"{root}/"


def _remap_text(text: str, synth_root_to_real: FrozenDict[str, str]) -> str:
    """Rewrite every synthetic root in Pyrefly's output `text` back to its real repo path.

    Pyrefly names a synthetic root in two shapes: as the leading segment of a file path
    (`__pyrefly_root_<n>/pkg/m.py`, e.g. an error location), and on its own (e.g. the
    `Search path override` line of a missing-import hint, where it is the last segment of an
    absolute sandbox path). The former maps to the file's real repo path (`src/python/pkg/m.py`;
    the build root adds no prefix); the latter to the real source root itself (`src/python`, or
    `.` for the build root).

    Only the synthetic names generated for this invocation are rewritten, and only as whole path
    segments: a name must not be preceded or followed by a word character, `.`, or `-`, so
    `__pyrefly_root_1` is left alone in `__pyrefly_root_10`, `my__pyrefly_root_1`,
    `a.__pyrefly_root_1`, and `__pyrefly_root_1.sub`. The one exception is a name directly after an
    ANSI color code (`\x1b[...m`, e.g. under `--color=always`), whose final `m` would otherwise
    look like part of a word; the code is kept as-is.
    """
    if not synth_root_to_real:
        return text
    # Longest name first, so no name can win over a longer one it prefixes.
    names = "|".join(re.escape(s) for s in sorted(synth_root_to_real, key=len, reverse=True))
    # Segment start: either an ANSI SGR sequence (any parameters, e.g. `\x1b[0m`, `\x1b[1;31m`,
    # `\x1b[38;5;12m`), matched and put back, or no name character or `.` before the name. Segment
    # end: `/` (a path under the root), or nothing that could continue a name.
    pattern = re.compile(rf"(?:(\x1b\[[0-9;:]*m)|(?<![\w.-]))({names})(?:(/)|(?![\w.-]))")

    def to_real(match: re.Match[str]) -> str:
        sgr, synth, slash = match.groups()
        root = synth_root_to_real[synth]
        real = _real_prefix(root) if slash else ("." if root in (".", "") else root)
        return f"{sgr or ''}{real}"

    return pattern.sub(to_real, text)


def _remap_path(path: str, synth_root_to_real: FrozenDict[str, str]) -> str:
    """Rewrite a single staged path (`__pyrefly_root_<n>/rel`) back to its real repo path."""
    for synth, root in synth_root_to_real.items():
        prefix = f"{synth}/"
        if path.startswith(prefix):
            return f"{_real_prefix(root)}{path[len(prefix) :]}"
    return path


async def _unstage_digest(digest: Digest, synth_root_to_real: FrozenDict[str, str]) -> Digest:
    """Reverse `_restage_sources_by_root`: move files from `__pyrefly_root_<n>/…` to real paths.

    Used by `suppress`, which edits the staged sources in place; the edited digest must be relocated
    to real paths before it is written back to the workspace.
    """
    if not synth_root_to_real:
        return digest
    parts: list[Digest] = []
    for synth, root in synth_root_to_real.items():
        subset = await digest_subset_to_digest(DigestSubset(digest, PathGlobs([f"{synth}/**"])))
        stripped = await remove_prefix(RemovePrefix(subset, synth))
        parts.append(stripped if root in (".", "") else await add_prefix(AddPrefix(stripped, root)))
    return await merge_digests(MergeDigests(tuple(parts)))


def _filter_baseline_entries(
    errors: Iterable, root_real_to_synth: dict[str, str], orphan_paths: frozenset[str]
) -> list:
    """Select (and path-remap) the baseline entries one partition's Pyrefly run should see.

    Pure planning for `_partition_baseline` (extracted so it is unit-testable):
      - entries for the partition's own files (keys of `root_real_to_synth`) are kept, with their
        real repo path rewritten to the staged path Pyrefly reports;
      - entries whose path is in `orphan_paths` (files that no longer exist) are kept as-is;
      - entries without a string `path` are passed through untouched, for Pyrefly to judge;
      - every other entry (a file checked by another partition, or not checked at all) is dropped.
    """
    kept: list = []
    for error in errors:
        path = error.get("path") if isinstance(error, dict) else None
        if not isinstance(path, str):
            kept.append(error)
        elif path in root_real_to_synth:
            kept.append({**error, "path": root_real_to_synth[path]})
        elif path in orphan_paths:
            kept.append(error)
    return kept


def _globbable(path: str) -> bool:
    """Whether `path` can be looked up as a literal relative path with `PathGlobs`."""
    return bool(path) and not (
        os.path.isabs(path)
        or path.startswith("!")
        or any(c in path for c in "*?[]{}")
        or ".." in path.split("/")
    )


async def _partition_baseline(
    baseline_digest: Digest,
    baseline_path: str,
    root_real_to_synth: dict[str, str],
    *,
    include_orphans: bool,
) -> Digest:
    """Give one partition's Pyrefly run only the baseline entries for its own files.

    `pyrefly-update-baseline` writes one merged baseline at real repo paths, but each partition's
    Pyrefly runs on re-staged sources and reports (and matches `--baseline` against) synthetic
    `__pyrefly_root_<n>/…` paths. Entries for this partition's files are remapped to those paths so
    gating still suppresses them.

    All other entries are dropped. Pyrefly (>= 1.3) treats a baseline entry as stale when its file
    is checked and no longer has the error, or when its file does not exist. In the sandbox, an
    entry for a file another partition checks names a path that does not exist, so passing it
    through made `--error-stale-baseline` fail every partition on the others' entries. Dropping
    them does not change which errors are gated, since an entry only matches errors in its own
    file and Pyrefly reports errors only for the files it is asked to check.

    With `include_orphans` (one partition per run, under `--error-stale-baseline`), entries for
    files that do not exist in the repo are passed through at their real path, which does not
    exist in the sandbox either, so Pyrefly reports them as stale exactly once.
    """
    contents = await get_digest_contents(baseline_digest)
    out: list[FileContent] = []
    for file_content in contents:
        if file_content.path != baseline_path or not file_content.content.strip():
            out.append(file_content)
            continue
        data = json.loads(file_content.content)
        errors = data.get("errors", []) if isinstance(data, dict) else None
        if not isinstance(errors, list):
            # Not a shape we understand; let Pyrefly report on it.
            out.append(file_content)
            continue
        orphan_paths: frozenset[str] = frozenset()
        if include_orphans:
            candidates = sorted(
                {
                    error["path"]
                    for error in errors
                    if isinstance(error, dict)
                    and isinstance(error.get("path"), str)
                    and error["path"] not in root_real_to_synth
                    and _globbable(error["path"])
                }
            )
            if candidates:
                existing = await path_globs_to_paths(
                    PathGlobs(candidates, glob_match_error_behavior=GlobMatchErrorBehavior.ignore)
                )
                orphan_paths = frozenset(candidates) - set(existing.files)
        data["errors"] = _filter_baseline_entries(errors, root_real_to_synth, orphan_paths)
        out.append(FileContent(baseline_path, json.dumps(data, indent=2).encode()))
    return await create_digest(CreateDigest(out))


def _has_config_flag(args: Iterable[str]) -> bool:
    """Whether `args` sets Pyrefly's config: `--config x`, `--config=x`, `-c x` or `-cx`."""
    return any(_is_flag(arg, "--config") or arg.startswith("-c") for arg in args)


def _discovered_config(files: Iterable[str]) -> str | None:
    """The discovered config Pyrefly itself would use: `pyrefly.toml` wins over `pyproject.toml`."""
    for name in ("pyrefly.toml", "pyproject.toml"):
        if name in files:
            return name
    return None


async def _setup_pyrefly_process(
    partition: PyreflyPartition,
    pyrefly: Pyrefly,
    platform: Platform,
    python_setup: PythonSetup,
    *,
    subcommand: tuple[str, ...] = ("check",),
    subcommand_args: tuple[str, ...] = (),
    update_baseline: bool = False,
    capture_root_sources: bool = False,
    cache_scope: ProcessCacheScope,
) -> PyreflyProcess:
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
    # The venv's Python shim runs the venv out of the sandbox's `.cache/pex_root`, rebuilding it
    # there if it is missing. Pants' own `VenvPexProcess` mounts the shared `pex_root` named cache
    # at that path; we build our `Process` by hand, so mount it ourselves. Without it, every run
    # rebuilds the entire third-party venv before Pyrefly starts.
    pex_environment = await find_pex_python(**implicitly())

    is_check = subcommand == ("check",)

    # Re-stage sources so no source root nests inside another, eliminating the dual-module-identity
    # false positives at whole-repo scope. Applied to the error-reporting subcommands
    # (`check`, including `--update-baseline`, and `suppress`); `coverage`/`dump-config` don't need
    # it. `synth_root_to_real` (empty when not re-staged) lets callers map synthetic output
    # paths back to real repo paths.
    stage_sources = subcommand in (("check",), ("suppress",))
    if stage_sources:
        restaged = await _restage_sources_by_root(transitive_sources.source_files.snapshot)
        reported_files = [restaged.real_to_synth[f] for f in root_sources.snapshot.files]
        source_digests: tuple[Digest, ...] = (restaged.digest,)
        # Non-nesting synthetic roots; disable the heuristic import-root so Pyrefly cannot append
        # the sandbox cwd and re-expose files under a second identity.
        search_path_args = [f"--search-path={p}" for p in restaged.search_paths]
        search_path_args.append("--disable-search-path-heuristics")
        synth_root_to_real = restaged.synth_root_to_real
        real_to_synth = restaged.real_to_synth
        logger.debug("Pyrefly re-staged source roots: %s", dict(synth_root_to_real))
    else:
        reported_files = list(root_sources.snapshot.files)
        source_digests = (
            root_sources.snapshot.digest,
            transitive_sources.source_files.snapshot.digest,
        )
        # Nearest-root-deduped real roots (cannot drop a genuinely-nesting ancestor like the build
        # root; that is what the re-staging path above is for).
        search_path_args = [
            f"--search-path={source_root}"
            for source_root in _dedupe_search_path_roots(
                transitive_sources.source_roots,
                transitive_sources.source_files.snapshot.files,
                pyrefly.exclude_source_roots,
            )
        ]
        synth_root_to_real = FrozenDict[str, str]({})
        real_to_synth = FrozenDict[str, str]({})

    # Baseline handling (check only). In `update` mode we (re)write a baseline to a fixed sandbox
    # path and capture it; otherwise we materialize the user's baseline (if set) and gate on it.
    baseline_args: list[str] = []
    baseline_digest = EMPTY_DIGEST
    output_files: tuple[str, ...] = ()
    if update_baseline:
        baseline_args = [f"--baseline={_BASELINE_OUTPUT}", "--update-baseline"]
        output_files = (_BASELINE_OUTPUT,)
    elif capture_root_sources:
        # `suppress` rewrites the checked files in place; capture them (at their staged paths, when
        # re-staged) so the goal can un-stage and write the edited sources back to the workspace.
        output_files = tuple(reported_files)
    elif is_check and pyrefly.baseline:
        baseline_digest = await path_globs_to_digest(
            PathGlobs(
                [pyrefly.baseline],
                glob_match_error_behavior=GlobMatchErrorBehavior.ignore,
            )
        )
        if baseline_digest != EMPTY_DIGEST:
            # Hand Pyrefly only this partition's entries, at the (re-staged) paths it reports, so
            # that gating and `--error-stale-baseline` are scoped to the partition's own files.
            baseline_digest = await _partition_baseline(
                baseline_digest,
                pyrefly.baseline,
                {f: real_to_synth.get(f, f) for f in root_sources.snapshot.files},
                include_orphans=(
                    partition.carries_orphan_baseline_entries
                    and _has_flag(pyrefly.args, _ERROR_STALE_BASELINE)
                ),
            )
            baseline_args = [f"--baseline={pyrefly.baseline}"]
        else:
            logger.warning(
                softwrap(
                    f"""
                    `[pyrefly].baseline` is set to `{pyrefly.baseline}`, but that file does not
                    exist. Run `pants pyrefly-update-baseline ::` to create it; checking without a
                    baseline for now.
                    """
                )
            )

    # Pass the files to check via an argfile rather than argv, so we never hit OS command-line
    # length limits on targets with many files (Pyrefly reads `@<file>`, like other clap CLIs).
    file_list_path = "__pyrefly_files.txt"
    generated_files = [FileContent(file_list_path, "\n".join(reported_files).encode())]

    # Pin one config for every file Pyrefly loads, as `pyrefly check` does in project mode and as
    # Pyrefly's own Bazel and Buck integrations do. Passing files explicitly otherwise makes Pyrefly
    # search upward from each file for its own config, and third-party files live in the
    # `pex_root` named cache outside the sandbox: they would get whatever config sits above that
    # cache, or one migrated from a `mypy.ini` a wheel ships, instead of the project's. An explicit
    # or discovered config is materialized into the input digest below; with neither, we generate
    # the config Pyrefly falls back to. A config the user passes in `[pyrefly].args` wins instead
    # (Pyrefly rejects a repeated `--config`), but those args only reach `check`.
    config_path: str | None = None
    if not (is_check and _has_config_flag(pyrefly.args)):
        config_path = pyrefly.config or _discovered_config(config_file_snapshot.snapshot.files)
        if not config_path:
            config_path = _GENERATED_CONFIG
            generated_files.append(FileContent(_GENERATED_CONFIG, _GENERATED_CONFIG_CONTENT))
    generated_digest = await create_digest(CreateDigest(generated_files))

    input_digest = await merge_digests(
        MergeDigests(
            (
                *source_digests,
                config_file_snapshot.snapshot.digest,
                requirements_venv_pex.digest,
                generated_digest,
                baseline_digest,
            )
        )
    )

    tool_key = "__pyrefly_tool"
    exe_path = os.path.normpath(os.path.join(tool_key, downloaded_pyrefly.exe))

    python_version = partition.interpreter_constraints.minimum_python_version(
        python_setup.interpreter_versions_universe
    )

    argv: list[str] = [exe_path, *subcommand]
    # First-party import roots (the analogue of MYPYPATH / sys.path). Computed above: non-nesting
    # synthetic roots on the `check` path, else nearest-root-deduped real roots.
    argv.extend(search_path_args)
    # Third-party deps + interpreter introspection.
    argv.append(f"--python-interpreter-path={requirements_venv_pex.python.argv0}")
    if python_version:
        argv.append(f"--python-version={python_version}")
    # The one config every file uses (see above).
    if config_path:
        argv.append(f"--config={config_path}")
    if is_check:
        # `check`-only flags; `coverage report`/`check` do not accept these.
        if pyrefly.output_format:
            argv.append(f"--output-format={pyrefly.output_format}")
        if pyrefly.min_severity:
            argv.append(f"--min-severity={pyrefly.min_severity}")
        argv.extend(f"--only={error_kind}" for error_kind in pyrefly.only)
        argv.extend(baseline_args)
        # User-provided args (can override any of the above). `--error-stale-baseline` is a gate
        # for `pants check`; Pyrefly rejects it alongside `--update-baseline`, so drop it when
        # regenerating rather than make `pyrefly-update-baseline` unusable for users who set it.
        argv.extend(
            arg
            for arg in pyrefly.args
            if not (update_baseline and _is_flag(arg, _ERROR_STALE_BASELINE))
        )
    # Subcommand-specific flags (e.g. `suppress --remove-unused`), for non-`check` subcommands.
    argv.extend(subcommand_args)
    # The files to report on, passed via the argfile created above.
    argv.append(f"@{file_list_path}")

    process = Process(
        argv=tuple(argv),
        input_digest=input_digest,
        immutable_input_digests={tool_key: downloaded_pyrefly.digest},
        append_only_caches={
            **pex_environment.in_sandbox(working_directory=None).append_only_caches,
            **(requirements_venv_pex.append_only_caches or {}),
        },
        output_files=output_files,
        description=f"Run Pyrefly on {pluralize(len(root_sources.snapshot.files), 'file')}.",
        level=LogLevel.DEBUG,
        cache_scope=cache_scope,
    )
    return PyreflyProcess(process=process, synth_root_to_real=synth_root_to_real)


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
    invocation = await _setup_pyrefly_process(
        partition,
        pyrefly,
        platform,
        python_setup,
        update_baseline=False,
        # `default_process_cache_scope` (which honors `--force`) exists on Pants >= 2.32;
        # on older versions fall back to the normal "cache successful runs" scope.
        cache_scope=getattr(
            check_subsystem, "default_process_cache_scope", ProcessCacheScope.SUCCESSFUL
        ),
    )
    process_result = await execute_process(invocation.process, **implicitly())
    # Exit 0 == clean, 1 == type errors found. Anything else (e.g. 3, or a 101 panic) is a Pyrefly
    # tool failure, not type errors — flag it so users don't misread a crash as code problems.
    if process_result.exit_code not in (0, 1):
        logger.warning(
            softwrap(
                f"""
                Pyrefly exited with code {process_result.exit_code} on partition
                ({partition.description()}). This usually indicates a Pyrefly tool error rather than
                type errors in your code; see the output above.
                """
            )
        )
    if invocation.synth_root_to_real:
        # Pyrefly reported synthetic re-staged paths (`__pyrefly_root_<n>/…`); translate diagnostics
        # back to real repo paths before surfacing them to the user. Also point Pyrefly's stale-
        # baseline hint at the goal that works under Pants (see `validate_pyrefly_args`).

        def rewrite(output: bytes) -> bytes:
            text = _remap_text(output.decode(errors="replace"), invocation.synth_root_to_real)
            return text.replace(_PYREFLY_PRUNE_HINT, _PANTS_PRUNE_HINT).encode()

        process_result = replace(
            process_result,
            stdout=rewrite(process_result.stdout),
            stderr=rewrite(process_result.stderr),
        )
    return CheckResult.from_fallible_process_result(
        process_result,
        partition_description=partition.description(),
    )


@rule(desc="Typecheck using Pyrefly", level=LogLevel.DEBUG)
async def pyrefly_typecheck(request: PyreflyRequest, pyrefly: Pyrefly) -> CheckResults:
    if pyrefly.skip:
        return CheckResults([], checker_name=request.tool_name)
    validate_pyrefly_args(pyrefly)

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
