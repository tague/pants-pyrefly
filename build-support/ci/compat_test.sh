#!/usr/bin/env bash
# Pyrefly compatibility test for a single Pyrefly version.
#
# Builds an isolated throwaway project that loads pants-pyrefly from source (like smoke_test.sh) and
# drives real Pants runs with `--pyrefly-version=<version>` -- only the version, never
# `known_versions`, so the plugin's shipped pins are what resolve the download. Each step must
# behave as expected:
#
#   1. `check` on a clean file passes;
#   2. `check` on a file with a missing import fails with that `missing-import` error;
#   3. `pyrefly-update-baseline`, then `check` gated on that baseline, passes;
#   4. `pyrefly-suppress`, then `check`, passes;
#   5. (only when the minimum Python is older than 3.10) `check` on a file using a `match` statement
#      fails with Pyrefly's `invalid-syntax` error for that Python version, proving Pyrefly really
#      checked the code as that version.
#
# Every Pyrefly process is forced to actually run (`--no-local-cache`) with its sandbox preserved,
# and the script asserts that the binary in each preserved sandbox reports `pyrefly <version>`, so
# it fails loudly if any other Pyrefly executed. It also asserts that each process was given
# `--python-version=<minimum Python>`, and that the interpreter it was pointed at
# (`--python-interpreter-path`) is at least that version.
#
# The plugin derives `--python-version` from the minimum of the partition's interpreter
# constraints, and builds the venv behind `--python-interpreter-path` (third-party packages) with
# an interpreter matching those constraints. So the machine needs a real interpreter that
# satisfies them: a `CPython==3.9.*` project needs Python 3.9 installed.
#
# Everything Pants executes lands in the script's own work directory: each run gets its own
# `--local-execution-root-dir` there (sandboxes and `immutable_inputs*`), so cleanup is removing
# that one directory and nothing is left in $TMPDIR.
#
# Usage: build-support/ci/compat_test.sh 1.2.0
#   PANTS_VERSION                  overrides the Pants version (default: this repo's pants.toml).
#   COMPAT_INTERPRETER_CONSTRAINTS the test project's `[python].interpreter_constraints`
#                                  (default: CPython>=3.11,<3.15).
#   COMPAT_PYTHON_VERSION          the minimum Python those constraints allow, i.e. the
#                                  `--python-version` the plugin must pass (default: 3.11). Set it
#                                  together with COMPAT_INTERPRETER_CONSTRAINTS.
#   COMPAT_PANTS_PYREFLY_VERSION   self-test only: the version actually passed to Pants, while the
#                                  script still expects <version>. Pointing it at a different
#                                  release must make the script fail on the version assertion.
set -euo pipefail

# Run Pants with a controlled configuration, so nothing from the developer's setup leaks into the
# throwaway project (e.g. `dynamic_ui = true` drops Pyrefly's diagnostics from the captured output
# on some Pants versions). Every run passes `--no-pantsrc` (no `/etc/pantsrc`, `~/.pants.rc`, or
# `.pants.rc` is read) and `--no-dynamic-ui`, and every inherited `PANTS_*` variable is unset here,
# for the whole script, except two that configure the scie-pants launcher rather than Pants:
# PANTS_VERSION (the Pants version to run) and PANTS_BOOTSTRAP_* (download mirrors and timeouts).
# Unsetting once up front is simpler than an `env` allowlist on every call, and leaves everything
# else (PATH, HOME, TMPDIR, SCIE_*, caches) as it is.
while IFS= read -r var; do
  unset "$var"
done < <(compgen -e | grep -E '^PANTS_' | grep -Ev '^(PANTS_VERSION|PANTS_BOOTSTRAP_.*)$' || true)

PYREFLY_VERSION="${1:?usage: compat_test.sh <pyrefly-version>}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PLUGIN_SRC="${REPO_ROOT}/pants-plugins/pants_pyrefly"
PANTS_VERSION="${PANTS_VERSION:-$(sed -n 's/^pants_version = "\(.*\)"$/\1/p' "${REPO_ROOT}/pants.toml")}"
PANTS_PYREFLY_VERSION_ARG="${COMPAT_PANTS_PYREFLY_VERSION:-$PYREFLY_VERSION}"
INTERPRETER_CONSTRAINTS="${COMPAT_INTERPRETER_CONSTRAINTS:-CPython>=3.11,<3.15}"
PYTHON_VERSION="${COMPAT_PYTHON_VERSION:-3.11}"
[[ "$PYTHON_VERSION" =~ ^3\.[0-9]+$ ]] ||
  { echo "COMPAT_PYTHON_VERSION must be 3.N, got '${PYTHON_VERSION}'" >&2; exit 2; }
