# Copyright 2026 Tague Griffith
# Licensed under the Apache License, Version 2.0 (see LICENSE).

# Stdlib-only maintenance scripts. Not part of the published plugin (they live outside the
# `pants_pyrefly` package), so they never enter the wheel.

python_sources(name="bin")

python_tests(
    name="tests",
    # The generator/test run outside a Pants sandbox in CI (plain `python3`); the test itself is
    # pure-stdlib + pytest and needs no network.
    skip_pyrefly=True,
)
