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
#   4. `pyrefly-suppress`, then `check`, passes.
#
# Every Pyrefly process is forced to actually run (`--no-local-cache`) with its sandbox preserved,
# and the script asserts that the binary in each preserved sandbox reports `pyrefly <version>`, so
# it fails loudly if any other Pyrefly executed.
#
# Everything Pants executes lands in the script's own work directory: each run gets its own
# `--local-execution-root-dir` there (sandboxes and `immutable_inputs*`), so cleanup is removing
# that one directory and nothing is left in $TMPDIR.
#
# Usage: build-support/ci/compat_test.sh 1.2.0
#   PANTS_VERSION                  overrides the Pants version (default: this repo's pants.toml).
#   COMPAT_PANTS_PYREFLY_VERSION   self-test only: the version actually passed to Pants, while the
#                                  script still expects <version>. Pointing it at a different
#                                  release must make the script fail on the version assertion.
set -euo pipefail

PYREFLY_VERSION="${1:?usage: compat_test.sh <pyrefly-version>}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PLUGIN_SRC="${REPO_ROOT}/pants-plugins/pants_pyrefly"
PANTS_VERSION="${PANTS_VERSION:-$(sed -n 's/^pants_version = "\(.*\)"$/\1/p' "${REPO_ROOT}/pants.toml")}"
PANTS_PYREFLY_VERSION_ARG="${COMPAT_PANTS_PYREFLY_VERSION:-$PYREFLY_VERSION}"

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

LABEL="[Pyrefly ${PYREFLY_VERSION}, Pants ${PANTS_VERSION}]"
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
interpreter_constraints = ["CPython>=3.11,<3.15"]

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
  OUT="$(pants --no-pantsd --no-local-cache --keep-sandboxes=always \
    "--local-execution-root-dir=${EXEC_ROOT}" \
    "--pyrefly-version=${PANTS_PYREFLY_VERSION_ARG}" "$@" 2>&1)" ||
    status=$?
  return "$status"
}

# Assert, from the preserved sandboxes of the run that just finished, that every Pyrefly process
# executed the requested version. Runs before cleanup: the binaries live under $EXEC_ROOT.
assert_ran_requested_version() {
  local count=0 run_sh dir ran
  for run_sh in "$EXEC_ROOT"/pants-sandbox-*/__run.sh; do
    [[ -f "$run_sh" ]] || continue
    grep -q '__pyrefly_tool/pyrefly' "$run_sh" || continue
    dir="$(dirname "$run_sh")"
    ran="$("$dir/__pyrefly_tool/pyrefly" --version 2>&1)" ||
      fail "could not run the Pyrefly binary preserved in $dir: $ran"
    [[ "$ran" == "pyrefly ${PYREFLY_VERSION}" ]] ||
      fail "expected 'pyrefly ${PYREFLY_VERSION}', but this run executed '${ran}' (sandbox $dir)"
    count=$((count + 1))
  done
  ((count > 0)) || fail "no Pyrefly process sandbox was preserved, so the version is unproven:
$OUT"
  echo "   verified: ${count} Pyrefly process(es) ran 'pyrefly ${PYREFLY_VERSION}'"
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

echo "COMPAT OK ${LABEL}"