PYTHON_MINOR="${PYTHON_VERSION#3.}"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/pyrefly-compat.XXXXXX")"
PROJECT="${WORK}/project"
cleanup() {
  local status=$?
  # Pants makes immutable inputs (the Pyrefly binary) read-only; make them deletable first.
  chmod -R u+w "$WORK" 2>/dev/null || true
  rm -rf "$WORK"
  exit "$status"
}
trap cleanup EXIT

LABEL="[Pyrefly ${PYREFLY_VERSION}, Pants ${PANTS_VERSION}, ${INTERPRETER_CONSTRAINTS}]"
step() { echo "== ${LABEL} $*"; }
fail() {
  echo "COMPAT FAILED ${LABEL}: $*" >&2
  exit 1
}

mkdir -p "$PROJECT/pants-plugins/pants_pyrefly" "$PROJECT/src"
cp "$PLUGIN_SRC"/{__init__,subsystems,skip_field,rules,register,goals}.py \
  "$PROJECT/pants-plugins/pants_pyrefly/"

cat >"$PROJECT/pants.toml" <<EOF
[GLOBAL]
pants_version = "${PANTS_VERSION}"
pythonpath = ["%(buildroot)s/pants-plugins"]
backend_packages = ["pants.backend.python", "pants_pyrefly"]

[python]
interpreter_constraints = ["${INTERPRETER_CONSTRAINTS}"]

[python-repos]
indexes = ["https://pypi.org/simple/"]
EOF

MISSING_MODULE="module_that_truly_does_not_exist_pyrefly_compat"
printf 'def add(a: int, b: int) -> int:\n    return a + b\n' >"$PROJECT/src/good.py"
printf 'import %s  # pants: no-infer-dep\n' "$MISSING_MODULE" >"$PROJECT/src/bad.py"
echo 'python_sources()' >"$PROJECT/src/BUILD"

cd "$PROJECT"

OUT=""
RUN=0
EXEC_ROOT=""
# Run Pants with the requested Pyrefly in a fresh execution root under $WORK; capture combined
# output in $OUT and return Pants's status.
run_pants() {
  local status=0
  RUN=$((RUN + 1))
  EXEC_ROOT="${WORK}/exec-${RUN}"
  mkdir -p "$EXEC_ROOT"
  OUT="$(pants --no-pantsd --no-pantsrc --no-dynamic-ui --no-local-cache --keep-sandboxes=always \
    "--local-execution-root-dir=${EXEC_ROOT}" \
    "--pyrefly-version=${PANTS_PYREFLY_VERSION_ARG}" "$@" 2>&1)" ||
    status=$?
  return "$status"
}

