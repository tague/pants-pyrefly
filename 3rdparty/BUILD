# Copyright 2026 Tague Griffith
# Licensed under the Apache License, Version 2.0 (see LICENSE).

# Generates `pantsbuild.pants` and `pantsbuild.pants.testutil` requirements (in the
# `pants-plugins` resolve) pinned to a range compatible with the `pants_version` in pants.toml.
pants_requirements(
    name="pants",
    resolve="pants-plugins",
    # Target the whole 2.33 minor line (matching `pants_version`) rather than an exact patch, so
    # the dev resolve accepts any Pants 2.33.x (the plugin API is stable within a minor version).
    # The published wheel carries no `pantsbuild.pants` requirement (see pants_pyrefly:dist).
    version_spec="==2.33.*",
)
