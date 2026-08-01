"""Shared, framework-free support for evaluation command adapters."""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any


def config_table(config: dict[str, Any], name: str) -> dict[str, Any]:
    """Read one optional TOML table without granting it extra authority."""

    value = config.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"Configuration section [{name}] must be a TOML table.")
    return value


def first_present(*values: object) -> object | None:
    """Return the first supplied non-null command/config value."""

    return next((value for value in values if value is not None), None)


def write_evaluation_record(path: Path, payload: str) -> Path:
    """Atomically write a private evaluation artifact chosen by the operator."""

    target = path.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            payload + ("" if payload.endswith("\n") else "\n"),
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return target
