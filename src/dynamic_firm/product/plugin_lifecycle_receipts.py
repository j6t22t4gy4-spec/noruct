"""Bounded, content-free lifecycle receipts for managed executable plugins.

The plugin registry owns the transaction that changes future-Job discovery.
This helper only validates and appends the historical projection stored in that
same registry; it never reads a package, starts a host, or changes a Job pin.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Mapping, Sequence


LIFECYCLE_RECEIPT_SCHEMA = "noruct.plugin-lifecycle-receipt.v1"
LIFECYCLE_RECEIPT_KEY = "lifecycle_receipts"
MAX_LIFECYCLE_RECEIPTS = 256
MAX_RECEIPT_VERSIONS = 32
_ACTIONS = frozenset(
    {
        "INSTALLED_INACTIVE",
        "ACTIVATED_FUTURE_JOB",
        "DISABLED_FUTURE_JOB",
        "ROLLED_BACK_FUTURE_JOB",
        "DEPENDENCY_ENVIRONMENT_BUILT",
        "WITHDRAWN_FUTURE_JOB",
    }
)
_RECEIPT_KEYS = frozenset(
    {
        "schema",
        "sequence",
        "recorded_at",
        "action",
        "plugin_id",
        "versions",
        "package_sha256",
    }
)


def append_lifecycle_receipt(
    registry: dict[str, Any],
    *,
    action: str,
    plugin_id: str,
    versions: Sequence[str],
    package_digests: Sequence[str],
) -> None:
    """Append one bounded, safe lifecycle fact to a validated registry."""

    receipts = validate_lifecycle_receipts(registry.get(LIFECYCLE_RECEIPT_KEY, []))
    if action not in _ACTIONS:
        raise ValueError("Plugin lifecycle action is invalid")
    if not _bounded_text(plugin_id, 96):
        raise ValueError("Plugin lifecycle id is invalid")
    normalized_versions = tuple(versions)
    normalized_digests = tuple(package_digests)
    if (
        not normalized_versions
        or len(normalized_versions) > MAX_RECEIPT_VERSIONS
        or len(normalized_versions) != len(normalized_digests)
        or not all(_bounded_text(value, 64) for value in normalized_versions)
        or not all(_sha256(value) for value in normalized_digests)
    ):
        raise ValueError("Plugin lifecycle receipt identity is invalid")
    receipts.append(
        {
            "schema": LIFECYCLE_RECEIPT_SCHEMA,
            "sequence": receipts[-1]["sequence"] + 1 if receipts else 1,
            "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "action": action,
            "plugin_id": plugin_id,
            "versions": list(normalized_versions),
            "package_sha256": list(normalized_digests),
        }
    )
    registry[LIFECYCLE_RECEIPT_KEY] = receipts[-MAX_LIFECYCLE_RECEIPTS:]


def lifecycle_receipts(
    registry: Mapping[str, object], *, plugin_id: str | None, limit: int
) -> tuple[Mapping[str, object], ...]:
    """Return newest-first content-free historical facts for operator review."""

    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_LIFECYCLE_RECEIPTS:
        raise ValueError("Plugin lifecycle receipt limit is invalid")
    if plugin_id is not None and not _bounded_text(plugin_id, 96):
        raise ValueError("Plugin lifecycle id is invalid")
    values = validate_lifecycle_receipts(registry.get(LIFECYCLE_RECEIPT_KEY, []))
    selected = (
        item for item in reversed(values) if plugin_id is None or item["plugin_id"] == plugin_id
    )
    return tuple(dict(item) for item in list(selected)[:limit])


def append_selected_plugin_receipt(
    registry: dict[str, Any],
    *,
    action: str,
    plugin_id: str,
    selected: Sequence[Mapping[str, object]],
) -> None:
    """Adapt validated registry records to the content-free receipt shape."""

    append_lifecycle_receipt(
        registry,
        action=action,
        plugin_id=plugin_id,
        versions=tuple(str(item["version"]) for item in selected),
        package_digests=tuple(str(item["package_digest"]) for item in selected),
    )


def validate_lifecycle_receipts(value: object) -> list[dict[str, object]]:
    """Validate backward-compatible registry history before any write or read."""

    if not isinstance(value, list) or len(value) > MAX_LIFECYCLE_RECEIPTS:
        raise ValueError("Plugin lifecycle receipt history is malformed")
    normalized: list[dict[str, object]] = []
    expected_sequence: int | None = None
    for item in value:
        if not isinstance(item, Mapping) or set(item) != _RECEIPT_KEYS:
            raise ValueError("Plugin lifecycle receipt history is malformed")
        sequence = item.get("sequence")
        recorded_at = item.get("recorded_at")
        action = item.get("action")
        plugin_id = item.get("plugin_id")
        versions = item.get("versions")
        digests = item.get("package_sha256")
        if (
            not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or sequence < 1
            or expected_sequence is not None and sequence != expected_sequence + 1
            or not isinstance(recorded_at, str)
            or not _timestamp(recorded_at)
            or action not in _ACTIONS
            or not isinstance(plugin_id, str)
            or not _bounded_text(plugin_id, 96)
            or not isinstance(versions, list)
            or not isinstance(digests, list)
            or not versions
            or len(versions) > MAX_RECEIPT_VERSIONS
            or len(versions) != len(digests)
            or not all(isinstance(entry, str) and _bounded_text(entry, 64) for entry in versions)
            or not all(isinstance(entry, str) and _sha256(entry) for entry in digests)
        ):
            raise ValueError("Plugin lifecycle receipt history is malformed")
        expected_sequence = sequence
        normalized.append(
            {
                "schema": LIFECYCLE_RECEIPT_SCHEMA,
                "sequence": sequence,
                "recorded_at": recorded_at,
                "action": action,
                "plugin_id": plugin_id,
                "versions": list(versions),
                "package_sha256": list(digests),
            }
        )
    return normalized


def _bounded_text(value: object, maximum_bytes: int) -> bool:
    return isinstance(value, str) and bool(value) and len(value.encode("utf-8")) <= maximum_bytes and "\x00" not in value


def _sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _timestamp(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None
