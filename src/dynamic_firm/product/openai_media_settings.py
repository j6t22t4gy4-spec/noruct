"""Atomic lifecycle editing for the optional direct media capability."""

from __future__ import annotations

import json
import os
import re
import tempfile
import tomllib
from pathlib import Path

from dynamic_firm.openai_media import OpenAIMediaConfig, media_config_from_settings


_HEADER = re.compile(r"(?m)^\[openai_media\][ \t]*(?:\r?\n|$)")
_TABLE = re.compile(r"(?m)^\[\[?[^\]\r\n]+\]\]?[ \t]*(?:\r?\n|$)")


def _without_table(text: str) -> str:
    match = _HEADER.search(text)
    if match is None:
        return text.strip()
    following = _TABLE.search(text, match.end())
    return (text[:match.start()] + (text[following.start():] if following else "")).strip()


def _atomic_write(path: Path, text: str) -> Path:
    target = path.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".noruct-config-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text); handle.flush(); os.fsync(handle.fileno())
        os.chmod(temporary, 0o600); os.replace(temporary, target)
    finally:
        try: os.unlink(temporary)
        except FileNotFoundError: pass
    return target


def media_table_text(config: OpenAIMediaConfig) -> str:
    config.validate(); quote = lambda value: json.dumps(value, ensure_ascii=False)
    return "\n".join((
        "[openai_media]", "enabled = true", f"api_key_env = {quote(config.api_key_env)}",
        f"image_enabled = {str(config.image_enabled).lower()}",
        f"speech_enabled = {str(config.speech_enabled).lower()}",
        f"transcription_enabled = {str(config.transcription_enabled).lower()}",
        f"video_enabled = {str(config.video_enabled).lower()}",
        f"image_model = {quote(config.image_model)}", f"speech_model = {quote(config.speech_model)}",
        f"transcription_model = {quote(config.transcription_model)}", f"video_model = {quote(config.video_model)}",
        f"timeout_seconds = {config.timeout_seconds:g}", f"video_timeout_seconds = {config.video_timeout_seconds:g}", "",
    ))


def write_media_settings(path: Path, config: OpenAIMediaConfig) -> Path:
    config.validate(); target = path.expanduser().resolve()
    existing = target.read_text(encoding="utf-8") if target.is_file() else ""
    remainder = _without_table(existing)
    return _atomic_write(target, (remainder + "\n\n" if remainder else "") + media_table_text(config))


def remove_media_settings(path: Path) -> bool:
    target = path.expanduser().resolve()
    if not target.is_file() or _HEADER.search(target.read_text(encoding="utf-8")) is None:
        return False
    remainder = _without_table(target.read_text(encoding="utf-8"))
    _atomic_write(target, remainder + ("\n" if remainder else ""))
    return True


def configured_media_policy(path: Path) -> OpenAIMediaConfig | None:
    target = path.expanduser().resolve()
    return None if not target.is_file() else media_config_from_settings(tomllib.loads(target.read_text(encoding="utf-8")))
