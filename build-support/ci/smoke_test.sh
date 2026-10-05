#!/usr/bin/env bash
# Consumption smoke test for a single Pants version.
#
# Builds an isolated throwaway project that loads pants-pyrefly from source (via `pythonpath`,
# the way an in-repo plugin is consumed) and runs `pants check`, asserting that a clean file
# passes and a broken file fails with Pyrefly's `missing-import` error for its missing module (any
# other failure, such as the plugin not loading or Pants not starting, fails the test). This
# exercises the version-conditional rules-API shim end to end on whatever PANTS_VERSION is
# requested, without needing per-version dev lockfiles.
#
# Everything Pants executes lands in the script's own work directory: each run gets its own
# `--local-execution-root-dir` there (sandboxes and `immutable_inputs*`), so cleanup is removing
# that one directory and nothing is left in $TMPDIR.
#
# Usage: PANTS_VERSION=2.27.1 build-support/ci/smoke_test.sh
set -euo pipefail

PANTS_VERSION="${PANTS_VERSION:?set PANTS_VERSION, e.g. 2.27.1}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PLUGIN_SRC="${REPO_ROOT}/pants-plugins/pants_pyrefly"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/pyrefly-smoke.XXXXXX")"
PROJECT="${WORK}/project"
cleanup() {
  local status=$?
  # Pants makes immutable inputs (the Pyrefly binary) read-only; make them deletable first.
  chmod -R u+w "$WORK" 2>/dev/null || true
  rm -rf "$WORK"
  exit "$status"
}
trap cleanup EXIT

mkdir -p "$PROJECT/pants-plugins/pants_pyrefly" "$PROJECT/src"
cp "$PLUGIN_SRC"/{__init__,subsystems,skip_field,rules,register,goals}.py \
  "$PROJECT/pants-plugins/pants_pyrefly/"

cat > "$PROJECT/pants.toml" <<EOF
[GLOBAL]
pants_version = "${PANTS_VERSION}"
pythonpath = ["%(buildroot)s/pants-plugins"]
backend_packages = ["pants.backend.python", "pants_pyrefly"]

[python]
interpreter_constraints = ["CPython>=3.11,<3.15"]

[python-repos]
indexes = ["https://pypi.org/simple/"]
EOF

MISSING_MODULE="module_that_truly_does_not_exist_pyrefly_smoke"
printf 'def add(a: int, b: int) -> int:\n    return a + b\n' > "$PROJECT/src/good.py"
printf 'import %s\n' "$MISSING_MODULE" > "$PROJECT/src/bad.py"
echo 'python_sources()' > "$PROJECT/src/BUILD"

cd "$PROJECT"

fail() {
  echo "SMOKE FAILED [Pants ${PANTS_VERSION}]: $*" >&2
  exit 1
}

OUT=""
RUN=0
# Run Pants in a fresh execution root under $WORK; capture combined output in $OUT and return
# Pants's status.
run_pants() {
  local status=0 exec_root
  RUN=$((RUN + 1))
  exec_root="${WORK}/exec-${RUN}"
  mkdir -p "$exec_root"
  OUT="$(pants --no-pantsd "--local-execution-root-dir=${exec_root}" "$@" 2>&1)" ||
    status=$?
  return "$status"
}

echo "== [Pants ${PANTS_VERSION}] good.py must PASS =="
run_pants check src/good.py || fail "check failed on good.py:
$OUT"

echo "== [Pants ${PANTS_VERSION}] bad.py must FAIL with missing-import =="
if run_pants check src/bad.py; then
  fail "check passed on bad.py, expected a missing-import error:
$OUT"
fi
# A non-zero exit alone proves nothing (the plugin failing to load, or Pants failing to start, also
# exits non-zero): require Pyrefly's own error at its own location (`--> src/bad.py:1:...`; Pants's
# dependency-inference warning also names the file and module), and the check goal's summary line
# for the pyrefly checker (`✕ pyrefly failed.` on older Pants, `✕ pyrefly (['<constraints>'])
# failed in 1.2s.` on newer), which only appears if the plugin's rules ran Pyrefly.
grep -q 'missing-import' <<<"$OUT" || fail "no missing-import error in the output:
$OUT"
grep -q "$MISSING_MODULE" <<<"$OUT" || fail "the error does not name $MISSING_MODULE:
$OUT"
grep -q -- '--> src/bad.py:1:' <<<"$OUT" ||
  fail "the error is not reported at the real path src/bad.py:
$OUT"
grep -Eq '^[^ ]+ pyrefly( \(.*\))? failed' <<<"$OUT" ||
  fail "no pyrefly summary line in the output, so Pyrefly did not run:
$OUT"

echo "SMOKE OK: pants-pyrefly loads and runs on Pants ${PANTS_VERSION}"
