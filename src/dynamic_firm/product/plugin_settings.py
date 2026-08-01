"""Atomic configuration helpers for the optional executable-plugin runtime."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from .executable_plugins import PluginRuntimeConfig, plugin_config_from_settings


_HEADER = re.compile(r"(?m)^\[plugins\][ \t]*(?:\r?\n|$)")
_TABLE_HEADER = re.compile(r"(?m)^\[\[?[^\]\r\n]+\]\]?[ \t]*(?:\r?\n|$)")


def plugin_table_text(root: Path) -> str:
    return "\n".join(("[plugins]", "enabled = true", f"root = {json.dumps(str(root.expanduser().resolve()), ensure_ascii=False)}", ""))


def _without_table(text: str) -> str:
    match = _HEADER.search(text)
    if match is None:
        return text.strip()
    following = _TABLE_HEADER.search(text, match.end())
    return (text[:match.start()] + text[following.start() if following else len(text):]).strip()


def _atomic_write(path: Path, value: str) -> Path:
    target = path.expanduser().resolve(); target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".noruct-config-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(value); handle.flush(); os.fsync(handle.fileno())
        os.chmod(temporary, 0o600); os.replace(temporary, target)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return target


def write_plugin_settings(path: Path, root: Path) -> Path:
    target = path.expanduser().resolve()
    existing = target.read_text(encoding="utf-8") if target.is_file() else ""
    remainder = _without_table(existing)
    return _atomic_write(target, (remainder + "\n\n" if remainder else "") + plugin_table_text(root))


def remove_plugin_settings(path: Path) -> bool:
    target = path.expanduser().resolve()
    if not target.is_file():
        return False
    existing = target.read_text(encoding="utf-8")
    if _HEADER.search(existing) is None:
        return False
    remainder = _without_table(existing)
    _atomic_write(target, remainder + ("\n" if remainder else ""))
    return True


def configured_plugin_runtime(path: Path) -> PluginRuntimeConfig | None:
    if not path.is_file():
        return None
    import tomllib
    return plugin_config_from_settings(tomllib.loads(path.read_text(encoding="utf-8")))
