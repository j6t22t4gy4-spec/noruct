"""Fail-closed intake for one manually authorized Employee Runtime provider slot."""

from __future__ import annotations

import json
import hashlib
import os
import re
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from dynamic_firm import __version__
from dynamic_firm.company.models import content_digest

from .source import (
    EMPLOYEE_ACTIVE_FORK_TREE_SHA256,
    EMPLOYEE_FOUNDATION_COMMIT,
    HISTORICAL_EMPLOYEE_CAPSULE_TREE_SHA256,
)


SCHEMA = "noruct.employee-runtime-provider-slot-evidence.v2"
LEGACY_SCHEMA = "noruct.employee-runtime-provider-slot-evidence.v1"
_MAX_BYTES = 256 * 1024
_HEX = re.compile(r"[0-9a-f]{64}")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}")
_SLOT_LIMITS = {"direct": 1, "read_tool": 2, "approval": 2, "cancel_recovery": 1}
_LEGACY_FIELDS = {
    "schema_version", "evidence_id", "content_hash", "recorded_at", "noruct_version",
    "source_commit", "capsule_tree_sha256", "wheel_sha256", "provider_id", "model_id",
    "slot", "operator_slot_authorized", "quota_confirmed", "activation",
    "commercial_default_eligible", "shared_network_release_authorized", "limits", "observed",
}
_FIELDS = _LEGACY_FIELDS | {
    "worker_python_sha256", "adapter_revision", "operator_authorized_at",
    "action_policy_sha256", "fixture_sha256", "event_sequence_sha256",
    "usage_accounting",
}
_FORBIDDEN_TERMS = ("api_key", "secret", "password", "credential", "transcript", "raw_prompt", "raw_response")
CODEX_EXEC_ADAPTER_REVISION = "noruct-codex-parent-tool-cancel-v1"


class ProviderSlotEvidenceError(ValueError):
    """Stable refusal for malformed or unsafe provider-slot evidence."""


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProviderSlotEvidenceError(f"provider slot evidence {label} is invalid")
    return value


def _integer(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ProviderSlotEvidenceError(f"provider slot evidence {label} is invalid")
    return value


def _timestamp(value: object, label: str) -> datetime:
    try:
        return datetime.fromisoformat(_string(value, label))
    except ValueError:
        raise ProviderSlotEvidenceError(
            f"provider slot evidence {label} is invalid"
        ) from None


def _reject_secret_shaped(value: object) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str) or any(term in key.lower() for term in _FORBIDDEN_TERMS):
                raise ProviderSlotEvidenceError("provider slot evidence contains a forbidden field")
            _reject_secret_shaped(child)
    elif isinstance(value, list):
        for child in value:
            _reject_secret_shaped(child)


def _read(path: str | Path) -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    if not source.is_file() or source.is_symlink():
        raise ProviderSlotEvidenceError("provider slot evidence artifact is unavailable")
    try:
        raw = source.read_bytes()
        if len(raw) > _MAX_BYTES:
            raise ProviderSlotEvidenceError("provider slot evidence artifact is too large")
        value = json.loads(raw.decode("utf-8"))
    except ProviderSlotEvidenceError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ProviderSlotEvidenceError("provider slot evidence artifact is invalid") from None
    if not isinstance(value, dict):
        raise ProviderSlotEvidenceError("provider slot evidence artifact is invalid")
    return value


