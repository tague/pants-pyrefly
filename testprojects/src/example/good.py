"""A correctly-typed module used to smoke-test the Pyrefly plugin end-to-end."""

from __future__ import annotations


def add(x: int, y: int) -> int:
    return x + y


def greet(name: str) -> str:
    return f"Hello, {name}!"


total: int = add(1, 2)
message: str = greet("world")
