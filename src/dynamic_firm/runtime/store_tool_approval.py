"""Approval and Tool-action lifecycle mixed into the canonical RunStore.

The owning RunStore retains the one SQLite connection, transaction boundary,
event sequence and subscriber notification authority.  This mixin owns only
the durable approval and tool-action transition methods.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from typing import Any, Mapping

from dynamic_firm._vendor.paperclip_runtime.approval_resolution import (
    classify_approval_transition,
)

from .models import (
    ApprovalDecision,
    ApprovalRecord,
    ApprovalRequest,
    ApprovalResolutionReceipt,
    ApprovalResumeState,
    EventType,
    RunEvent,
    RunStatus,
    ToolCall,
    ToolEffect,
    ToolResult,
    ToolRisk,
    Usage,
    to_primitive,
    usage_from_dict,
    utc_now,
)
from .redaction import redact_prompt_text, redact_runtime_value


class ApprovalConflict(RuntimeError):
    """A durable approval was reused with a different request or decision."""


def _json(value: Any) -> str:
    return json.dumps(
        to_primitive(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _safe_json(value: Any) -> str:
    return _json(redact_runtime_value(to_primitive(value)))


def _digest_json(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _loads(value: str | None, default: Any) -> Any:
    return json.loads(value) if value else default


class RunStoreToolApprovalMixin:
    """Durable Tool intent, approval and terminal-receipt transitions."""

    @staticmethod
    def _approval_record(row: Mapping[str, Any]) -> ApprovalRecord:
        value = _loads(str(row["request_json"]), {})
        request = ApprovalRequest(
            action_id=str(value["action_id"]),
            run_id=str(value["run_id"]),
            job_id=str(value["job_id"]),
            task_id=str(value["task_id"]),
            employee_id=str(value["employee_id"]),
            tool_name=str(value["tool_name"]),
            effect=ToolEffect(value["effect"]),
            risk=ToolRisk(value["risk"]),
            resource_key=str(value["resource_key"]),
            preview=str(value["preview"]),
            allow_session=bool(value.get("allow_session", False)),
        )
        decision = (
            ApprovalDecision(str(row["decision"]))
            if row.get("decision") is not None
            else None
        )
        if row.get("resume_completed_at"):
            resume_state = ApprovalResumeState.COMPLETED
        elif row.get("resume_claimed_at"):
            resume_state = ApprovalResumeState.CLAIMED
        elif decision is not None:
            resume_state = ApprovalResumeState.READY
        else:
            resume_state = ApprovalResumeState.WAITING
        return ApprovalRecord(
            request=request,
            request_hash=str(row["request_hash"]),
            decision=decision,
            decided_by=(str(row["decided_by"]) if row.get("decided_by") else None),
            resume_state=resume_state,
            created_at=datetime.fromisoformat(str(row["created_at"])),
            resolved_at=(
                datetime.fromisoformat(str(row["resolved_at"]))
                if row.get("resolved_at")
                else None
            ),
            resume_claimed_at=(
                datetime.fromisoformat(str(row["resume_claimed_at"]))
                if row.get("resume_claimed_at")
                else None
            ),
            resume_completed_at=(
                datetime.fromisoformat(str(row["resume_completed_at"]))
                if row.get("resume_completed_at")
                else None
            ),
        )

    def get_approval(self, action_id: str) -> ApprovalRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM approval_requests WHERE action_id = ?",
                (action_id,),
            ).fetchone()
        return self._approval_record(dict(row)) if row else None

    def list_pending_approvals(self, run_id: str | None = None) -> list[ApprovalRecord]:
        query = "SELECT * FROM approval_requests WHERE decision IS NULL"
        values: tuple[Any, ...] = ()
        if run_id is not None:
            query += " AND run_id = ?"
            values = (run_id,)
        query += " ORDER BY created_at, action_id"
        with self._lock:
            rows = self._conn.execute(query, values).fetchall()
        return [self._approval_record(dict(row)) for row in rows]

    def record_approval_request(
        self,
        request: ApprovalRequest,
    ) -> tuple[ApprovalRecord, bool]:
        safe_request = redact_runtime_value(to_primitive(request))
        if not isinstance(safe_request, dict):
            raise ValueError("Approval request projection must be an object")
        request_hash = _digest_json(safe_request)
        request_json = _json(safe_request)
        event: RunEvent | None = None
        with self._transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM approval_requests WHERE action_id = ?",
                (request.action_id,),
            ).fetchone()
            if existing:
                if str(existing["request_hash"]) != request_hash:
                    raise ApprovalConflict(
                        f"Approval action {request.action_id} was reused with a different request"
                    )
                return self._approval_record(dict(existing)), False
            action = conn.execute(
                "SELECT * FROM tool_actions WHERE action_id = ?",
                (request.action_id,),
            ).fetchone()
            if not action or str(action["run_id"]) != request.run_id:
                raise KeyError(f"Unknown approval action: {request.action_id}")
            if str(action["status"]) != "INTENT_RECORDED":
                raise RuntimeError("Approval must be recorded before a tool action starts")
            run = conn.execute(
                "SELECT * FROM employee_runs WHERE run_id = ?",
                (request.run_id,),
            ).fetchone()
            if not run or RunStatus(str(run["status"])).terminal:
                raise RuntimeError("Approval request belongs to a missing or terminal run")
            now = utc_now()
            conn.execute(
                """
                INSERT INTO approval_requests(
                    action_id, run_id, request_hash, request_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    request.action_id,
                    request.run_id,
                    request_hash,
                    request_json,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            conn.execute(
                "UPDATE employee_runs SET status = ?, updated_at = ? WHERE run_id = ?",
                (RunStatus.WAITING_APPROVAL.value, now.isoformat(), request.run_id),
            )
            updated_run = conn.execute(
                "SELECT * FROM employee_runs WHERE run_id = ?",
                (request.run_id,),
            ).fetchone()
            event = self._insert_event(
                conn,
                updated_run,
                EventType.APPROVAL_REQUIRED,
                {
                    "action_id": request.action_id,
                    "tool_name": request.tool_name,
                    "effect": request.effect.value,
                    "risk": request.risk.value,
                    "resource_key": request.resource_key,
                    "preview": request.preview,
                    "allow_session": request.allow_session,
                    "request_hash": request_hash,
                },
            )
            created = conn.execute(
                "SELECT * FROM approval_requests WHERE action_id = ?",
                (request.action_id,),
            ).fetchone()
        assert event is not None and created is not None
        self._notify(event)
        return self._approval_record(dict(created)), True

    def resolve_approval(
        self,
        action_id: str,
        decision: ApprovalDecision,
        *,
        decided_by: str = "interactive-user",
    ) -> ApprovalResolutionReceipt:
        event: RunEvent | None = None
        with self._transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM approval_requests WHERE action_id = ?",
                (action_id,),
            ).fetchone()
            if not existing:
                raise KeyError(f"Unknown approval: {action_id}")
            request_value = _loads(str(existing["request_json"]), {})
            if decision == ApprovalDecision.ALLOW_SESSION and not bool(
                request_value.get("allow_session", False)
            ):
                raise ApprovalConflict("This approval does not allow a session-wide grant")
            transition = classify_approval_transition(
                str(existing["decision"]) if existing["decision"] is not None else None,
                decision.value,
            )
            if transition.conflict:
                raise ApprovalConflict(
                    f"Approval {action_id} is already resolved as {existing['decision']}"
                )
            if not transition.applied:
                return ApprovalResolutionReceipt(
                    self._approval_record(dict(existing)),
                    False,
                )
            current_run = conn.execute(
                "SELECT status FROM employee_runs WHERE run_id = ?",
                (existing["run_id"],),
            ).fetchone()
            if not current_run or RunStatus(str(current_run["status"])).terminal:
                raise ApprovalConflict("A terminal run cannot accept a new approval decision")
            now = utc_now()
            actor = redact_prompt_text(decided_by.strip() or "interactive-user")
            changed = conn.execute(
                """
                UPDATE approval_requests
                SET decision = ?, decided_by = ?, resolved_at = ?, updated_at = ?
                WHERE action_id = ? AND decision IS NULL
                """,
                (decision.value, actor, now.isoformat(), now.isoformat(), action_id),
            ).rowcount
            latest = conn.execute(
                "SELECT * FROM approval_requests WHERE action_id = ?",
                (action_id,),
            ).fetchone()
            if not changed:
                raced = classify_approval_transition(
                    str(latest["decision"]) if latest["decision"] is not None else None,
                    decision.value,
                )
                if raced.conflict or latest["decision"] is None:
                    raise ApprovalConflict(f"Approval {action_id} resolved concurrently")
                return ApprovalResolutionReceipt(
                    self._approval_record(dict(latest)),
                    False,
                )
            run = conn.execute(
                "SELECT * FROM employee_runs WHERE run_id = ?",
                (latest["run_id"],),
            ).fetchone()
            event = self._insert_event(
                conn,
                run,
                EventType.APPROVAL_RESOLVED,
                {
                    "action_id": action_id,
                    "tool_name": request_value.get("tool_name"),
                    "decision": decision.value,
                    "decided_by": actor,
                },
            )
        assert event is not None and latest is not None
        self._notify(event)
        return ApprovalResolutionReceipt(self._approval_record(dict(latest)), True)

    def claim_approval_resume(self, action_id: str) -> bool:
        event: RunEvent | None = None
        with self._transaction() as conn:
            approval = conn.execute(
                "SELECT * FROM approval_requests WHERE action_id = ?",
                (action_id,),
            ).fetchone()
            if not approval:
                raise KeyError(f"Unknown approval: {action_id}")
            if approval["decision"] is None:
                raise RuntimeError("An unresolved approval cannot resume")
            if approval["resume_claimed_at"] is not None:
                return False
            current_run = conn.execute(
                "SELECT status FROM employee_runs WHERE run_id = ?",
                (approval["run_id"],),
            ).fetchone()
            if (
                not current_run
                or RunStatus(str(current_run["status"]))
                != RunStatus.WAITING_APPROVAL
            ):
                raise RuntimeError("Approval resume requires a waiting run")
            action = conn.execute(
                "SELECT * FROM tool_actions WHERE action_id = ?",
                (action_id,),
            ).fetchone()
            if not action or str(action["status"]) != "INTENT_RECORDED":
                raise RuntimeError("Approval resume requires an unstarted tool intent")
            now = utc_now()
            changed = conn.execute(
                """
                UPDATE approval_requests
                SET resume_claimed_at = ?, updated_at = ?
                WHERE action_id = ? AND decision IS NOT NULL
                    AND resume_claimed_at IS NULL
                """,
                (now.isoformat(), now.isoformat(), action_id),
            ).rowcount
            if not changed:
                return False
            conn.execute(
                "UPDATE employee_runs SET status = ?, updated_at = ? WHERE run_id = ?",
                (RunStatus.RUNNING.value, now.isoformat(), approval["run_id"]),
            )
            run = conn.execute(
                "SELECT * FROM employee_runs WHERE run_id = ?",
                (approval["run_id"],),
            ).fetchone()
            event = self._insert_event(
                conn,
                run,
                EventType.APPROVAL_RESUME_CLAIMED,
                {"action_id": action_id, "decision": str(approval["decision"])},
            )
        assert event is not None
        self._notify(event)
        return True

    def complete_approval_resume(self, action_id: str) -> bool:
        event: RunEvent | None = None
        with self._transaction() as conn:
            approval = conn.execute(
                "SELECT * FROM approval_requests WHERE action_id = ?",
                (action_id,),
            ).fetchone()
            if not approval:
                return False
            if approval["resume_completed_at"] is not None:
                return False
            if approval["resume_claimed_at"] is None:
                raise RuntimeError("Approval resume was not claimed")
            action = conn.execute(
                "SELECT * FROM tool_actions WHERE action_id = ?",
                (action_id,),
            ).fetchone()
            if not action or str(action["status"]) not in {"SUCCEEDED", "FAILED"}:
                raise RuntimeError("Approval resume cannot complete before the tool action")
            now = utc_now()
            changed = conn.execute(
                """
                UPDATE approval_requests
                SET resume_completed_at = ?, updated_at = ?
                WHERE action_id = ? AND resume_claimed_at IS NOT NULL
                    AND resume_completed_at IS NULL
                """,
                (now.isoformat(), now.isoformat(), action_id),
            ).rowcount
            if not changed:
                return False
            run = conn.execute(
                "SELECT * FROM employee_runs WHERE run_id = ?",
                (approval["run_id"],),
            ).fetchone()
            event = self._insert_event(
                conn,
                run,
                EventType.APPROVAL_RESUME_COMPLETED,
                {"action_id": action_id, "tool_status": str(action["status"])},
            )
        assert event is not None
        self._notify(event)
        return True

    def record_tool_intent(
        self,
        run_id: str,
        action_id: str,
        model_call_index: int,
        call: ToolCall,
        arguments_hash: str,
        resource_key: str,
        *,
        effect: ToolEffect | None = None,
        idempotency_mode: str | None = None,
        usage_delta: Usage | None = None,
        new_usage: Usage | None = None,
    ) -> tuple[dict[str, Any], bool]:
        event: RunEvent | None = None
        with self._transaction() as conn:
            existing = conn.execute("SELECT * FROM tool_actions WHERE action_id = ?", (action_id,)).fetchone()
            if existing:
                return dict(existing), False
            run = conn.execute("SELECT * FROM employee_runs WHERE run_id = ?", (run_id,)).fetchone()
            if not run:
                raise KeyError(f"Unknown run: {run_id}")
            now = utc_now()
            conn.execute(
                """
                INSERT INTO tool_actions(
                    action_id, run_id, model_call_index, tool_call_id, tool_name, effect, idempotency_mode,
                    arguments_json, arguments_hash, resource_key, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'INTENT_RECORDED', ?)
                """,
                (
                    action_id,
                    run_id,
                    model_call_index,
                    call.call_id,
                    call.name,
                    None if effect is None else effect.value,
                    idempotency_mode,
                    _safe_json(call.arguments),
                    arguments_hash,
                    redact_prompt_text(resource_key),
                    now.isoformat(),
                ),
            )
            # Independent read-only actions can arrive concurrently. Reserve
            # each tool call from the transaction's current usage snapshot so
            # one intent cannot overwrite another intent's accounting.
            if usage_delta is not None:
                current_usage = usage_from_dict(_loads(run["usage_json"], {}))
                new_usage = current_usage.plus(usage_delta)
            if new_usage is not None:
                conn.execute(
                    "UPDATE employee_runs SET usage_json = ?, updated_at = ? WHERE run_id = ?",
                    (_json(new_usage), now.isoformat(), run_id),
                )
                run = conn.execute("SELECT * FROM employee_runs WHERE run_id = ?", (run_id,)).fetchone()
            event = self._insert_event(
                conn,
                run,
                EventType.TOOL_INTENT_RECORDED,
                {
                    "action_id": action_id,
                    "tool_call_id": call.call_id,
                    "tool_name": call.name,
                    "arguments_hash": arguments_hash,
                    "resource_key": resource_key,
                },
                usage_delta,
            )
            action = conn.execute("SELECT * FROM tool_actions WHERE action_id = ?", (action_id,)).fetchone()
        assert event is not None
        self._notify(event)
        return dict(action), True

    def mark_tool_started(self, action_id: str) -> RunEvent:
        with self._transaction() as conn:
            action = conn.execute("SELECT * FROM tool_actions WHERE action_id = ?", (action_id,)).fetchone()
            if not action:
                raise KeyError(f"Unknown action: {action_id}")
            if action["status"] in {"SUCCEEDED", "FAILED", "INDETERMINATE"}:
                raise RuntimeError(f"Action already terminal: {action_id}")
            conn.execute(
                "UPDATE tool_actions SET status = 'STARTED', started_at = ? WHERE action_id = ?",
                (utc_now().isoformat(), action_id),
            )
            run = conn.execute("SELECT * FROM employee_runs WHERE run_id = ?", (action["run_id"],)).fetchone()
            event = self._insert_event(
                conn,
                run,
                EventType.TOOL_STARTED,
                {"action_id": action_id, "tool_name": action["tool_name"]},
            )
        self._notify(event)
        return event

    def mark_tool_terminal(self, action_id: str, result: ToolResult) -> RunEvent:
        event_type = EventType.TOOL_SUCCEEDED if result.ok else EventType.TOOL_FAILED
        status = "SUCCEEDED" if result.ok else "FAILED"
        with self._transaction() as conn:
            action = conn.execute("SELECT * FROM tool_actions WHERE action_id = ?", (action_id,)).fetchone()
            if not action:
                raise KeyError(f"Unknown action: {action_id}")
            if action["status"] in {"SUCCEEDED", "FAILED", "INDETERMINATE"}:
                raise RuntimeError(f"Action already terminal: {action_id}")
            now = utc_now()
            conn.execute(
                """
                UPDATE tool_actions
                SET status = ?, result_json = ?, error_json = ?, finished_at = ?
                WHERE action_id = ?
                """,
                (
                    status,
                    _safe_json(result),
                    _json({"code": result.error_code}) if result.error_code else None,
                    now.isoformat(),
                    action_id,
                ),
            )
            conn.execute(
                "DELETE FROM effect_resource_leases WHERE owner_action_id = ?",
                (action_id,),
            )
            run = conn.execute("SELECT * FROM employee_runs WHERE run_id = ?", (action["run_id"],)).fetchone()
            event = self._insert_event(
                conn,
                run,
                event_type,
                {
                    "action_id": action_id,
                    "tool_name": action["tool_name"],
                    "ok": result.ok,
                    "error_code": result.error_code,
                    "output_bytes": len(result.content.encode("utf-8")),
                },
            )
        self._notify(event)
        return event

    def get_tool_result(self, action_id: str) -> ToolResult | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT result_json FROM tool_actions WHERE action_id = ?",
                (action_id,),
            ).fetchone()
        if not row or not row["result_json"]:
            return None
        value = _loads(row["result_json"], {})
        return ToolResult(
            call_id=value["call_id"],
            name=value["name"],
            ok=bool(value["ok"]),
            content=value["content"],
            action_id=value["action_id"],
            error_code=value.get("error_code"),
            replayed=True,
        )

    def get_tool_output_bytes(self, run_id: str) -> int:
        """Return successful tool content retained for the model in this run."""

        with self._lock:
            rows = self._conn.execute(
                "SELECT result_json FROM tool_actions WHERE run_id = ? AND status = 'SUCCEEDED'",
                (run_id,),
            ).fetchall()
        return sum(
            len(str(_loads(row["result_json"], {}).get("content", "")).encode("utf-8"))
            for row in rows
        )

    def list_job_tool_receipts(self, job_id: str) -> list[dict[str, Any]]:
        """Return content-free tool terminal evidence for recovery decisions."""

        with self._lock:
            rows = self._conn.execute(
                """
                SELECT action.action_id, action.run_id, action.tool_name,
                       action.effect, action.idempotency_mode, action.status,
                       action.created_at, action.started_at, action.finished_at,
                       run.task_id, run.status AS run_status
                FROM tool_actions AS action
                JOIN employee_runs AS run ON run.run_id = action.run_id
                WHERE run.job_id = ?
                ORDER BY run.task_id, action.created_at, action.action_id
                """,
                (job_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _effect_resource_digest(resource_key: str) -> str:
        if not resource_key.strip() or len(resource_key.encode("utf-8")) > 1_024:
            raise ValueError("effect resource key is invalid")
        return hashlib.sha256(
            f"noruct.effect-resource.v1|{resource_key}".encode("utf-8")
        ).hexdigest()

    def acquire_effect_resource_lease(
        self,
        *,
        action_id: str,
        run_id: str,
        effect: ToolEffect,
        resource_key: str,
    ) -> bool:
        """Claim one exact effectful resource without serializing read work.

        Only WRITE, EXECUTE, and EXTERNAL_COMMUNICATION actions use this
        lease.  It is held from the durable tool-start boundary until that
        action reaches a durable terminal receipt.  If a process disappears,
        run terminality and wall-clock time are not release evidence: a
        started action stays sealed until an explicit recovery resolution
        proves the resource may be released.
        """

        if effect not in {
            ToolEffect.WRITE,
            ToolEffect.EXECUTE,
            ToolEffect.EXTERNAL_COMMUNICATION,
        }:
            raise ValueError("effect resource leases require an effectful tool action")
        digest = self._effect_resource_digest(resource_key)
        with self._transaction() as conn:
            action = conn.execute(
                "SELECT run_id, status FROM tool_actions WHERE action_id = ?",
                (action_id,),
            ).fetchone()
            if action is None:
                raise KeyError(f"Unknown tool action: {action_id}")
            if str(action["run_id"]) != run_id:
                raise ValueError("effect resource lease does not belong to the action run")
            if str(action["status"]) in {"SUCCEEDED", "FAILED", "INDETERMINATE"}:
                return False
            sealed = conn.execute(
                """
                SELECT 1
                  FROM effect_recovery_cases AS recovery
             LEFT JOIN effect_recovery_resolutions AS resolution
                    ON resolution.action_id = recovery.action_id
                 WHERE recovery.resource_digest = ?
                   AND COALESCE(resolution.resource_released, 0) = 0
                 LIMIT 1
                """,
                (digest,),
            ).fetchone()
            if sealed is not None:
                return False
            existing = conn.execute(
                "SELECT * FROM effect_resource_leases WHERE resource_digest = ?",
                (digest,),
            ).fetchone()
            if existing is not None:
                if str(existing["owner_action_id"]) == action_id:
                    return True
                owner = conn.execute(
                    """
                    SELECT action.status AS action_status, run.status AS run_status
                    FROM tool_actions AS action
                    JOIN employee_runs AS run ON run.run_id = action.run_id
                    WHERE action.action_id = ?
                    """,
                    (str(existing["owner_action_id"]),),
                ).fetchone()
                # A terminal run is not proof that its already-started effect
                # did not happen.  Only a never-started intent may be reclaimed
                # after its run ends; STARTED/INDETERMINATE and unknown legacy
                # states remain sealed until a recovery resolution says the
                # resource can be released.
                owner_reclaimable = owner is not None and (
                    str(owner["action_status"]) in {"SUCCEEDED", "FAILED"}
                    or (
                        str(owner["action_status"]) == "INTENT_RECORDED"
                        and RunStatus(str(owner["run_status"])).terminal
                    )
                )
                if not owner_reclaimable:
                    return False
                conn.execute(
                    "DELETE FROM effect_resource_leases WHERE resource_digest = ?",
                    (digest,),
                )
            conn.execute(
                """
                INSERT INTO effect_resource_leases(
                    resource_digest, effect, owner_action_id, owner_run_id, acquired_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (digest, effect.value, action_id, run_id, utc_now().isoformat()),
            )
            return True
