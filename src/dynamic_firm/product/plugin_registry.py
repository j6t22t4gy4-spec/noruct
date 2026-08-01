"""Private persistence helpers for the executable-plugin registry."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

from dynamic_firm.product.plugin_lifecycle_receipts import (
    LIFECYCLE_RECEIPT_KEY,
    validate_lifecycle_receipts,
)


def read_plugin_registry(root: Path, registry_path: Path, *, schema: str) -> dict[str, Any]:
    if not registry_path.is_file():
        return {"schema": schema, "plugins": [], "receipts": {}, "environments": {}, LIFECYCLE_RECEIPT_KEY: []}
    try:
        value = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Plugin registry is unreadable") from exc
    allowed_keys = (
        {"schema", "plugins"}, {"schema", "plugins", "receipts"},
        {"schema", "plugins", "receipts", "environments"},
        {"schema", "plugins", LIFECYCLE_RECEIPT_KEY},
        {"schema", "plugins", "receipts", "environments", LIFECYCLE_RECEIPT_KEY},
    )
    if not isinstance(value, dict) or set(value) not in allowed_keys or value.get("schema") != schema or not isinstance(value["plugins"], list):
        raise ValueError("Plugin registry is malformed")
    value.setdefault("receipts", {})
    if not isinstance(value["receipts"], dict) or not all(isinstance(key, str) and isinstance(item, Mapping) for key, item in value["receipts"].items()):
        raise ValueError("Plugin registry receipts are malformed")
    value.setdefault("environments", {})
    if not isinstance(value["environments"], dict) or not all(isinstance(key, str) and isinstance(item, Mapping) for key, item in value["environments"].items()):
        raise ValueError("Plugin registry environments are malformed")
    try:
        value[LIFECYCLE_RECEIPT_KEY] = validate_lifecycle_receipts(value.get(LIFECYCLE_RECEIPT_KEY, []))
    except ValueError as exc:
        raise ValueError("Plugin registry lifecycle receipts are malformed") from exc
    return value


def write_plugin_registry(root: Path, registry_path: Path, value: Mapping[str, Any]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".noruct-plugin-registry-", dir=root)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, registry_path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
