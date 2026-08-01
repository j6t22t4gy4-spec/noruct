"""Private Hermes-derived streaming memory-context leak guard.

Extracted from ``agent/memory_manager.py`` at Noruct's exact registered H1
commit. External memory provider lifecycle, threads, tool schemas, plugins and
configuration are intentionally excluded.
"""

from __future__ import annotations

import re


_FENCE_TAG_RE = re.compile(r'</?\s*memory-context\s*>', re.IGNORECASE)
_INTERNAL_CONTEXT_RE = re.compile(
    r'<\s*memory-context\s*>[\s\S]*?</\s*memory-context\s*>',
    re.IGNORECASE,
)
_INTERNAL_NOTE_RE = re.compile(
    r'\[System note:\s*The following is recalled memory context,\s*NOT new user input\.\s*Treat as (?:informational background data|authoritative reference data[^\]]*)\.\]\s*',
    re.IGNORECASE,
)


def sanitize_context(text: str) -> str:
    """Strip memory fences, injected context blocks and their system note."""
    text = _INTERNAL_CONTEXT_RE.sub('', text)
    text = _INTERNAL_NOTE_RE.sub('', text)
    return _FENCE_TAG_RE.sub('', text)


class StreamingContextScrubber:
    """Statefully suppress a block-delimited memory fence across text deltas."""

    _OPEN_TAG = "<memory-context>"
    _CLOSE_TAG = "</memory-context>"

    def __init__(self) -> None:
        self._in_span = False
        self._buf = ""
        self._at_block_boundary = True

    def feed(self, text: str) -> str:
        if not text:
            return ""
        buf = self._buf + text
        self._buf = ""
        out: list[str] = []
        while buf:
            if self._in_span:
                index = buf.lower().find(self._CLOSE_TAG)
                if index == -1:
                    held = self._max_partial_suffix(buf, self._CLOSE_TAG)
                    self._buf = buf[-held:] if held else ""
                    return "".join(out)
                buf = buf[index + len(self._CLOSE_TAG):]
                self._in_span = False
                continue
            index = self._find_boundary_open_tag(buf)
            if index == -1:
                held = self._max_pending_open_suffix(buf) or self._max_partial_suffix(
                    buf, self._OPEN_TAG
                )
                if held:
                    self._append_visible(out, buf[:-held])
                    self._buf = buf[-held:]
                else:
                    self._append_visible(out, buf)
                return "".join(out)
            if index:
                self._append_visible(out, buf[:index])
            buf = buf[index + len(self._OPEN_TAG):]
            self._in_span = True
        return "".join(out)

    def flush(self) -> str:
        if self._in_span:
            self._buf = ""
            self._in_span = False
            return ""
        tail = self._buf
        self._buf = ""
        return tail

    @staticmethod
    def _max_partial_suffix(buf: str, tag: str) -> int:
        tag_lower = tag.lower()
        buf_lower = buf.lower()
        for length in range(min(len(buf_lower), len(tag_lower) - 1), 0, -1):
            if tag_lower.startswith(buf_lower[-length:]):
                return length
        return 0

    def _find_boundary_open_tag(self, buf: str) -> int:
        lower = buf.lower()
        start = 0
        while True:
            index = lower.find(self._OPEN_TAG, start)
            if index == -1:
                return -1
            if self._is_block_boundary(buf, index) and self._has_block_opener_suffix(buf, index):
                return index
            start = index + 1

    def _max_pending_open_suffix(self, buf: str) -> int:
        if not buf.lower().endswith(self._OPEN_TAG):
            return 0
        index = len(buf) - len(self._OPEN_TAG)
        return len(self._OPEN_TAG) if self._is_block_boundary(buf, index) else 0

    def _has_block_opener_suffix(self, buf: str, index: int) -> bool:
        after = index + len(self._OPEN_TAG)
        return after < len(buf) and buf[after] in "\r\n"

    def _is_block_boundary(self, buf: str, index: int) -> bool:
        if index == 0:
            return self._at_block_boundary
        preceding = buf[:index]
        newline = preceding.rfind("\n")
        if newline == -1:
            return self._at_block_boundary and preceding.strip() == ""
        return preceding[newline + 1:].strip() == ""

    def _append_visible(self, out: list[str], text: str) -> None:
        if text:
            out.append(text)
            newline = text.rfind("\n")
            self._at_block_boundary = (
                text[newline + 1:].strip() == "" if newline != -1 else self._at_block_boundary and text.strip() == ""
            )
