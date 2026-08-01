"""Durable, content-free ownership receipts for remote effect resources."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .interruption import EffectRecoveryOutcome
from .models import RunStatus, ToolEffect, utc_now
from .redaction import redact_prompt_text


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DEVICE_ID = re.compile(r"^device-[a-z0-9_-]{2,80}$")
_LEASE_ID = re.compile(r"^coord-lease-[0-9a-f-]{16,120}$")
_EFFECTFUL = frozenset(
    {
        ToolEffect.WRITE.value,
        ToolEffect.EXECUTE.value,
        ToolEffect.EXTERNAL_COMMUNICATION.value,
    }
)
_REMOTE_RELEASE_STATUSES = frozenset({"RELEASED", "MISSING"})
_REMOTE_RELEASE_REASONS = frozenset(
    {
        "LOCAL_PRE_HANDLER_REJECTION",
        "LOCAL_HANDLER_PROVED_NO_EFFECT",
        "LOCAL_TERMINAL_RECEIPT",
        "OPERATOR_EFFECT_RESOLUTION",
    }
)


def _action_proves_no_effect(row: Any) -> bool:
    try:
        error = json.loads(str(row["error_json"] or "{}"))
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    return error.get("code") == "TOOL_REJECTED_BEFORE_EFFECT"


class RunStoreRemoteEffectCoordinationMixin:
    """Bind a remote claim to its original authority before network access."""

    def prepare_remote_effect_resource_claim(
        self,
        action_id: str,
        *,
        authority_digest: str,
        origin: str,
        company_scope_digest: str,
        device_id: str,
        resource_digest: str,
        lease_id: str,
    ) -> dict[str, Any]:
        if (
            _SHA256.fullmatch(authority_digest) is None
            or _SHA256.fullmatch(company_scope_digest) is None
            or _SHA256.fullmatch(resource_digest) is None
            or _DEVICE_ID.fullmatch(device_id) is None
            or _LEASE_ID.fullmatch(lease_id) is None
            or lease_id != f"coord-lease-{action_id}"
            or len(origin.encode("utf-8")) > 512
            or not origin.startswith(
                ("https://", "http://localhost", "http://127.0.0.1", "http://[::1]")
            )
        ):
            raise ValueError("Remote effect resource claim identity is invalid")
        with self._transaction() as conn:
            action = conn.execute(
                """
                SELECT action.*, run.job_id
                  FROM tool_actions AS action
                  JOIN employee_runs AS run ON run.run_id = action.run_id
                 WHERE action.action_id = ?
                """,
                (action_id,),
            ).fetchone()
            if action is None:
                raise KeyError(f"Unknown action: {action_id}")
            if str(action["effect"]) not in _EFFECTFUL:
                raise ValueError("Remote resource claims require an effectful action")
            if str(action["status"]) != "INTENT_RECORDED":
                raise RuntimeError("Remote resource claim must be prepared before handler start")
            expected_resource_digest = hashlib.sha256(
                f"noruct.effect-resource.v1|{action['resource_key']}".encode("utf-8")
            ).hexdigest()
            if resource_digest != expected_resource_digest:
                raise ValueError("Remote resource claim does not match the action resource")
            expected = {
                "action_id": action_id,
                "run_id": str(action["run_id"]),
                "job_id": str(action["job_id"]),
                "authority_digest": authority_digest,
                "origin": origin,
                "company_scope_digest": company_scope_digest,
                "device_id": device_id,
                "resource_digest": resource_digest,
                "lease_id": lease_id,
            }
            existing = conn.execute(
                "SELECT * FROM effect_remote_resource_claims WHERE action_id = ?",
                (action_id,),
            ).fetchone()
            if existing is not None:
                if any(str(existing[key]) != value for key, value in expected.items()):
                    raise ValueError("Remote effect resource claim identity changed")
                return {**dict(existing), "prepared": True}
            prepared_at = utc_now().isoformat()
            conn.execute(
                """
                INSERT INTO effect_remote_resource_claims(
                    action_id, run_id, job_id, authority_digest, origin,
                    company_scope_digest, device_id, resource_digest,
                    lease_id, prepared_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action_id,
                    expected["run_id"],
                    expected["job_id"],
                    authority_digest,
                    origin,
                    company_scope_digest,
                    device_id,
                    resource_digest,
                    lease_id,
                    prepared_at,
                ),
            )
            return {**expected, "prepared_at": prepared_at, "prepared": True}

    def remote_effect_resource_claim(
        self,
        *,
        job_id: str,
        action_id: str,
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT claim.*, action.status AS action_status,
                       action.started_at, action.error_json,
                       run.status AS run_status,
                       release.remote_status, release.release_reason,
                       release.released_at,
                       resolution.outcome AS remote_resolution_outcome,
                       resolution.evidence_digest AS remote_resolution_evidence,
                       resolution.resolved_by AS remote_resolved_by,
                       resolution.reason_safe AS remote_resolution_reason,
                       resolution.resolved_at AS remote_resolved_at,
                       CASE WHEN recovery.action_id IS NULL THEN 0 ELSE 1 END
                           AS has_effect_recovery_case
                  FROM effect_remote_resource_claims AS claim
                  JOIN tool_actions AS action ON action.action_id = claim.action_id
                  JOIN employee_runs AS run ON run.run_id = claim.run_id
             LEFT JOIN effect_remote_resource_releases AS release
                    ON release.action_id = claim.action_id
             LEFT JOIN effect_remote_resource_resolutions AS resolution
                    ON resolution.action_id = claim.action_id
             LEFT JOIN effect_recovery_cases AS recovery
                    ON recovery.action_id = claim.action_id
                 WHERE claim.job_id = ? AND claim.action_id = ?
                """,
                (job_id, action_id),
            ).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["has_effect_recovery_case"] = bool(value["has_effect_recovery_case"])
        value["remote_closed"] = value["remote_status"] in _REMOTE_RELEASE_STATUSES
        return value

    def list_job_remote_effect_resource_claims(
        self,
        job_id: str,
    ) -> tuple[dict[str, Any], ...]:
        """Project remote-only recovery facts without owner or resource data."""

        with self._lock:
            rows = self._conn.execute(
                """
                SELECT claim.action_id, claim.job_id, action.effect,
                       action.status AS action_status, action.started_at,
                       action.error_json,
                       run.status AS run_status, release.remote_status,
                       resolution.outcome AS resolution_outcome,
                       resolution.resolved_at
                  FROM effect_remote_resource_claims AS claim
                  JOIN tool_actions AS action ON action.action_id = claim.action_id
                  JOIN employee_runs AS run ON run.run_id = claim.run_id
             LEFT JOIN effect_remote_resource_releases AS release
                    ON release.action_id = claim.action_id
             LEFT JOIN effect_remote_resource_resolutions AS resolution
                    ON resolution.action_id = claim.action_id
             LEFT JOIN effect_recovery_cases AS recovery
                    ON recovery.action_id = claim.action_id
                 WHERE claim.job_id = ? AND recovery.action_id IS NULL
                 ORDER BY claim.prepared_at, claim.action_id
                """,
                (job_id,),
            ).fetchall()
        projected = []
        for row in rows:
            remote_status = (
                str(row["remote_status"])
                if row["remote_status"] in _REMOTE_RELEASE_STATUSES
                else "OPEN"
            )
            if remote_status in _REMOTE_RELEASE_STATUSES:
                case_status = "CLOSED"
                next_action = "NONE"
            elif not RunStatus(str(row["run_status"])).terminal:
                case_status = "OPEN"
                next_action = "WAIT_FOR_TERMINAL"
            elif row["started_at"] is None or _action_proves_no_effect(row):
                case_status = "OPEN"
                next_action = "CONFIRM_NO_EFFECT_AND_RELEASE_EXACT_OWNER"
            elif str(row["action_status"]) == "SUCCEEDED":
                case_status = "OPEN"
                next_action = "CONFIRM_SUCCEEDED_AND_RELEASE_EXACT_OWNER"
            else:
                case_status = "FAIL_CLOSED"
                next_action = "MANUAL_INVESTIGATION_NO_RELEASE"
            projected.append(
                {
                    "job_id": str(row["job_id"]),
                    "action_id": str(row["action_id"]),
                    "effect": str(row["effect"]),
                    "action_status": str(row["action_status"]),
                    "run_status": str(row["run_status"]),
                    "case_status": case_status,
                    "remote_status": remote_status,
                    "resolution_outcome": (
                        None
                        if row["resolution_outcome"] is None
                        else str(row["resolution_outcome"])
                    ),
                    "resolved_at": (
                        None
                        if row["resolved_at"] is None
                        else str(row["resolved_at"])
                    ),
                    "next_action": next_action,
                }
            )
        return tuple(projected)

    def record_remote_effect_resource_release(
        self,
        *,
        job_id: str,
        action_id: str,
        remote_status: str,
        release_reason: str,
    ) -> dict[str, Any]:
        if remote_status not in _REMOTE_RELEASE_STATUSES:
            raise ValueError("Remote effect resource release status is invalid")
        if release_reason not in _REMOTE_RELEASE_REASONS:
            raise ValueError("Remote effect resource release reason is invalid")
        with self._transaction() as conn:
            claim = conn.execute(
                "SELECT * FROM effect_remote_resource_claims WHERE action_id = ?",
                (action_id,),
            ).fetchone()
            if claim is None or str(claim["job_id"]) != job_id:
                raise KeyError(f"Unknown remote effect resource claim: {action_id}")
            existing = conn.execute(
                "SELECT * FROM effect_remote_resource_releases WHERE action_id = ?",
                (action_id,),
            ).fetchone()
            if existing is not None:
                return dict(existing)
            released_at = utc_now().isoformat()
            conn.execute(
                """
                INSERT INTO effect_remote_resource_releases(
                    action_id, remote_status, release_reason, released_at
                ) VALUES (?, ?, ?, ?)
                """,
                (action_id, remote_status, release_reason, released_at),
            )
            return {
                "action_id": action_id,
                "remote_status": remote_status,
                "release_reason": release_reason,
                "released_at": released_at,
            }

    def prepare_remote_effect_resolution(
        self,
        *,
        job_id: str,
        action_id: str,
        outcome: EffectRecoveryOutcome,
        evidence_digest: str | None,
        resolved_by: str,
        reason: str,
    ) -> dict[str, Any]:
        """Freeze one releasing conclusion before its remote side effect."""

        digest = evidence_digest.strip() if evidence_digest else None
        actor = redact_prompt_text(resolved_by.strip())[:160]
        reason_safe = redact_prompt_text(reason.strip())[:500]
        if not outcome.releases_resource:
            raise ValueError("A sealed-unknown conclusion must not prepare remote release")
        if digest is None or _SHA256.fullmatch(digest) is None:
            raise ValueError("Remote effect resolution requires evidence SHA-256")
        if not actor or not reason_safe:
            raise ValueError("Remote effect resolution requires an operator and reason")
        expected = {
            "outcome": outcome.value,
            "evidence_digest": digest,
            "resolved_by": actor,
            "reason_safe": reason_safe,
        }
        with self._transaction() as conn:
            recovery = conn.execute(
                """
                SELECT recovery.job_id, run.status AS run_status
                  FROM effect_recovery_cases AS recovery
                  JOIN employee_runs AS run ON run.run_id = recovery.run_id
                 WHERE recovery.action_id = ?
                """,
                (action_id,),
            ).fetchone()
            if recovery is None or str(recovery["job_id"]) != job_id:
                raise KeyError(f"Unknown effect recovery case: {action_id}")
            if not RunStatus(str(recovery["run_status"])).terminal:
                raise ValueError("An active run cannot resolve an indeterminate effect")
            resolution = conn.execute(
                "SELECT * FROM effect_recovery_resolutions WHERE action_id = ?",
                (action_id,),
            ).fetchone()
            if resolution is not None and any(
                resolution[key] != value for key, value in expected.items()
            ):
                raise ValueError("Effect recovery case already has a different resolution")
            prepared = conn.execute(
                "SELECT * FROM effect_remote_resolution_preparations WHERE action_id = ?",
                (action_id,),
            ).fetchone()
            if prepared is not None:
                if any(prepared[key] != value for key, value in expected.items()):
                    raise ValueError("Effect remote release already has a different resolution")
                return dict(prepared)
            prepared_at = utc_now().isoformat()
            conn.execute(
                """
                INSERT INTO effect_remote_resolution_preparations(
                    action_id, outcome, evidence_digest, resolved_by,
                    reason_safe, prepared_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    action_id,
                    outcome.value,
                    digest,
                    actor,
                    reason_safe,
                    prepared_at,
                ),
            )
        return {"action_id": action_id, **expected, "prepared_at": prepared_at}

    def resolve_terminal_remote_effect_resource(
        self,
        *,
        job_id: str,
        action_id: str,
        outcome: EffectRecoveryOutcome,
        evidence_digest: str | None,
        resolved_by: str,
        reason: str,
    ) -> dict[str, Any]:
        """Close a stranded remote claim when no handler-unknown case exists."""

        digest = evidence_digest.strip() if evidence_digest else None
        actor = redact_prompt_text(resolved_by.strip())[:160]
        reason_safe = redact_prompt_text(reason.strip())[:500]
        if digest is None or _SHA256.fullmatch(digest) is None:
            raise ValueError("Remote effect resolution requires evidence SHA-256")
        if not actor or not reason_safe:
            raise ValueError("Remote effect resolution requires an operator and reason")
        with self._transaction() as conn:
            claim = conn.execute(
                """
                SELECT claim.*, action.status AS action_status,
                       action.started_at, action.error_json,
                       run.status AS run_status,
                       release.remote_status,
                       CASE WHEN recovery.action_id IS NULL THEN 0 ELSE 1 END
                           AS has_effect_recovery_case
                  FROM effect_remote_resource_claims AS claim
                  JOIN tool_actions AS action ON action.action_id = claim.action_id
                  JOIN employee_runs AS run ON run.run_id = claim.run_id
             LEFT JOIN effect_remote_resource_releases AS release
                    ON release.action_id = claim.action_id
             LEFT JOIN effect_recovery_cases AS recovery
                    ON recovery.action_id = claim.action_id
                 WHERE claim.action_id = ?
                """,
                (action_id,),
            ).fetchone()
            if claim is None or str(claim["job_id"]) != job_id:
                raise KeyError(f"Unknown remote effect resource claim: {action_id}")
            if bool(claim["has_effect_recovery_case"]):
                raise ValueError("Indeterminate handlers require the effect recovery case")
            if not RunStatus(str(claim["run_status"])).terminal:
                raise ValueError("An active run cannot resolve a remote effect claim")
            if str(claim["remote_status"]) not in _REMOTE_RELEASE_STATUSES:
                raise ValueError("Remote effect resource must be closed before local resolution")
            if claim["started_at"] is None or _action_proves_no_effect(claim):
                required_outcome = EffectRecoveryOutcome.CONFIRMED_NO_EFFECT
            elif str(claim["action_status"]) == "SUCCEEDED":
                required_outcome = EffectRecoveryOutcome.CONFIRMED_SUCCEEDED
            else:
                raise ValueError("Terminal action evidence cannot prove a safe remote resolution")
            if outcome is not required_outcome:
                raise ValueError(
                    f"Remote effect claim requires {required_outcome.value}"
                )
            expected = {
                "outcome": outcome.value,
                "evidence_digest": digest,
                "resolved_by": actor,
                "reason_safe": reason_safe,
            }
            existing = conn.execute(
                "SELECT * FROM effect_remote_resource_resolutions WHERE action_id = ?",
                (action_id,),
            ).fetchone()
            if existing is not None:
                if any(existing[key] != value for key, value in expected.items()):
                    raise ValueError("Remote effect claim already has a different resolution")
                resolved_at = str(existing["resolved_at"])
            else:
                resolved_at = utc_now().isoformat()
                conn.execute(
                    """
                    INSERT INTO effect_remote_resource_resolutions(
                        action_id, outcome, evidence_digest, resolved_by,
                        reason_safe, resolved_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        action_id,
                        outcome.value,
                        digest,
                        actor,
                        reason_safe,
                        resolved_at,
                    ),
                )
                conn.execute(
                    "DELETE FROM effect_resource_leases WHERE owner_action_id = ?",
                    (action_id,),
                )
        return {
            "job_id": job_id,
            "action_id": action_id,
            **expected,
            "resource_released": True,
            "remote_resource_released": True,
            "remote_status": str(claim["remote_status"]),
            "resolved_at": resolved_at,
        }

    def validate_terminal_remote_effect_resolution(
        self,
        *,
        job_id: str,
        action_id: str,
        outcome: EffectRecoveryOutcome,
        evidence_digest: str | None,
        resolved_by: str,
        reason: str,
    ) -> None:
        """Reject an unsafe remote-only conclusion before any network write."""

        digest = evidence_digest.strip() if evidence_digest else None
        if digest is None or _SHA256.fullmatch(digest) is None:
            raise ValueError("Remote effect resolution requires evidence SHA-256")
        if not redact_prompt_text(resolved_by.strip())[:160] or not redact_prompt_text(
            reason.strip()
        )[:500]:
            raise ValueError("Remote effect resolution requires an operator and reason")
        claim = self.remote_effect_resource_claim(job_id=job_id, action_id=action_id)
        if claim is None:
            raise KeyError(f"Unknown remote effect resource claim: {action_id}")
        if bool(claim["has_effect_recovery_case"]):
            raise ValueError("Indeterminate handlers require the effect recovery case")
        if not RunStatus(str(claim["run_status"])).terminal:
            raise ValueError("An active run cannot resolve a remote effect claim")
        if claim["started_at"] is None or _action_proves_no_effect(claim):
            required_outcome = EffectRecoveryOutcome.CONFIRMED_NO_EFFECT
        elif str(claim["action_status"]) == "SUCCEEDED":
            required_outcome = EffectRecoveryOutcome.CONFIRMED_SUCCEEDED
        else:
            raise ValueError("Terminal action evidence cannot prove a safe remote resolution")
        if outcome is not required_outcome:
            raise ValueError(f"Remote effect claim requires {required_outcome.value}")
