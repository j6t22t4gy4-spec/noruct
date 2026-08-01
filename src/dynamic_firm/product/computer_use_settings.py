"""Atomic lifecycle editing for the optional local computer-use policy."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from dynamic_firm.computer_use_connector import ComputerUseConfig, computer_use_config_from_settings


_HEADER = re.compile(r"(?m)^\[computer_use\][ \t]*(?:\r?\n|$)")
_TABLE_HEADER = re.compile(r"(?m)^\[\[?[^\]\r\n]+\]\]?[ \t]*(?:\r?\n|$)")


def computer_use_table_text(config: ComputerUseConfig) -> str:
    config.validate()
    quote = lambda value: json.dumps(value, ensure_ascii=False)
    apps = ", ".join(quote(item) for item in config.allowed_apps)
    return "\n".join((
        "[computer_use]",
        "enabled = true",
        f"driver_command = {quote(str(config.driver_command.expanduser().resolve()))}",
        f"allowed_apps = [{apps}]",
        f"timeout_seconds = {config.timeout_seconds:g}",
        f"max_result_bytes = {config.max_result_bytes}",
        f"allow_control = {str(config.allow_control).lower()}",
        "",
    ))


def _without_table(text: str) -> str:
    match = _HEADER.search(text)
    if match is None:
        return text.strip()
    following = _TABLE_HEADER.search(text, match.end())
    return (text[:match.start()] + text[following.start() if following else len(text):]).strip()


def _atomic_write(path: Path, value: str) -> Path:
    target = path.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".noruct-config-", dir=target.parent)
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


def write_computer_use_settings(path: Path, config: ComputerUseConfig) -> Path:
    target = path.expanduser().resolve()
    existing = target.read_text(encoding="utf-8") if target.is_file() else ""
    remainder = _without_table(existing)
    return _atomic_write(target, (remainder + "\n\n" if remainder else "") + computer_use_table_text(config))


def remove_computer_use_settings(path: Path) -> bool:
    target = path.expanduser().resolve()
    if not target.is_file():
        return False
    existing = target.read_text(encoding="utf-8")
    if _HEADER.search(existing) is None:
        return False
    remainder = _without_table(existing)
    _atomic_write(target, remainder + ("\n" if remainder else ""))
    return True


def configured_computer_use_policy(path: Path) -> ComputerUseConfig | None:
    if not path.is_file():
        return None
    import tomllib
    return computer_use_config_from_settings(tomllib.loads(path.read_text(encoding="utf-8")))
