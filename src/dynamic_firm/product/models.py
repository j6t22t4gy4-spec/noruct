from __future__ import annotations

from dataclasses import dataclass

from dynamic_firm._vendor.model_catalog import get_local_codex_model_ids
from dynamic_firm._vendor.session_shell.fuzzy_model_filter import filter_indices

MAX_MODEL_OPTIONS = 20


@dataclass(frozen=True, slots=True)
class ModelOption:
    model_id: str
    detail: str
    current: bool = False


def model_options(provider_kind: str, current_model: str) -> tuple[ModelOption, ...]:
    """Build a bounded, offline-first model picker inventory."""

    current = current_model.strip() or "codex-default"
    candidates: list[tuple[str, str]] = []

    if provider_kind == "openai_codex":
        candidates.append(("codex-default", "Use the Codex CLI configured default"))
        if current != "codex-default":
            candidates.append((current, "Current session model"))
        candidates.extend(
            (model_id, "Discovered in the local Codex model cache")
            for model_id in get_local_codex_model_ids()
        )
    else:
        candidates.append((current, "Current configured model"))

    if current not in {model_id for model_id, _ in candidates}:
        candidates.insert(0, (current, "Current session model"))

    seen: set[str] = set()
    options: list[ModelOption] = []
    for model_id, detail in candidates:
        normalized = model_id.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        options.append(ModelOption(normalized, detail, normalized == current))
        if len(options) >= MAX_MODEL_OPTIONS:
            break
    return tuple(options)


def filter_model_options(
    options: tuple[ModelOption, ...],
    query: str,
    *,
    limit: int = MAX_MODEL_OPTIONS,
) -> tuple[ModelOption, ...]:
    """Bounded local model search using a private source-derived scorer."""

    normalized = " ".join(query.split())
    if len(normalized) > 160:
        raise ValueError("Model search query is too long")
    if limit < 1 or limit > MAX_MODEL_OPTIONS:
        raise ValueError("Model search limit is outside the local catalog bound")
    indices = filter_indices(tuple(option.model_id for option in options), normalized)
    return tuple(options[index] for index in indices[:limit])


__all__ = ["ModelOption", "filter_model_options", "model_options"]
