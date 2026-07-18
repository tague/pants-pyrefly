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
        version="0.3.0",
        description="A Pants plugin that runs the Pyrefly type checker in the `check` goal.",
        long_description_content_type="text/markdown",
        license="Apache-2.0",
        # Pure-Python (`py3-none-any`), so it runs on the interpreter of any supported Pants:
        # 2.27 (CPython 3.11) through 2.32 (CPython 3.14). Floor is decoupled from the build IC.
        python_requires=">=3.11",
        classifiers=[
            "Intended Audience :: Developers",
            "License :: OSI Approved :: Apache Software License",
            "Operating System :: MacOS :: MacOS X",
            "Operating System :: POSIX :: Linux",
            "Programming Language :: Python",
            "Programming Language :: Python :: 3 :: Only",
            "Programming Language :: Python :: 3.11",
            "Programming Language :: Python :: 3.12",
            "Programming Language :: Python :: 3.13",
            "Programming Language :: Python :: 3.14",
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
