# Copyright 2026 Tague Griffith
# Licensed under the Apache License, Version 2.0 (see LICENSE).

python_sources(
    resolve="pants-plugins",
    interpreter_constraints=["==3.14.*"],
    # We don't (yet) run Pyrefly on the plugin's own sources, which lean heavily on the
    # `pantsbuild.pants` API. Verification happens via the integration tests instead.
    skip_pyrefly=True,
)

python_tests(
    name="tests",
    resolve="pants-plugins",
    interpreter_constraints=["==3.14.*"],
    skip_pyrefly=True,
)

python_distribution(
    name="dist",
    dependencies=[":pants_pyrefly"],
    interpreter_constraints=["==3.14.*"],
    provides=python_artifact(
        name="pants-pyrefly",
        version="0.1.0",
        description="A Pants plugin that runs the Pyrefly type checker in the `check` goal.",
        long_description_content_type="text/markdown",
        license="Apache-2.0",
        classifiers=[
            "Intended Audience :: Developers",
            "License :: OSI Approved :: Apache Software License",
            "Operating System :: MacOS :: MacOS X",
            "Operating System :: POSIX :: Linux",
            "Programming Language :: Python",
            "Programming Language :: Python :: 3 :: Only",
            "Topic :: Software Development :: Build Tools",
        ],
        keywords=["pants", "pantsbuild", "pants-plugin", "pyrefly", "typecheck"],
    ),
    entry_points={
        "pantsbuild.plugin": {
            "rules": "pants_pyrefly.register:rules",
        },
    },
    long_description_path="README.md",
    repositories=["@pypi"],
    wheel=True,
    sdist=True,
)
