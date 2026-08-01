"""Atomic configuration lifecycle for the optional SearXNG search tool."""

from __future__ import annotations

import json
import os
import re
import tempfile
import tomllib
from pathlib import Path

from dynamic_firm.web_search import SearxngSearchConfig, config_from_settings


_HEADER = re.compile(r"(?m)^\[web_search\][ \t]*(?:\r?\n|$)")
_TABLE = re.compile(r"(?m)^\[\[?[^\]\r\n]+\]\]?[ \t]*(?:\r?\n|$)")


def _without_table(text: str) -> str:
    match = _HEADER.search(text)
    if match is None:
        return text.strip()
    following = _TABLE.search(text, match.end())
    return (text[:match.start()] + (text[following.start():] if following else "")).strip()


def _atomic_write(path: Path, text: str) -> Path:
    target = path.expanduser().resolve(); target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".noruct-config-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text); handle.flush(); os.fsync(handle.fileno())
        os.chmod(temporary, 0o600); os.replace(temporary, target)
    finally:
        try: os.unlink(temporary)
        except FileNotFoundError: pass
    return target


def web_search_table_text(config: SearxngSearchConfig) -> str:
    config.validate(); quote = lambda value: json.dumps(value, ensure_ascii=False)
    return "\n".join((
        "[web_search]", "enabled = true", f"base_url = {quote(config.normalized_base_url)}",
        f"timeout_seconds = {config.timeout_seconds:g}", f"max_results = {config.max_results}",
        f"max_result_bytes = {config.max_result_bytes}", "",
    ))


def write_web_search_settings(path: Path, config: SearxngSearchConfig) -> Path:
    config.validate(); target = path.expanduser().resolve()
    existing = target.read_text(encoding="utf-8") if target.is_file() else ""
    remainder = _without_table(existing)
    return _atomic_write(target, (remainder + "\n\n" if remainder else "") + web_search_table_text(config))


def remove_web_search_settings(path: Path) -> bool:
    target = path.expanduser().resolve()
    if not target.is_file() or _HEADER.search(target.read_text(encoding="utf-8")) is None:
        return False
    remainder = _without_table(target.read_text(encoding="utf-8"))
    _atomic_write(target, remainder + ("\n" if remainder else ""))
    return True


def configured_web_search_policy(path: Path) -> SearxngSearchConfig | None:
    target = path.expanduser().resolve()
    return None if not target.is_file() else config_from_settings(tomllib.loads(target.read_text(encoding="utf-8")))
