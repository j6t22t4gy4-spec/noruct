"""Wire-payload safety helpers derived from Hermes Agent.

Upstream: NousResearch/hermes-agent
Commit: 7fe1cb384e4f99aae3243c4c578904ac8c114b25
Path: agent/message_sanitization.py
Upstream SHA-256: d840b7cce4adaefcfc54f7ac77fe06fbeff4a3cca7858664d87fb3e4e4716769
Copyright (c) 2025 Nous Research
SPDX-License-Identifier: MIT

Modified for Dynamic Firm: extracted surrogate and JSON argument repair only;
sanitizes mapping keys as well as values; never logs argument contents; returns
None instead of an executable empty object when non-empty input is unrepairable.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any


logger = logging.getLogger(__name__)
_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")


def sanitize_surrogates(text: str) -> str:
    """Replace lone surrogate code points with U+FFFD."""

    if _SURROGATE_RE.search(text):
        return _SURROGATE_RE.sub("\ufffd", text)
    return text


def sanitize_structure_surrogates(payload: Any) -> bool:
    """Replace surrogate code points throughout nested dict/list values in-place."""

    found = False

    def walk(node: Any) -> None:
        nonlocal found
        if isinstance(node, dict):
            for key, value in list(node.items()):
                target_key = sanitize_surrogates(key) if isinstance(key, str) else key
                if target_key != key:
                    if target_key in node:
                        raise ValueError("Surrogate sanitization would create a duplicate mapping key")
                    del node[key]
                    node[target_key] = value
                    key = target_key
                    found = True
                if isinstance(value, str):
                    sanitized = sanitize_surrogates(value)
                    if sanitized != value:
                        node[key] = sanitized
                        found = True
                elif isinstance(value, (dict, list)):
                    walk(value)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                if isinstance(value, str):
                    sanitized = sanitize_surrogates(value)
                    if sanitized != value:
                        node[index] = sanitized
                        found = True
                elif isinstance(value, (dict, list)):
                    walk(value)

    walk(payload)
    return found


def _escape_invalid_chars_in_json_strings(raw: str) -> str:
    """Escape unescaped control characters occurring inside JSON strings."""

    out: list[str] = []
    in_string = False
    index = 0
    while index < len(raw):
        character = raw[index]
        if in_string:
            if character == "\\" and index + 1 < len(raw):
                out.extend((character, raw[index + 1]))
                index += 2
                continue
            if character == '"':
                in_string = False
                out.append(character)
            elif ord(character) < 0x20:
                out.append(f"\\u{ord(character):04x}")
            else:
                out.append(character)
        else:
            if character == '"':
                in_string = True
            out.append(character)
        index += 1
    return "".join(out)


def _parsed_object(raw: str, *, strict: bool = True) -> dict[str, Any] | None:
    try:
        parsed = json.loads(raw, strict=strict)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    try:
        sanitize_structure_surrogates(parsed)
    except ValueError:
        return None
    return parsed


def repair_tool_call_arguments(
    raw_arguments: str | None,
    tool_name: str = "?",
) -> dict[str, Any] | None:
    """Repair common model JSON mistakes, accepting JSON objects only."""

    del tool_name  # Retained for call-site compatibility; never log model-controlled values.
    raw = sanitize_surrogates(raw_arguments.strip()) if isinstance(raw_arguments, str) else ""
    if not raw or raw == "None":
        return {}

    parsed = _parsed_object(raw, strict=False)
    if parsed is not None:
        return parsed

    fixed = re.sub(r",\s*([}\]])", r"\1", raw)
    fixed = re.sub(r",\s*$", "", fixed)
    missing_brackets = fixed.count("[") - fixed.count("]")
    missing_braces = fixed.count("{") - fixed.count("}")
    if missing_brackets > 0:
        fixed += "]" * missing_brackets
    if missing_braces > 0:
        fixed += "}" * missing_braces

    for _ in range(50):
        parsed = _parsed_object(fixed)
        if parsed is not None:
            if fixed != raw:
                logger.warning("Repaired malformed tool arguments")
            return parsed
        if fixed.endswith("}") and fixed.count("}") > fixed.count("{"):
            fixed = fixed[:-1]
        elif fixed.endswith("]") and fixed.count("]") > fixed.count("["):
            fixed = fixed[:-1]
        else:
            break

    escaped = _escape_invalid_chars_in_json_strings(fixed)
    parsed = _parsed_object(escaped)
    if parsed is not None:
        logger.warning("Repaired control characters in tool arguments")
        return parsed

    logger.warning("Rejected unrepairable tool arguments")
    return None