def validate_provider_slot_evidence(path: str | Path) -> dict[str, Any]:
    """Validate one secret-free, explicitly authorized live provider slot.

    Acceptance only says that an artifact is structurally credible for later
    human review. It cannot change any runtime default or release gate.
    """

    value = _read(path)
    schema_version = value.get("schema_version")
    fields = _LEGACY_FIELDS if schema_version == LEGACY_SCHEMA else _FIELDS
    if set(value) != fields:
        raise ProviderSlotEvidenceError("provider slot evidence fields do not match schema")
    _reject_secret_shaped(value)
    if schema_version not in {SCHEMA, LEGACY_SCHEMA} or value["noruct_version"] != __version__:
        raise ProviderSlotEvidenceError("provider slot evidence version is not accepted")
    if value["source_commit"] != EMPLOYEE_FOUNDATION_COMMIT or value["capsule_tree_sha256"] not in {
        EMPLOYEE_ACTIVE_FORK_TREE_SHA256,
        HISTORICAL_EMPLOYEE_CAPSULE_TREE_SHA256,
    }:
        raise ProviderSlotEvidenceError("provider slot evidence runtime identity changed")
    if not _HEX.fullmatch(_string(value["wheel_sha256"], "wheel_sha256")):
        raise ProviderSlotEvidenceError("provider slot evidence wheel identity is invalid")
    for label in ("provider_id", "model_id"):
        if not _IDENTIFIER.fullmatch(_string(value[label], label)):
            raise ProviderSlotEvidenceError(f"provider slot evidence {label} is invalid")
    slot = _string(value["slot"], "slot")
    if slot not in _SLOT_LIMITS:
        raise ProviderSlotEvidenceError("provider slot evidence slot is invalid")
    if value["operator_slot_authorized"] is not True or value["quota_confirmed"] is not True:
        raise ProviderSlotEvidenceError("provider slot evidence lacks explicit authorization")
    if value["activation"] != "explicit_preview_only" or value["commercial_default_eligible"] is not False or value["shared_network_release_authorized"] is not False:
        raise ProviderSlotEvidenceError("provider slot evidence attempts an unauthorized release claim")
    limits = value["limits"]
    observed = value["observed"]
    if set(limits) != {"max_model_calls", "max_tool_calls", "max_wall_time_ms"}:
        raise ProviderSlotEvidenceError("provider slot evidence limits or observations are invalid")
    max_calls = _integer(limits["max_model_calls"], "max_model_calls")
    calls = _integer(observed["external_model_calls"], "external_model_calls")
    if max_calls != _SLOT_LIMITS[slot] or not 1 <= calls <= max_calls or _integer(limits["max_wall_time_ms"], "max_wall_time_ms") < 1:
        raise ProviderSlotEvidenceError("provider slot evidence model limits are invalid")
    if type(observed["provider_request_id_present"]) is not bool or observed["provider_request_id_present"] is not True:
        raise ProviderSlotEvidenceError("provider slot evidence request identity is missing")
    tool_intents = _integer(observed["tool_intents"], "tool_intents")
    approvals = _integer(observed["approval_events"], "approval_events")
    status = _string(observed["terminal_status"], "terminal_status")
    expected = {
        "direct": (0, 0, "SUCCEEDED"), "read_tool": (1, 0, "SUCCEEDED"),
        "approval": (1, 1, "SUCCEEDED"), "cancel_recovery": (0, 0, "CANCELLED"),
    }[slot]
    if (tool_intents, approvals, status) != expected:
        raise ProviderSlotEvidenceError("provider slot evidence contract observation is invalid")
    recorded_at = _timestamp(value["recorded_at"], "recorded_at")
    if schema_version == SCHEMA:
        for label in (
            "worker_python_sha256",
            "action_policy_sha256",
            "fixture_sha256",
            "event_sequence_sha256",
        ):
            if not _HEX.fullmatch(_string(value[label], label)):
                raise ProviderSlotEvidenceError(
                    f"provider slot evidence {label} is invalid"
                )
        if not _IDENTIFIER.fullmatch(_string(value["adapter_revision"], "adapter_revision")):
            raise ProviderSlotEvidenceError("provider slot evidence adapter_revision is invalid")
        authorized_at = _timestamp(
            value["operator_authorized_at"], "operator_authorized_at"
        )
        if authorized_at > recorded_at:
            raise ProviderSlotEvidenceError(
                "provider slot evidence authorization is after recording"
            )
        if value["usage_accounting"] not in {
            "subscription_quota_usd_unavailable",
            "provider_reported_usd",
        }:
            raise ProviderSlotEvidenceError(
                "provider slot evidence usage accounting is invalid"
            )
        expected_flags = {
            "direct": (False, False, False),
            "read_tool": (True, False, False),
            "approval": (True, True, False),
            "cancel_recovery": (False, False, True),
        }[slot]
        if set(observed) != {
            "external_model_calls", "tool_intents", "approval_events",
            "terminal_status", "provider_request_id_present", "parent_owned_tool",
            "side_effect_committed", "cancellation_event_present",
        }:
            raise ProviderSlotEvidenceError("provider slot evidence observations are invalid")
        flags = tuple(
            observed[key]
            for key in (
                "parent_owned_tool", "side_effect_committed",
                "cancellation_event_present",
            )
        )
        if any(type(flag) is not bool for flag in flags) or flags != expected_flags:
            raise ProviderSlotEvidenceError(
                "provider slot evidence side-effect observation is invalid"
            )
    elif set(observed) != {
        "external_model_calls", "tool_intents", "approval_events", "terminal_status",
        "provider_request_id_present",
    }:
        raise ProviderSlotEvidenceError("provider slot evidence observations are invalid")
    hashed = dict(value)
    evidence_id = _string(hashed.pop("evidence_id"), "evidence_id")
    digest = content_digest({key: value for key, value in hashed.items() if key != "content_hash"})
    if value["content_hash"] != digest or evidence_id != f"employee-provider-slot-{digest[:24]}":
        raise ProviderSlotEvidenceError("provider slot evidence hash is invalid")
    return value


