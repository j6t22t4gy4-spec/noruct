"""Atomic configuration for user-owned compatible external skill roots.

The capability deliberately stores directories only.  Discovery remains a
fresh read-only scan performed by the existing external-skill loader; no skill
content, scripts, or assets are copied or executed during setup.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Sequence

from dynamic_firm.product.external_skills import external_skill_directories


_HEADER = re.compile(r"(?m)^\[skills\][ \t]*(?:\r?\n|$)")
_TABLE_HEADER = re.compile(r"(?m)^\[\[?[^\]\r\n]+\]\]?[ \t]*(?:\r?\n|$)")


def _without_skills_table(text: str) -> str:
    match = _HEADER.search(text)
    if match is None:
        return text.strip()
    following = _TABLE_HEADER.search(text, match.end())
    return (text[:match.start()] + text[following.start() if following else len(text):]).strip()


def write_external_skill_settings(path: Path, roots: Sequence[str | Path]) -> Path:
    """Replace only `[skills]` after validating existing local directories."""

    resolved = external_skill_directories(tuple(roots))
    if not resolved:
        raise ValueError("Configure at least one existing external skill directory")
    target = path.expanduser().resolve()
    existing = target.read_text(encoding="utf-8") if target.is_file() else ""
    table = "[skills]\nexternal_dirs = " + json.dumps([str(item) for item in resolved], ensure_ascii=False) + "\n"
    value = (_without_skills_table(existing) + "\n\n" if _without_skills_table(existing) else "") + table
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".noruct-skills-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return target


def remove_external_skill_settings(path: Path) -> bool:
    """Remove only the optional external-skill roots table.

    Disconnecting a root must never delete the user-owned skill files.  It
    only stops future Jobs from discovering them through global settings.
    """

    target = path.expanduser().resolve()
    if not target.is_file():
        return False
    existing = target.read_text(encoding="utf-8")
    remainder = _without_skills_table(existing)
    if remainder == existing.strip():
        return False
    descriptor, temporary = tempfile.mkstemp(prefix=".noruct-skills-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(remainder + ("\n" if remainder else ""))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return True
