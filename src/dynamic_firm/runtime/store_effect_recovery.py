"""Durable, non-replaying recovery cases for indeterminate external effects."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .interruption import (
    EffectInterruptionReason,
    EffectRecoveryOutcome,
    RecoveryDisposition,
)
from .models import EventType, RunStatus, ToolEffect, ToolResult, to_primitive, utc_now
from .redaction import redact_prompt_text, redact_runtime_value


_EFFECTFUL = frozenset(
    {
        ToolEffect.WRITE.value,
        ToolEffect.EXECUTE.value,
        ToolEffect.EXTERNAL_COMMUNICATION.value,
    }
)
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _safe_json(value: Any) -> str:
    return json.dumps(
        redact_runtime_value(to_primitive(value)),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _unknown_result(action: Any) -> ToolResult:
    return ToolResult(
        call_id=str(action["tool_call_id"]),
        name=str(action["tool_name"]),
        ok=False,
        content=(
            "The effect outcome is unknown. This action is sealed and will not "
            "be executed again automatically."
        ),
        action_id=str(action["action_id"]),
        error_code="EFFECT_OUTCOME_UNKNOWN",
    )


class RunStoreEffectRecoveryMixin:
    """Keep an unknown effect sealed until an explicit evidence receipt exists."""

    def mark_tool_effect_indeterminate(
        self,
        action_id: str,
        *,
        cause: EffectInterruptionReason,
    ) -> ToolResult:
        event = None
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
                raise ValueError("Only an effectful started action can be indeterminate")
            status = str(action["status"])
            if status == "INDETERMINATE":
                result_json = json.loads(str(action["result_json"] or "{}"))
                return ToolResult(
                    call_id=str(result_json["call_id"]),
                    name=str(result_json["name"]),
                    ok=False,
                    content=str(result_json["content"]),
                    action_id=action_id,
                    error_code="EFFECT_OUTCOME_UNKNOWN",
                    replayed=True,
                )
            if status != "STARTED":
                raise RuntimeError(
                    f"Effect action must be STARTED before it can become indeterminate: {action_id}"
                )

            result = _unknown_result(action)
            now = utc_now().isoformat()
            resource_digest = hashlib.sha256(
                f"noruct.effect-resource.v1|{action['resource_key']}".encode("utf-8")
            ).hexdigest()
            conn.execute(
                """
                INSERT INTO effect_recovery_cases(
                    action_id, run_id, job_id, resource_digest, effect, cause,
                    disposition, detected_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action_id,
                    action["run_id"],
                    action["job_id"],
                    resource_digest,
                    action["effect"],
                    cause.value,
                    RecoveryDisposition.RECONCILE_OR_COMPENSATE_REQUIRED.value,
                    now,
                ),
            )
            conn.execute(
                """
                UPDATE tool_actions
                   SET status = 'INDETERMINATE', result_json = ?, error_json = ?
                 WHERE action_id = ? AND status = 'STARTED'
                """,
                (
                    _safe_json(result),
                    _safe_json({"code": "EFFECT_OUTCOME_UNKNOWN", "cause": cause.value}),
                    action_id,
                ),
            )
            run = conn.execute(
                "SELECT * FROM employee_runs WHERE run_id = ?",
                (action["run_id"],),
            ).fetchone()
            event = self._insert_event(
                conn,
                run,
                EventType.TOOL_EFFECT_OUTCOME_UNKNOWN,
                {
                    "action_id": action_id,
                    "tool_name": action["tool_name"],
                    "effect": action["effect"],
                    "cause": cause.value,
                    "resource_digest": resource_digest,
                },
            )
        self._notify(event)
        return result

    def mark_run_started_effects_indeterminate(
        self,
        run_id: str,
        *,
        cause: EffectInterruptionReason,
    ) -> tuple[str, ...]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT action_id FROM tool_actions
                 WHERE run_id = ? AND status = 'STARTED'
                   AND effect IN (?, ?, ?)
                 ORDER BY created_at, action_id
                """,
                (run_id, *_EFFECTFUL),
            ).fetchall()
        action_ids = tuple(str(row["action_id"]) for row in rows)
        for candidate in action_ids:
            self.mark_tool_effect_indeterminate(candidate, cause=cause)
        return action_ids

    def list_job_effect_recovery_cases(self, job_id: str) -> tuple[dict[str, Any], ...]:
        """Return content-free cases and resolutions for an operator surface."""

        with self._lock:
            rows = self._conn.execute(
                """
                SELECT recovery.action_id, recovery.run_id, recovery.job_id,
                       recovery.resource_digest, recovery.effect, recovery.cause,
                       recovery.disposition, recovery.detected_at,
                       action.tool_name, action.status AS action_status,
                       run.status AS run_status,
                       resolution.outcome, resolution.evidence_digest,
                       resolution.resolved_by, resolution.reason_safe,
                       resolution.resource_released, resolution.resolved_at,
                       CASE WHEN lease.owner_action_id IS NULL THEN 0 ELSE 1 END AS lease_held
                  FROM effect_recovery_cases AS recovery
                  JOIN tool_actions AS action ON action.action_id = recovery.action_id
                  JOIN employee_runs AS run ON run.run_id = recovery.run_id
             LEFT JOIN effect_recovery_resolutions AS resolution
                    ON resolution.action_id = recovery.action_id
             LEFT JOIN effect_resource_leases AS lease
                    ON lease.owner_action_id = recovery.action_id
                 WHERE recovery.job_id = ?
                 ORDER BY recovery.detected_at, recovery.action_id
                """,
                (job_id,),
            ).fetchall()
        return tuple(
            {
                **dict(row),
                "case_status": (
                    "OPEN"
                    if row["outcome"] is None
                    else (
                        "RESOLVED"
                        if bool(row["resource_released"])
                        else "SEALED_UNKNOWN"
                    )
                ),
                "lease_held": bool(row["lease_held"]),
                "resource_released": (
                    None
                    if row["resource_released"] is None
                    else bool(row["resource_released"])
                ),
            }
            for row in rows
        )

    def resolve_effect_recovery_case(
        self,
        *,
        job_id: str,
        action_id: str,
        outcome: EffectRecoveryOutcome,
        evidence_digest: str | None,
        resolved_by: str,
        reason: str,
    ) -> dict[str, Any]:
        """Append one operator conclusion without rewriting the tool receipt."""

        actor = redact_prompt_text(resolved_by.strip())[:160]
        reason_safe = redact_prompt_text(reason.strip())[:500]
        if not actor or not reason_safe:
            raise ValueError("Effect resolution requires an operator and reason")
        digest = evidence_digest.strip() if evidence_digest else None
        if digest is not None and _SHA256.fullmatch(digest) is None:
            raise ValueError("Effect resolution evidence must be a lowercase SHA-256 digest")
        if outcome.releases_resource and digest is None:
            raise ValueError("A resource-releasing effect resolution requires evidence")

        with self._transaction() as conn:
            recovery = conn.execute(
                """
                SELECT recovery.*, run.status AS run_status
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
            existing = conn.execute(
                "SELECT * FROM effect_recovery_resolutions WHERE action_id = ?",
                (action_id,),
            ).fetchone()
            expected = {
                "outcome": outcome.value,
                "evidence_digest": digest,
                "resolved_by": actor,
                "reason_safe": reason_safe,
                "resource_released": int(outcome.releases_resource),
            }
            remote_claim = conn.execute(
                "SELECT 1 FROM effect_remote_resource_claims WHERE action_id = ?",
                (action_id,),
            ).fetchone()
            if remote_claim is not None:
                remote_preparation = conn.execute(
                    "SELECT * FROM effect_remote_resolution_preparations WHERE action_id = ?",
                    (action_id,),
                ).fetchone()
                remote_release = conn.execute(
                    "SELECT 1 FROM effect_remote_resource_releases WHERE action_id = ?",
                    (action_id,),
                ).fetchone()
                if outcome.releases_resource:
                    if remote_preparation is None or any(
                        remote_preparation[key] != expected[key]
                        for key in (
                            "outcome",
                            "evidence_digest",
                            "resolved_by",
                            "reason_safe",
                        )
                    ):
                        raise ValueError(
                            "Remote effect release preparation does not match the operator resolution"
                        )
                    if remote_release is None:
                        raise ValueError(
                            "Remote effect resource must be closed before local resolution"
                        )
                elif remote_preparation is not None:
                    raise ValueError(
                        "A prepared remote release cannot be replaced by SEALED_UNKNOWN"
                    )
            if existing is not None:
                if any(existing[key] != value for key, value in expected.items()):
                    raise ValueError("Effect recovery case already has a different resolution")
                resolved_at = str(existing["resolved_at"])
            else:
                resolved_at = utc_now().isoformat()
                conn.execute(
                    """
                    INSERT INTO effect_recovery_resolutions(
                        action_id, outcome, evidence_digest, resolved_by,
                        reason_safe, resource_released, resolved_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        action_id,
                        outcome.value,
                        digest,
                        actor,
                        reason_safe,
                        int(outcome.releases_resource),
                        resolved_at,
                    ),
                )
                if outcome.releases_resource:
                    conn.execute(
                        "DELETE FROM effect_resource_leases WHERE owner_action_id = ?",
                        (action_id,),
                    )
        return {
            "job_id": job_id,
            "action_id": action_id,
            **expected,
            "resource_released": bool(expected["resource_released"]),
            "resolved_at": resolved_at,
        }

    def effect_resource_is_sealed(self, resource_digest: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT 1
                  FROM effect_recovery_cases AS recovery
             LEFT JOIN effect_recovery_resolutions AS resolution
                    ON resolution.action_id = recovery.action_id
                 WHERE recovery.resource_digest = ?
                   AND COALESCE(resolution.resource_released, 0) = 0
                 LIMIT 1
                """,
                (resource_digest,),
            ).fetchone()
        return row is not None