def validate_provider_evidence_matrix(directory: str | Path) -> dict[str, Any]:
    """Require all four bounded slots from one immutable provider identity."""

    root = Path(directory).expanduser().resolve()
    if not root.is_dir() or root.is_symlink():
        raise ProviderSlotEvidenceError("provider evidence matrix directory is unavailable")
    return validate_provider_evidence_matrix_records(
        {slot: root / f"{slot}.json" for slot in sorted(_SLOT_LIMITS)}
    )


def validate_provider_evidence_matrix_records(
    paths: Mapping[str, str | Path],
) -> dict[str, Any]:
    """Validate canonical slot paths without copying or renaming evidence files."""

    if set(paths) != set(_SLOT_LIMITS):
        raise ProviderSlotEvidenceError("provider evidence matrix slots are incomplete")
    records = {slot: validate_provider_slot_evidence(paths[slot]) for slot in sorted(_SLOT_LIMITS)}
    if any(record["schema_version"] != SCHEMA for record in records.values()):
        raise ProviderSlotEvidenceError(
            "provider evidence matrix requires current slot evidence records"
        )
    identity = {
        (
            record["provider_id"], record["model_id"], record["wheel_sha256"],
            record["adapter_revision"], record["worker_python_sha256"],
        )
        for record in records.values()
    }
    if len(identity) != 1:
        raise ProviderSlotEvidenceError("provider evidence matrix mixes provider identities")
    return {
        "schema_version": "noruct.employee-runtime-provider-evidence-matrix.v1",
        "complete": True,
        "slots": tuple(records),
        "provider_id": next(iter(identity))[0],
        "model_id": next(iter(identity))[1],
        "commercial_default_eligible": False,
        "shared_network_release_authorized": False,
    }


