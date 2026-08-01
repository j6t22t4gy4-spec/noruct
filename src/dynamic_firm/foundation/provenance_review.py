"""Fail-closed validation for a human-owned shipped-capsule provenance record.

This boundary validates only the immutable packet/record bindings an operator
explicitly supplies.  It never discovers a record implicitly, changes a
runtime default, or turns a completed provenance review into a release grant.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any


SCHEMA = "noruct.shipped-capsule-secondary-provenance-decisions.v1"
_DRAFT = "DRAFT_NOT_REVIEWED"
_REVIEWED = "REVIEWED_NOT_RELEASE_AUTHORIZED"
_DISPOSITIONS = {"APPROVE", "REPLACE", "EXCLUDE"}


class ProvenanceReviewError(ValueError):
    """Raised when a supplied human review is stale, incomplete, or permissive."""


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProvenanceReviewError(f"JSON input is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise ProvenanceReviewError(f"expected JSON object: {path}")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _record_key(record: dict[str, Any]) -> tuple[str, int]:
    try:
        return str(record["upstream_path"]), int(record["marker_line"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ProvenanceReviewError("record is missing a stable source key") from exc


def _is_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _expected_records(packet: dict[str, Any]) -> dict[tuple[str, int], dict[str, Any]]:
    records: dict[tuple[str, int], dict[str, Any]] = {}
    for record in packet.get("records", []):
        if not isinstance(record, dict):
            raise ProvenanceReviewError("review packet has an invalid record")
        local = record.get("local_review_range")
        immutable = record.get("immutable_origin")
        if not isinstance(local, dict) or not isinstance(immutable, dict):
            raise ProvenanceReviewError("review packet record lacks immutable bindings")
        key = _record_key({"upstream_path": record.get("upstream_path"), "marker_line": local.get("marker_line")})
        if key in records:
            raise ProvenanceReviewError("review packet has duplicate source records")
        records[key] = {
            "capsule_source_sha256": record.get("capsule_source_sha256"),
            "local_excerpt_sha256": local.get("excerpt_sha256"),
            "origin_license_file_sha256": immutable.get("license_file_sha256"),
            "origin_source_commit": immutable.get("source_commit"),
        }
    if not records:
        raise ProvenanceReviewError("review packet has no pending provenance records")
    return records


def validate_provenance_review(*, packet_path: str | Path, decisions_path: str | Path) -> dict[str, Any]:
    """Validate an explicitly supplied record without granting activation.

    Paths are intentionally caller-provided: an installed product must not
    infer human legal records from an arbitrary working directory.
    """

    packet_source = Path(packet_path).expanduser().resolve()
    decisions_source = Path(decisions_path).expanduser().resolve()
    if not packet_source.is_file() or packet_source.is_symlink():
        raise ProvenanceReviewError("review packet is unavailable")
    if not decisions_source.is_file() or decisions_source.is_symlink():
        raise ProvenanceReviewError("review decision record is unavailable")
    if packet_source.stat().st_size > 512 * 1024 or decisions_source.stat().st_size > 512 * 1024:
        raise ProvenanceReviewError("review input exceeds the bounded validator limit")
    packet = _read(packet_source)
    decisions = _read(decisions_source)
    if packet.get("packet_kind") != "noruct_shipped_capsule_secondary_provenance_human_review_v1":
        raise ProvenanceReviewError("review packet schema is unsupported")
    if packet.get("not_commercial_approval") is not True or packet.get("commercial_default_activation") != "blocked":
        raise ProvenanceReviewError("review packet must remain explicitly non-commercial")
    if decisions.get("schema_version") != SCHEMA:
        raise ProvenanceReviewError("decision record schema is unsupported")
    status = decisions.get("status")
    if status not in {_DRAFT, _REVIEWED}:
        raise ProvenanceReviewError("decision record cannot claim commercial authorization")
    if decisions.get("commercial_release_authorized") is not False:
        raise ProvenanceReviewError("decision record cannot authorize commercial release")
    if decisions.get("commercial_default_activation") is not False:
        raise ProvenanceReviewError("decision record cannot enable the runtime default")

    bindings = decisions.get("input_bindings")
    packet_bindings = packet.get("input_bindings")
    if not isinstance(bindings, dict) or not isinstance(packet_bindings, dict):
        raise ProvenanceReviewError("decision record lacks input bindings")
    expected_bindings = {
        "review_packet_sha256": _sha256(packet_source),
        "capsule_manifest_sha256": packet_bindings.get("capsule_manifest_sha256"),
        "capsule_source_tree_sha256": packet_bindings.get("capsule_source_tree_sha256"),
        "provenance_audit_sha256": packet_bindings.get("provenance_audit_sha256"),
        "technical_evidence_sha256": packet_bindings.get("technical_evidence_sha256"),
    }
    if bindings != expected_bindings:
        raise ProvenanceReviewError("decision record is not bound to the current review packet")

    expected_records = _expected_records(packet)
    actual_records: dict[tuple[str, int], dict[str, Any]] = {}
    for record in decisions.get("records", []):
        if not isinstance(record, dict):
            raise ProvenanceReviewError("decision record contains an invalid row")
        key = _record_key(record)
        if key in actual_records:
            raise ProvenanceReviewError("decision record contains duplicate rows")
        actual_records[key] = record
    if set(actual_records) != set(expected_records):
        raise ProvenanceReviewError("decision record must cover exactly the pending packet rows")

    complete = True
    for index, key in enumerate(sorted(expected_records), start=1):
        record = actual_records[key]
        expected = expected_records[key]
        if record.get("record_id") != f"ER-{index:02d}":
            raise ProvenanceReviewError(f"{key}: record id is not stable")
        for field, value in expected.items():
            if record.get(field) != value:
                raise ProvenanceReviewError(f"{key}: {field} does not match the review packet")
        disposition = record.get("disposition")
        if status == _DRAFT:
            if disposition != "PENDING":
                raise ProvenanceReviewError(f"{key}: draft record cannot contain a disposition")
            if any(record.get(field) not in (None, [], "") for field in ("reviewer", "reviewed_at", "evidence_refs", "reason")):
                raise ProvenanceReviewError(f"{key}: draft record cannot contain review evidence")
            complete = False
            continue
        if disposition not in _DISPOSITIONS:
            raise ProvenanceReviewError(f"{key}: review disposition is invalid")
        if not isinstance(record.get("reviewer"), str) or not record["reviewer"].strip():
            raise ProvenanceReviewError(f"{key}: reviewed record needs a reviewer")
        if not _is_timestamp(record.get("reviewed_at")):
            raise ProvenanceReviewError(f"{key}: reviewed record needs an ISO-8601 timestamp")
        if not isinstance(record.get("reason"), str) or not record["reason"].strip():
            raise ProvenanceReviewError(f"{key}: reviewed record needs a reason")
        references = record.get("evidence_refs")
        if not isinstance(references, list) or not references or not all(isinstance(item, str) and item.strip() for item in references):
            raise ProvenanceReviewError(f"{key}: reviewed record needs evidence references")

    return {
        "ok": True,
        "review_complete": complete,
        "commercial_release_authorized": False,
        "commercial_default_activation": False,
        "pending_record_count": len(expected_records),
        "status": status,
    }
