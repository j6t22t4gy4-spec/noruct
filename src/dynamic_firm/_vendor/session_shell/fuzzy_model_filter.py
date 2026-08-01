"""Private extraction of the Hermes terminal fuzzy ranking algorithm.

Source: hermes_cli/curses_ui.py at the exact registered Hermes Agent H1 pin.
The surrounding curses renderer, colors, UI labels and product entrypoints are
intentionally excluded. This module has no runtime dependency outside stdlib.
"""

from __future__ import annotations


_WORD_BOUNDARY = frozenset("-_/. ")


def _is_boundary(target: str, index: int) -> bool:
    """True if position ``index`` in ``target`` starts a word."""
    if index == 0:
        return True
    prev = target[index - 1]
    if prev in _WORD_BOUNDARY:
        return True
    cur = target[index]
    return prev == prev.lower() and cur != cur.lower() and cur == cur.upper()


def _token_score(orig: str, lower: str, token: str) -> float | None:
    """Score one token against a target; return None when it cannot match."""
    score = 0.0
    prev = -1
    search_from = 0
    positions: list[int] = []
    for ch in token:
        idx = lower.find(ch, search_from)
        if idx < 0:
            return None
        positions.append(idx)
        score += 1
        if prev >= 0 and idx == prev + 1:
            score += 5
        elif prev >= 0:
            score -= min(idx - prev - 1, 3)
        if _is_boundary(orig, idx):
            score += 3
        if idx == 0:
            score += 5
        prev = idx
        search_from = idx + 1
    if positions and positions[0] == 0 and positions[-1] == len(positions) - 1:
        score += 8
    if lower == token:
        score += 20
    score -= len(lower) * 0.01
    return score


def _fuzzy_score(label: str, query: str) -> float | None:
    """Aggregate each query token using AND semantics."""
    lower = label.lower()
    tokens = query.lower().split()
    if not tokens:
        return 0.0
    total = 0.0
    for token in tokens:
        token_score = _token_score(label, lower, token)
        if token_score is None:
            return None
        total += token_score
    return total


def filter_indices(items: tuple[str, ...], query: str) -> tuple[int, ...]:
    """Return matching item indices ranked best-first, stable on equal score."""
    normalized = query.strip()
    if not normalized:
        return tuple(range(len(items)))
    scored: list[tuple[int, float]] = []
    for index, label in enumerate(items):
        score = _fuzzy_score(label, normalized)
        if score is not None:
            scored.append((index, score))
    scored.sort(key=lambda pair: (-pair[1], pair[0]))
    return tuple(index for index, _ in scored)
