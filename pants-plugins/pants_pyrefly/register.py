# Copyright 2026 Tague Griffith
# Licensed under the Apache License, Version 2.0 (see LICENSE).

"""A Pants plugin that runs the Pyrefly type checker (https://pyrefly.org) in the `check` goal."""

from __future__ import annotations

from collections.abc import Iterable

from pants_pyrefly import goals
from pants_pyrefly import rules as pyrefly_rules
from pants_pyrefly import skip_field, subsystems
from pants.engine.rules import Rule
from pants.engine.unions import UnionRule


def rules() -> Iterable[Rule | UnionRule]:
    return (
        *pyrefly_rules.rules(),
        *goals.rules(),
        *skip_field.rules(),
        *subsystems.rules(),
    )