# Assert, from the preserved sandboxes of the run that just finished, that every Pyrefly process
# executed the requested version, was told `--python-version=$PYTHON_VERSION`, and was pointed at an
# interpreter of at least that version. Runs before cleanup: the binaries live under $EXEC_ROOT.
assert_ran_requested_version() {
  local count=0 run_sh dir ran interp interp_version
  local interpreters=()
  for run_sh in "$EXEC_ROOT"/pants-sandbox-*/__run.sh; do
    [[ -f "$run_sh" ]] || continue
    grep -q '__pyrefly_tool/pyrefly' "$run_sh" || continue
    dir="$(dirname "$run_sh")"
    ran="$("$dir/__pyrefly_tool/pyrefly" --version 2>&1)" ||
      fail "could not run the Pyrefly binary preserved in $dir: $ran"
    [[ "$ran" == "pyrefly ${PYREFLY_VERSION}" ]] ||
      fail "expected 'pyrefly ${PYREFLY_VERSION}', but this run executed '${ran}' (sandbox $dir)"
    grep -Eq -- "--python-version=${PYTHON_VERSION//./\\.}([^0-9]|\$)" "$run_sh" ||
      fail "expected --python-version=${PYTHON_VERSION} in the Pyrefly command (sandbox $dir):
$(cat "$run_sh")"
    interp="$(grep -Eo -- "--python-interpreter-path=[^ '\"]+" "$run_sh" | head -n 1)"
    interp="${interp#--python-interpreter-path=}"
    [[ -n "$interp" ]] || fail "no --python-interpreter-path in the Pyrefly command (sandbox $dir)"
    interp_version="$(cd "$dir" && "$interp" -c \
      'import sys; print("%d.%d" % sys.version_info[:2])' 2>&1)" ||
      fail "could not run the interpreter Pyrefly was given ($interp, sandbox $dir):" \
        "$interp_version"
    [[ "$interp_version" =~ ^3\.([0-9]+)$ ]] && ((BASH_REMATCH[1] >= PYTHON_MINOR)) ||
      fail "Pyrefly was pointed at Python '${interp_version}', older than ${PYTHON_VERSION}" \
        "(sandbox $dir)"
    interpreters+=("$interp_version")
    count=$((count + 1))
  done
  ((count > 0)) || fail "no Pyrefly process sandbox was preserved, so the version is unproven:
$OUT"
  echo "   verified: ${count} Pyrefly process(es) ran 'pyrefly ${PYREFLY_VERSION}'" \
    "with --python-version=${PYTHON_VERSION} and interpreter Python ${interpreters[*]}"
}

step "1. check on a clean file must PASS"
run_pants check src/good.py || fail "check failed on good.py:
$OUT"
assert_ran_requested_version

step "2. check on a file with a missing import must FAIL with missing-import"
if run_pants check src/bad.py; then
  fail "check passed on bad.py, expected a missing-import error:
$OUT"
fi
grep -q 'missing-import' <<<"$OUT" || fail "no missing-import error in the output:
$OUT"
grep -q "$MISSING_MODULE" <<<"$OUT" || fail "the error does not name $MISSING_MODULE:
$OUT"
grep -q 'src/bad.py' <<<"$OUT" || fail "the error is not reported at the real path src/bad.py:
$OUT"
assert_ran_requested_version

step "3. pyrefly-update-baseline, then check gated on it, must PASS"
run_pants --pyrefly-baseline=pyrefly-baseline.json pyrefly-update-baseline src:: ||
  fail "pyrefly-update-baseline failed:
$OUT"
assert_ran_requested_version
python3 - "$MISSING_MODULE" <<'PY' || fail "the baseline does not record the bad.py error"
import json, sys
errors = json.load(open("pyrefly-baseline.json"))["errors"]
assert any(
    e.get("path") == "src/bad.py" and e.get("name") == "missing-import" for e in errors
), errors
PY
run_pants --pyrefly-baseline=pyrefly-baseline.json check src:: ||
  fail "check gated on the baseline failed:
$OUT"
assert_ran_requested_version
rm -f pyrefly-baseline.json

step "4. pyrefly-suppress, then check, must PASS"
run_pants pyrefly-suppress src:: || fail "pyrefly-suppress failed:
$OUT"
assert_ran_requested_version
grep -q 'pyrefly: ignore' src/bad.py || fail "pyrefly-suppress did not add an ignore comment:
$(cat src/bad.py)"
run_pants check src:: || fail "check after pyrefly-suppress failed:
$OUT"
assert_ran_requested_version

if ((PYTHON_MINOR < 10)); then
  step "5. check on a file with a match statement (Python 3.10+) must FAIL with invalid-syntax"
  mkdir -p syntax
  echo 'python_sources()' >syntax/BUILD
  printf '%s\n' 'def kind(value: object) -> str:' '    match value:' '        case _:' \
    '            return "any"' >syntax/uses_match.py
  if run_pants check syntax/uses_match.py; then
    fail "check passed on a match statement under Python ${PYTHON_VERSION}:
$OUT"
  fi
  grep -q 'invalid-syntax' <<<"$OUT" || fail "no invalid-syntax error in the output:
$OUT"
  grep -q "Python ${PYTHON_VERSION} " <<<"$OUT" ||
    fail "the invalid-syntax error does not name Python ${PYTHON_VERSION}:
$OUT"
  assert_ran_requested_version
fi

echo "COMPAT OK ${LABEL}"