def capture_provider_slot_evidence(
    *,
    ledger_path: str | Path,
    run_id: str,
    slot: str,
    wheel_path: str | Path,
    worker_python: str | Path,
    fixture_root: str | Path,
    provider_id: str,
    model_id: str,
    max_wall_time_ms: int,
    operator_authorized_at: str,
    output_path: str | Path,
) -> dict[str, Any]:
    """Build one v2 record from a completed ledger without exporting raw runtime data."""

    if slot not in _SLOT_LIMITS:
        raise ProviderSlotEvidenceError("provider slot evidence slot is invalid")
    ledger = Path(ledger_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    if not ledger.is_file() or ledger.is_symlink() or output.exists():
        raise ProviderSlotEvidenceError("provider slot capture path is unavailable")
    fixture_sha256 = _fixture_digest(fixture_root)
    wheel_sha256 = _file_digest(wheel_path)
    worker_python_sha256 = _file_digest(worker_python)
    try:
        conn = sqlite3.connect(f"file:{ledger}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        run = conn.execute(
            "SELECT status, usage_json, request_json FROM employee_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if run is None:
            raise ProviderSlotEvidenceError("provider slot capture run is unavailable")
        actions = conn.execute(
            "SELECT status FROM tool_actions WHERE run_id = ?", (run_id,)
        ).fetchall()
        approvals = conn.execute(
            "SELECT COUNT(*) AS count FROM approval_requests WHERE run_id = ?", (run_id,)
        ).fetchone()
        events = conn.execute(
            "SELECT seq, event_type, payload_json FROM run_events WHERE run_id = ? ORDER BY seq",
            (run_id,),
        ).fetchall()
    except sqlite3.Error as exc:
        raise ProviderSlotEvidenceError("provider slot capture ledger is invalid") from exc
    finally:
        try:
            conn.close()
        except (UnboundLocalError, sqlite3.Error):
            pass
    try:
        usage = json.loads(str(run["usage_json"]))
        request = json.loads(str(run["request_json"]))
    except (TypeError, json.JSONDecodeError):
        raise ProviderSlotEvidenceError("provider slot capture ledger is invalid") from None
    action_policy = request.get("action_policy")
    if not isinstance(action_policy, dict):
        raise ProviderSlotEvidenceError("provider slot capture action policy is unavailable")
    safe_events: list[dict[str, Any]] = []
    provider_request_id_present = False
    cancellation_event_present = False
    model_call_started = 0
    for event in events:
        try:
            payload = json.loads(str(event["payload_json"]))
        except json.JSONDecodeError:
            raise ProviderSlotEvidenceError("provider slot capture ledger is invalid") from None
        has_request_id = isinstance(payload, dict) and isinstance(
            payload.get("provider_request_id"), str
        ) and bool(payload["provider_request_id"])
        provider_request_id_present = provider_request_id_present or has_request_id
        cancellation_event_present = cancellation_event_present or (
            str(event["event_type"]) == "MODEL_CALL_CANCELLED" and has_request_id
        )
        model_call_started += int(str(event["event_type"]) == "MODEL_CALL_STARTED")
        safe_events.append(
            {"seq": int(event["seq"]), "type": str(event["event_type"]), "provider_request_id_present": has_request_id}
        )
    observed = {
        # A cancellation may stop the transport after a provider request has
        # started but before a completion/usage record exists. The cancel slot
        # therefore counts its bounded started call, while completed slots use
        # committed usage as before.
        "external_model_calls": (
            model_call_started if slot == "cancel_recovery"
            else _integer(usage.get("model_calls"), "model_calls")
        ),
        "tool_intents": len(actions),
        "approval_events": int(approvals["count"]),
        "terminal_status": str(run["status"]),
        "provider_request_id_present": provider_request_id_present,
        "parent_owned_tool": bool(actions),
        "side_effect_committed": slot == "approval" and any(
            str(action["status"]) == "SUCCEEDED" for action in actions
        ),
        "cancellation_event_present": cancellation_event_present,
    }
    recorded_at = datetime.now(UTC).isoformat()
    value = {
        "schema_version": SCHEMA,
        "recorded_at": recorded_at,
        "operator_authorized_at": operator_authorized_at,
        "noruct_version": __version__,
        "source_commit": EMPLOYEE_FOUNDATION_COMMIT,
        "capsule_tree_sha256": EMPLOYEE_ACTIVE_FORK_TREE_SHA256,
        "wheel_sha256": wheel_sha256,
        "worker_python_sha256": worker_python_sha256,
        "adapter_revision": CODEX_EXEC_ADAPTER_REVISION,
        "action_policy_sha256": content_digest(action_policy),
        "fixture_sha256": fixture_sha256,
        "event_sequence_sha256": content_digest(safe_events),
        "usage_accounting": "subscription_quota_usd_unavailable",
        "provider_id": provider_id,
        "model_id": model_id,
        "slot": slot,
        "operator_slot_authorized": True,
        "quota_confirmed": True,
        "activation": "explicit_preview_only",
        "commercial_default_eligible": False,
        "shared_network_release_authorized": False,
        "limits": {
            "max_model_calls": _SLOT_LIMITS[slot],
            "max_tool_calls": 1,
            "max_wall_time_ms": _integer(max_wall_time_ms, "max_wall_time_ms"),
        },
        "observed": observed,
    }
    digest = content_digest(value)
    record = {
        **value,
        "content_hash": digest,
        "evidence_id": f"employee-provider-slot-{digest[:24]}",
    }
    _publish_validated_capture(output, record)
    return record


def _file_digest(path: str | Path) -> str:
    source = Path(path).expanduser().resolve()
    if not source.is_file() or source.is_symlink():
        raise ProviderSlotEvidenceError("provider slot capture identity file is unavailable")
    digest = hashlib.sha256()
    with source.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65_536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fixture_digest(path: str | Path) -> str:
    root = Path(path).expanduser().resolve()
    if not root.is_dir() or root.is_symlink():
        raise ProviderSlotEvidenceError("provider slot capture fixture is unavailable")
    entries = []
    for item in sorted(root.rglob("*")):
        if item.is_symlink():
            raise ProviderSlotEvidenceError("provider slot capture fixture is mutable")
        if item.is_file():
            entries.append({"path": item.relative_to(root).as_posix(), "sha256": _file_digest(item)})
    return content_digest({"fixture_revision": "h2-23-v2", "files": entries})


def _publish_validated_capture(path: Path, value: Mapping[str, Any]) -> Path:
    """Validate a capture before atomically publishing its canonical path.

    A malformed operator timestamp or observation must not leave a record that
    later tooling could mistake for a completed evidence artifact.  The output
    path is published with a no-replace hard link only after validation; an
    existing output is never overwritten.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=".noruct-provider-capture-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True)
        validate_provider_slot_evidence(temporary)
        try:
            os.link(temporary, path)
        except FileExistsError:
            raise ProviderSlotEvidenceError("provider slot capture path is unavailable") from None
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return path
