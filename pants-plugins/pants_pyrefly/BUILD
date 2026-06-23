# Copyright 2026 Tague Griffith
# Licensed under the Apache License, Version 2.0 (see LICENSE).

python_sources(
    resolve="pants-plugins",
    interpreter_constraints=["==3.14.*"],
)

python_tests(
    name="tests",
    resolve="pants-plugins",
    interpreter_constraints=["==3.14.*"],
    skip_pyrefly=True,
)

python_distribution(
    name="dist",
    # `pants_requirements()` leaks a `pantsbuild.pants` install-requirement into the wheel
    # (pantsbuild/pants#20729). Pants provides itself at runtime and is no longer on PyPI, so
    # exclude it transitively: the published wheel must carry no `pantsbuild.pants` dependency.
    dependencies=[":pants_pyrefly", "!!3rdparty:pants#pantsbuild.pants"],
    # Build under the dev resolve's interpreter (CPython 3.14 / Pants 2.32); `python_requires`
    # below sets the *wheel's* floor independently of this build-time constraint.
    interpreter_constraints=["==3.14.*"],
    provides=python_artifact(
        name="pants-pyrefly",
        version="0.1.0",
        description="A Pants plugin that runs the Pyrefly type checker in the `check` goal.",
        long_description_content_type="text/markdown",
        license="Apache-2.0",
        # Pure-Python plugin; wheel floor is CPython 3.12 (decoupled from the build IC above).
        # Pants 2.27 runs on CPython 3.11, below this floor — install from source there instead.
        python_requires=">=3.12",
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
