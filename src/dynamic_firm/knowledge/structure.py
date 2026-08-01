"""Bounded navigation hints for extracted Knowledge text, not new facts."""
from __future__ import annotations

import re
from dataclasses import dataclass

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_IMAGE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")

@dataclass(frozen=True, slots=True)
class KnowledgeStructureHint:
    headings: tuple[str, ...]
    table_count: int
    image_references: tuple[str, ...]
    text_lines: int

def inspect_extracted_text(value: str, *, maximum_bytes: int = 64_000) -> KnowledgeStructureHint:
    if maximum_bytes < 1 or maximum_bytes > 1_048_576:
        raise ValueError("Knowledge structure preview byte limit is invalid")
    text = value.encode("utf-8")[:maximum_bytes].decode("utf-8", errors="ignore")
    lines = text.splitlines()
    headings = tuple(match.group(2).strip()[:240] for line in lines if (match := _HEADING.match(line)))[:50]
    tables = sum(1 for index in range(len(lines) - 1) if "|" in lines[index] and re.match(r"^\s*\|?\s*:?-{3,}", lines[index + 1]))
    images = tuple(match.group(1).strip()[:512] for match in _IMAGE.finditer(text))[:50]
    return KnowledgeStructureHint(headings, tables, images, len(lines))
