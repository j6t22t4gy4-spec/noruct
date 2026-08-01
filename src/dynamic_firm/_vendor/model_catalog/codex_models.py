"""Codex model discovery from local cache and config.

Modified extract from NousResearch/hermes-agent:
- commit: 89bd0fba903bbfd78b0d99ce6f194863dd01b7e1
- upstream path: hermes_cli/codex_models.py
- upstream SHA-256: add0c1a4ef5f30762618f0e9a7bdc5311eee85bea8035ca273277f174e8333b9

Noruct retains only the standard-library local config/cache readers. The
upstream access-token API probe, private endpoint, hardcoded model catalog,
and synthetic forward-compatibility logic are intentionally excluded.
See the adjacent LICENSE and THIRD_PARTY_NOTICES.md.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import List, Optional

_MAX_LOCAL_MODEL_FILE_BYTES = 2_000_000


def _read_local_text(path: Path) -> Optional[str]:
    """Read one bounded regular file without following a credential-bearing symlink."""

    try:
        if path.is_symlink() or not path.is_file():
            return None
        with path.open("rb") as handle:
            raw = handle.read(_MAX_LOCAL_MODEL_FILE_BYTES + 1)
        if len(raw) > _MAX_LOCAL_MODEL_FILE_BYTES:
            return None
        return raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _read_default_model(codex_home: Path) -> Optional[str]:
    config_path = codex_home / "config.toml"
    text = _read_local_text(config_path)
    if text is None:
        return None
    try:
        import tomllib
    except Exception:
        return None
    try:
        payload = tomllib.loads(text)
    except Exception:
        return None
    model = payload.get("model") if isinstance(payload, dict) else None
    if isinstance(model, str) and model.strip():
        return model.strip()
    return None


def _read_cache_models(codex_home: Path) -> List[str]:
    cache_path = codex_home / "models_cache.json"
    text = _read_local_text(cache_path)
    if text is None:
        return []
    try:
        raw = json.loads(text)
    except Exception:
        return []

    entries = raw.get("models") if isinstance(raw, dict) else None
    sortable = []
    if isinstance(entries, list):
        for item in entries:
            if not isinstance(item, dict):
                continue
            slug = item.get("slug")
            if not isinstance(slug, str) or not slug.strip():
                continue
            slug = slug.strip()
            visibility = item.get("visibility")
            if isinstance(visibility, str) and visibility.strip().lower() in {
                "hide",
                "hidden",
            }:
                continue
            priority = item.get("priority")
            rank = int(priority) if isinstance(priority, (int, float)) else 10_000
            sortable.append((rank, slug))

    sortable.sort(key=lambda item: (item[0], item[1]))
    deduped: List[str] = []
    for _, slug in sortable:
        if slug not in deduped:
            deduped.append(slug)
    return deduped


def get_local_codex_model_ids() -> List[str]:
    """Return model IDs found in the user-managed Codex config and cache."""

    codex_home_str = os.getenv("CODEX_HOME", "").strip() or str(Path.home() / ".codex")
    codex_home = Path(codex_home_str).expanduser()
    ordered: List[str] = []

    default_model = _read_default_model(codex_home)
    if default_model:
        ordered.append(default_model)

    for model_id in _read_cache_models(codex_home):
        if model_id not in ordered:
            ordered.append(model_id)

    return ordered
