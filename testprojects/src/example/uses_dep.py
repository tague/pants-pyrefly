"""Imports a third-party dependency, to verify Pyrefly resolves `site-packages`.

If Pyrefly could not see the resolved third-party requirements, it would report a
"missing import" error for `typing_extensions`.
"""

from __future__ import annotations

from typing_extensions import assert_type


def double(x: int) -> int:
    return x * 2


assert_type(double(21), int)
