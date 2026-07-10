#!/usr/bin/env bash
# Consumption smoke test for a single Pants version.
#
# Builds an isolated throwaway project that loads pants-pyrefly from source (via `pythonpath`,
# the way an in-repo plugin is consumed) and runs `pants check`, asserting that a clean file
# passes and a broken file fails. This exercises the version-conditional rules-API shim end to
# end on whatever PANTS_VERSION is requested, without needing per-version dev lockfiles.
#
# Usage: PANTS_VERSION=2.27.0 build-support/ci/smoke_test.sh
set -euo pipefail

PANTS_VERSION="${PANTS_VERSION:?set PANTS_VERSION, e.g. 2.27.0}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PLUGIN_SRC="${REPO_ROOT}/pants-plugins/pants_pyrefly"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/pyrefly-smoke.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

mkdir -p "$WORK/pants-plugins/pants_pyrefly" "$WORK/src"
cp "$PLUGIN_SRC"/{__init__,subsystems,skip_field,rules,register,goals}.py \
  "$WORK/pants-plugins/pants_pyrefly/"

cat > "$WORK/pants.toml" <<EOF
[GLOBAL]
pants_version = "${PANTS_VERSION}"
pythonpath = ["%(buildroot)s/pants-plugins"]
backend_packages = ["pants.backend.python", "pants_pyrefly"]

[python]
interpreter_constraints = ["CPython>=3.11,<3.15"]

[python-repos]
indexes = ["https://pypi.org/simple/"]
EOF

printf 'def add(a: int, b: int) -> int:\n    return a + b\n' > "$WORK/src/good.py"
printf 'import module_that_truly_does_not_exist_pyrefly_smoke\n' > "$WORK/src/bad.py"
echo 'python_sources()' > "$WORK/src/BUILD"

cd "$WORK"

echo "== [Pants ${PANTS_VERSION}] good.py must PASS =="
pants --no-pantsd check src/good.py

echo "== [Pants ${PANTS_VERSION}] bad.py must FAIL =="
if pants --no-pantsd check src/bad.py; then
  echo "SMOKE FAILED: expected 'check' to fail on bad.py, but it passed" >&2
  exit 1
fi

echo "SMOKE OK: pants-pyrefly loads and runs on Pants ${PANTS_VERSION}"
