"""Additive, bounded v2 envelope for the execution-summary projection.

The v1 payload remains the only source of the existing summary semantics.  V2
stores it verbatim and accepts only caller-supplied extension facts; it does
not inspect or derive facts from a Job, prompt, transcript, or tool output.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from typing import Any

from .execution_summary import EXECUTION_SUMMARY_SCHEMA


EXECUTION_SUMMARY_V2_SCHEMA = "noruct.execution-summary.v2"
SUPPORTED_EXECUTION_SUMMARY_SCHEMAS = (
    EXECUTION_SUMMARY_V2_SCHEMA,
    EXECUTION_SUMMARY_SCHEMA,
)

_COLLECTION_FIELDS = (
    "assignment_rationale",
    "ai_contribution",
    "review_focus",
    "material_alternatives",
)
_SCALAR_FIELDS = ("improvement_status", "evidence_level")
_EXTENSION_FIELDS = frozenset((*_COLLECTION_FIELDS, *_SCALAR_FIELDS))
_COLLECTION_LIMIT = 3
_NOT_RECORDED = "NOT_RECORDED"
_UNKNOWN = "UNKNOWN"


def _require_v1_payload(payload: object) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise TypeError("v1 execution summary must be a mapping")
    if payload.get("schema_version") != EXECUTION_SUMMARY_SCHEMA:
        raise ValueError("payload is not noruct.execution-summary.v1")
    return payload


def _fact_collection(name: str, value: object) -> dict[str, Any]:
    if value is None:
        entries: tuple[Mapping[str, Any], ...] = ()
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if len(value) > _COLLECTION_LIMIT:
            raise ValueError(f"{name} accepts at most {_COLLECTION_LIMIT} entries")
        if not all(isinstance(entry, Mapping) for entry in value):
            raise TypeError(f"{name} entries must be mappings")
        entries = tuple(deepcopy(entry) for entry in value)
    else:
        raise TypeError(f"{name} must be a sequence of mappings")
    return {
        "status": "RECORDED" if entries else _NOT_RECORDED,
        "items": entries,
    }


def _fact_scalar(name: str, value: object, missing: str) -> str:
    if value is None:
        return missing
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    return value if value else missing


def execution_summary_v2(
    v1_payload: Mapping[str, Any],
    *,
    extension_facts: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Wrap a v1 summary with explicit, bounded extension facts.

    The nested ``v1`` value is a deep copy with the same mapping content, so a
    caller may continue passing it to an existing v1 consumer independently.
    Missing extension facts are represented by fixed conservative states.
    """

    original = _require_v1_payload(v1_payload)
    facts: Mapping[str, Any] = {} if extension_facts is None else extension_facts
    if not isinstance(facts, Mapping):
        raise TypeError("extension_facts must be a mapping")
    unknown = set(facts) - _EXTENSION_FIELDS
    if unknown:
        raise ValueError(f"unknown execution-summary v2 extension(s): {sorted(unknown)!r}")

    extensions: dict[str, Any] = {
        name: _fact_collection(name, facts.get(name))
        for name in _COLLECTION_FIELDS
    }
    extensions["improvement_status"] = _fact_scalar(
        "improvement_status", facts.get("improvement_status"), _NOT_RECORDED
    )
    extensions["evidence_level"] = _fact_scalar(
        "evidence_level", facts.get("evidence_level"), _UNKNOWN
    )
    return {
        "schema_version": EXECUTION_SUMMARY_V2_SCHEMA,
        "v1": deepcopy(original),
        "extensions": extensions,
    }


def v1_payload_from_execution_summary_v2(
    envelope: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the independently usable v1 payload nested in a v2 envelope."""

    if not isinstance(envelope, Mapping):
        raise TypeError("execution-summary v2 envelope must be a mapping")
    if envelope.get("schema_version") != EXECUTION_SUMMARY_V2_SCHEMA:
        raise ValueError("payload is not noruct.execution-summary.v2")
    return dict(deepcopy(_require_v1_payload(envelope.get("v1"))))


def negotiate_execution_summary_version(
    requested_version: str | None,
    supported_versions: Iterable[str],
) -> str:
    """Choose a known version, preferring the requested version when present.

    If the requested version is unavailable, the highest supported known
    version is selected.  Migration between v1 and v2 is explicit through
    :func:`migrate_execution_summary`.
    """

    supported = tuple(dict.fromkeys(supported_versions))
    unknown = set(supported) - set(SUPPORTED_EXECUTION_SUMMARY_SCHEMAS)
    if unknown:
        raise ValueError(f"unknown execution-summary version(s): {sorted(unknown)!r}")
    if requested_version is not None and requested_version not in SUPPORTED_EXECUTION_SUMMARY_SCHEMAS:
        raise ValueError(f"unknown execution-summary version: {requested_version!r}")
    if requested_version in supported:
        return requested_version
    for version in SUPPORTED_EXECUTION_SUMMARY_SCHEMAS:
        if version in supported:
            return version
    raise ValueError("no supported execution-summary version")


def migrate_execution_summary(
    payload: Mapping[str, Any],
    target_version: str,
) -> dict[str, Any]:
    """Explicitly migrate between the v1 payload and the additive v2 envelope."""

    if target_version not in SUPPORTED_EXECUTION_SUMMARY_SCHEMAS:
        raise ValueError(f"unknown execution-summary version: {target_version!r}")
    if not isinstance(payload, Mapping):
        raise TypeError("execution summary must be a mapping")

    source_version = payload.get("schema_version")
    if source_version == EXECUTION_SUMMARY_SCHEMA:
        original = _require_v1_payload(payload)
        if target_version == EXECUTION_SUMMARY_SCHEMA:
            return dict(deepcopy(original))
        return execution_summary_v2(original)
    if source_version == EXECUTION_SUMMARY_V2_SCHEMA:
        if target_version == EXECUTION_SUMMARY_V2_SCHEMA:
            return dict(deepcopy(payload))
        return v1_payload_from_execution_summary_v2(payload)
    raise ValueError(f"unknown execution-summary source version: {source_version!r}")
