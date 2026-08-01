"""Interrupted-run recovery mutation for the canonical runtime Store.

Recovery retains the owning Store's transaction and terminalization authority.
It is isolated because process-interruption repair is operational lifecycle
logic, not ordinary run lookup or ACTIVE JOB audit projection.
"""

from __future__ import annotations

import json
import hashlib
import re
from datetime import datetime
from dataclasses import dataclass
from typing import Any, Mapping

from dynamic_firm.company.model_invocation_receipt import (
    InvocationTerminalStatus,
    ModelInvocationReceipt,
    ReceiptAvailability,
)

from .models import (
    EmployeeRunResult,
    EventType,
    Failure,
    FailureCategory,
    RunStatus,
    result_from_dict,
    to_primitive,
    usage_from_dict,
    utc_now,
)
from .interruption import EffectInterruptionReason
from .redaction import redact_runtime_value


_RECOVERY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}\Z")


@dataclass(frozen=True, slots=True)
class FrozenRunRecoveryInspection:
    """Content-free preflight for a deliberately operator-confirmed recovery."""

    run_id: str
    status: RunStatus
    binding_digest: str
    dispatch_epoch_present: bool
    outstanding_invocation_ids: tuple[str, ...]
    terminal_receipt_count: int
    tool_action_count: int

    @property
    def requires_effect_recovery(self) -> bool:
        return self.tool_action_count > 0


def _json(value: Any) -> str:
    return json.dumps(
        to_primitive(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _loads(value: str | None, default: Any) -> Any:
    return json.loads(value) if value else default


def _digest_json(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


class RunStoreRecoveryMixin:
    """Fail-closed recovery operations composed into :class:`RunStore`."""

    def inspect_frozen_run_recovery(self, run_id: str) -> FrozenRunRecoveryInspection:
        """Inspect one frozen run without claiming, cancelling, or replaying it."""
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("frozen run recovery requires a run id")
        with self._lock:
            run = self._conn.execute(
                "SELECT run_id, status FROM employee_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise KeyError(f"Unknown run: {run_id}")
            binding = self._conn.execute(
                "SELECT binding_digest FROM employee_run_frozen_routes WHERE run_id = ?", (run_id,)
            ).fetchone()
            if binding is None:
                raise ValueError("frozen run recovery requires a frozen route binding")
            lease = self._conn.execute(
                "SELECT 1 FROM employee_run_model_invocation_dispatch_leases WHERE run_id = ?", (run_id,)
            ).fetchone()
            reservations = self._conn.execute(
                "SELECT invocation_id FROM employee_run_model_invocation_dispatch_reservations WHERE run_id = ? ORDER BY invocation_id",
                (run_id,),
            ).fetchall()
            receipt_count = self._conn.execute(
                "SELECT COUNT(*) AS count FROM employee_run_model_invocation_receipts WHERE run_id = ?", (run_id,)
            ).fetchone()
            tool_count = self._conn.execute(
                "SELECT COUNT(*) AS count FROM tool_actions WHERE run_id = ?", (run_id,)
            ).fetchone()
        return FrozenRunRecoveryInspection(
            run_id=run_id,
            status=RunStatus(str(run["status"])),
            binding_digest=str(binding["binding_digest"]),
            dispatch_epoch_present=lease is not None,
            outstanding_invocation_ids=tuple(str(row["invocation_id"]) for row in reservations),
            terminal_receipt_count=int(receipt_count["count"] if receipt_count else 0),
            tool_action_count=int(tool_count["count"] if tool_count else 0),
        )

    def claim_and_terminalize_frozen_run(
        self,
        run_id: str,
        *,
        expected_binding_digest: str,
        recovery_id: str,
        operator_confirmed_abandoned: bool,
    ) -> EmployeeRunResult:
        """Seal a frozen physical lane after an explicit no-replay operator claim.

        This is intentionally not used by startup recovery.  The caller must
        have inspected the exact run and explicitly confirmed that its prior
        dispatcher is abandoned.  Tool-bearing runs are refused so their
        effect-specific recovery stays on the existing reconcile path.
        """
        if not operator_confirmed_abandoned:
            raise ValueError("frozen run terminalization requires explicit operator confirmation")
        if not isinstance(expected_binding_digest, str) or len(expected_binding_digest) != 64:
            raise ValueError("frozen run terminalization requires an exact binding digest")
        if not isinstance(recovery_id, str) or not _RECOVERY_ID.fullmatch(recovery_id):
            raise ValueError("frozen run recovery id is invalid")
        with self._transaction() as conn:
            run = conn.execute("SELECT * FROM employee_runs WHERE run_id = ?", (run_id,)).fetchone()
            if run is None:
                raise KeyError(f"Unknown run: {run_id}")
            current = RunStatus(str(run["status"]))
            if current.terminal:
                result = self.get_result(run_id)
                if result is None:
                    raise ValueError("terminal frozen run has no result")
                return result
            binding = self._get_frozen_route_binding_in_transaction(conn, run_id)
            if binding is None or binding.digest != expected_binding_digest:
                raise ValueError("frozen run terminalization binding mismatch")
            tools = conn.execute("SELECT 1 FROM tool_actions WHERE run_id = ? LIMIT 1", (run_id,)).fetchone()
            if tools is not None:
                raise ValueError("frozen run with tool evidence requires effect recovery")
            claim = conn.execute(
                "SELECT recovery_id, binding_digest, status FROM employee_run_frozen_route_recovery_claims WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if claim is not None and (
                str(claim["recovery_id"]) != recovery_id
                or str(claim["binding_digest"]) != expected_binding_digest
            ):
                raise ValueError("frozen run recovery is already claimed")
            now = utc_now().isoformat()
            if claim is None:
                conn.execute(
                    "INSERT INTO employee_run_frozen_route_recovery_claims(run_id, recovery_id, binding_digest, status, created_at) VALUES(?,?,?,?,?)",
                    (run_id, recovery_id, expected_binding_digest, "CLAIMED", now),
                )
            reservations = conn.execute(
                "SELECT invocation_id, context_projection_digest, attempt_id FROM employee_run_model_invocation_dispatch_reservations WHERE run_id = ? ORDER BY invocation_id",
                (run_id,),
            ).fetchall()
            for reservation in reservations:
                receipt = ModelInvocationReceipt(
                    invocation_id=str(reservation["invocation_id"]),
                    route_binding_digest=binding.digest,
                    context_projection_digest=str(reservation["context_projection_digest"]),
                    attempt_id=str(reservation["attempt_id"]),
                    fanout_parent_id=None,
                    terminal_status=InvocationTerminalStatus.INDETERMINATE,
                    output_digest=None,
                    usage_availability=ReceiptAvailability.UNAVAILABLE,
                    usage_units=None,
                    cost_availability=ReceiptAvailability.UNAVAILABLE,
                    cost_usd=None,
                    latency_ms=0.0,
                    safe_error_code="DISPATCHER_ABANDONED",
                )
                self._store_receipt_in_transaction(conn, run_id, receipt)
            conn.execute(
                "DELETE FROM employee_run_model_invocation_dispatch_reservations WHERE run_id = ?", (run_id,)
            )
            conn.execute(
                "DELETE FROM employee_run_model_invocation_dispatch_leases WHERE run_id = ?", (run_id,)
            )
            conn.execute(
                "UPDATE employee_run_frozen_route_recovery_claims SET status = 'TERMINALIZED', terminalized_at = ? WHERE run_id = ?",
                (now, run_id),
            )
        failure = Failure(
            code="FROZEN_DISPATCHER_ABANDONED",
            category=FailureCategory.INTERNAL,
            message_safe="An operator sealed an abandoned frozen dispatcher without replay.",
            retryable=False,
        )
        return self.terminalize(
            EmployeeRunResult(
                run_id=str(run["run_id"]), request_id=str(run["request_id"]), job_id=str(run["job_id"]),
                task_id=str(run["task_id"]), employee_id=str(run["employee_id"]), status=RunStatus.FAILED,
                summary="Frozen dispatcher sealed without replay.", output_artifact_refs=(), acceptance_evidence=(),
                unresolved_issues=("A new Kernel attempt is required.",), observations=(), suggested_followups=(),
                signals=(), partial_result=True,
                usage=usage_from_dict(json.loads(run["usage_json"]) if run["usage_json"] else {}),
                last_event_seq=0,
                started_at=datetime.fromisoformat(run["started_at"]) if run["started_at"] else None,
                finished_at=utc_now(), failure=failure,
            ),
            EventType.RUN_FAILED,
            {"failure_code": failure.code, "recovery_id": recovery_id},
        )

    def recover_interrupted_runs(
        self,
        *,
        preserve_waiting_approvals: bool = False,
    ) -> list[EmployeeRunResult]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM employee_runs WHERE status IN (?, ?, ?, ?)",
                (
                    RunStatus.CREATED.value,
                    RunStatus.RUNNING.value,
                    RunStatus.WAITING_APPROVAL.value,
                    RunStatus.CANCELLING.value,
                ),
            ).fetchall()
        recovered: list[EmployeeRunResult] = []
        for row in rows:
            # A frozen binding is the durable pre-dispatch ownership boundary.
            # A just-created runtime task has not yet had a chance to commit
            # its lease, and ordinary startup recovery cannot distinguish it
            # from a stopped owner.  Both that CREATED interval and a leased
            # live dispatcher are therefore deferred to explicit frozen-run
            # recovery authority; generic recovery must not terminalize them.
            with self._lock:
                frozen_run = self._conn.execute(
                    """
                    SELECT 1
                    FROM employee_run_frozen_routes
                    WHERE run_id = ?
                    LIMIT 1
                    """,
                    (row["run_id"],),
                ).fetchone()
            if frozen_run is not None:
                continue
            # A frozen dispatcher lease is proof only that one live local
            # service owns this physical invocation lane.  Its presence is
            # deliberately enough to suppress ordinary startup recovery: a
            # newly opened Store cannot infer that the owning process or its
            # provider call has ended, and it has no takeover authority.
            with self._lock:
                active_dispatcher = self._conn.execute(
                    """
                    SELECT 1
                    FROM employee_run_model_invocation_dispatch_leases
                    WHERE run_id = ?
                    LIMIT 1
                    """,
                    (row["run_id"],),
                ).fetchone()
            if active_dispatcher is not None:
                continue
            if preserve_waiting_approvals and row["status"] == RunStatus.WAITING_APPROVAL.value:
                with self._lock:
                    resumable = self._conn.execute(
                        """
                        SELECT 1 FROM approval_requests AS approval
                        JOIN tool_actions AS action ON action.action_id = approval.action_id
                        WHERE approval.run_id = ?
                            AND approval.resume_claimed_at IS NULL
                            AND action.status = 'INTENT_RECORDED'
                        LIMIT 1
                        """,
                        (row["run_id"],),
                    ).fetchone()
                    interrupted_job = self._conn.execute(
                        """
                        SELECT 1 FROM job_snapshots AS snapshot
                        LEFT JOIN job_terminal_events AS terminal
                            ON terminal.job_id = snapshot.job_id
                        WHERE snapshot.job_id = ?
                            AND terminal.job_id IS NULL
                        LIMIT 1
                        """,
                        (row["job_id"],),
                    ).fetchone()
                # A direct conversation may retain an unclaimed approval. A
                # Job-scoped run cannot: its stopped Firm graph must retain
                # budget, audit, and effect authority on the next attempt.
                if resumable is not None and interrupted_job is None:
                    continue
            # TOOL_STARTED is the last trustworthy boundary after a process
            # loss.  An effectful handler may have committed remotely before
            # the process disappeared, so seal it before terminalizing the
            # owning run.  Read-only actions need no resource recovery case.
            self.mark_run_started_effects_indeterminate(
                str(row["run_id"]),
                cause=EffectInterruptionReason.PROCESS_OR_MACHINE_LOSS,
            )
            failure = Failure(
                code="PROCESS_INTERRUPTED",
                category=FailureCategory.INTERNAL,
                message_safe="The runtime process ended before the run reached a terminal state.",
                retryable=True,
            )
            result = EmployeeRunResult(
                run_id=row["run_id"],
                request_id=row["request_id"],
                job_id=row["job_id"],
                task_id=row["task_id"],
                employee_id=row["employee_id"],
                status=RunStatus.FAILED,
                summary="Run interrupted before completion.",
                output_artifact_refs=(),
                acceptance_evidence=(),
                unresolved_issues=("A new Kernel-controlled attempt is required.",),
                observations=(),
                suggested_followups=(),
                signals=(),
                partial_result=True,
                usage=usage_from_dict(json.loads(row["usage_json"]) if row["usage_json"] else {}),
                last_event_seq=0,
                started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] else None,
                finished_at=utc_now(),
                failure=failure,
            )
            recovered.append(
                self.terminalize(result, EventType.RUN_FAILED, {"failure_code": failure.code})
            )
        return recovered

    def save_local_resume_envelope(
        self,
        *,
        job_id: str,
        work_order_digest: str,
        graph_digest: str,
        references: Mapping[str, str],
    ) -> dict[str, Any]:
        if not job_id or len(work_order_digest) != 64 or len(graph_digest) != 64:
            raise ValueError("Resume envelope identity is invalid")
        if any(not key or not value or len(value) > 256 for key, value in references.items()):
            raise ValueError("Resume envelope references are invalid")
        payload = {
            "job_id": job_id,
            "work_order_digest": work_order_digest,
            "graph_digest": graph_digest,
            "references": dict(sorted(references.items())),
        }
        digest = _digest_json(payload)
        now = utc_now().isoformat()
        with self._transaction() as conn:
            existing = conn.execute(
                "SELECT work_order_digest, graph_digest, references_json, integrity_digest, status "
                "FROM local_resume_envelopes WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if existing is not None:
                existing_payload = {
                    "job_id": job_id,
                    "work_order_digest": str(existing["work_order_digest"]),
                    "graph_digest": str(existing["graph_digest"]),
                    "references": _loads(existing["references_json"], {}),
                }
                if _digest_json(existing_payload) != str(existing["integrity_digest"]):
                    raise RuntimeError("Resume envelope integrity mismatch")
                if existing_payload != payload:
                    raise ValueError("Resume envelope cannot be replaced for an existing job")
                return {
                    **payload,
                    "integrity_digest": digest,
                    "status": str(existing["status"]),
                }
            conn.execute(
                """
                INSERT INTO local_resume_envelopes(
                    job_id, work_order_digest, graph_digest, references_json,
                    integrity_digest, status, created_at, updated_at
                ) VALUES(?,?,?,?,?,'PENDING',?,?)
                """,
                (
                    job_id,
                    work_order_digest,
                    graph_digest,
                    _json(payload["references"]),
                    digest,
                    now,
                    now,
                ),
            )
        return {**payload, "integrity_digest": digest, "status": "PENDING"}

    def finalize_local_resume_envelope(self, job_id: str) -> None:
        with self._transaction() as conn:
            conn.execute("UPDATE local_resume_envelopes SET status = 'TERMINAL', updated_at = ? WHERE job_id = ? AND status = 'PENDING'", (utc_now().isoformat(), job_id))

    def local_resume_envelope(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM local_resume_envelopes WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        value = {
            "job_id": str(row["job_id"]),
            "work_order_digest": str(row["work_order_digest"]),
            "graph_digest": str(row["graph_digest"]),
            "references": _loads(row["references_json"], {}),
            "integrity_digest": str(row["integrity_digest"]),
            "status": str(row["status"]),
        }
        signed_payload = {
            key: value[key]
            for key in ("job_id", "work_order_digest", "graph_digest", "references")
        }
        if _digest_json(signed_payload) != value["integrity_digest"]:
            raise RuntimeError("Resume envelope integrity mismatch")
        return value

    def recovery_candidate(self, job_id: str) -> dict[str, Any]:
        """Return a verified non-terminal local continuation candidate only.

        This intentionally does not resume execution or recreate any request.
        The caller must still validate local source hashes and durable approval
        receipts before constructing a new Kernel-owned continuation.
        """
        envelope = self.local_resume_envelope(job_id)
        if envelope is None:
            raise KeyError(f"Resume envelope was not found: {job_id}")
        if envelope["status"] != "PENDING":
            raise ValueError("Only a non-terminal resume envelope is recoverable")
        return {
            **envelope,
            "dispatch_allowed": False,
            "required_checks": (
                "source_hashes",
                "approval_receipts",
                "budget_lease",
                "active_job_audit",
            ),
        }

    def authorize_same_job_continuation(
        self,
        *,
        job_id: str,
        request_snapshot_hash: str,
        graph_digest: str,
    ) -> dict[str, Any]:
        """Persist one explicit, content-free fresh-start continuation receipt.

        This is intentionally narrower than a resume queue.  It neither
        retains the Work Order body nor starts a worker.  A later Kernel entry
        may claim this receipt only when it presents the exact frozen request
        and graph that the operator revalidated.
        """

        if len(request_snapshot_hash) != 64 or len(graph_digest) != 64:
            raise ValueError("Same-Job continuation identity is invalid")
        now = utc_now().isoformat()
        with self._transaction() as conn:
            self._job_snapshot_row(conn, job_id)
            terminal = conn.execute(
                "SELECT 1 FROM job_terminal_events WHERE job_id = ?", (job_id,)
            ).fetchone()
            if terminal is not None:
                raise ValueError("Terminal ACTIVE JOB cannot be continued")
            existing = conn.execute(
                "SELECT * FROM same_job_continuation_admissions WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["request_snapshot_hash"]) != request_snapshot_hash
                    or str(existing["graph_digest"]) != graph_digest
                ):
                    raise ValueError("Same-Job continuation receipt conflicts")
                if str(existing["status"]) != "PENDING":
                    raise ValueError("Same-Job continuation receipt was already claimed")
                return dict(existing)
            conn.execute(
                """
                INSERT INTO same_job_continuation_admissions(
                    job_id, request_snapshot_hash, graph_digest, status, admitted_at, claimed_at
                ) VALUES(?,?,?,'PENDING',?,NULL)
                """,
                (job_id, request_snapshot_hash, graph_digest, now),
            )
            row = conn.execute(
                "SELECT * FROM same_job_continuation_admissions WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        assert row is not None
        return dict(row)

    def claim_same_job_continuation(
        self,
        *,
        job_id: str,
        request_snapshot_hash: str,
        graph_digest: str,
    ) -> dict[str, Any]:
        """Consume the one explicit continuation receipt before Kernel dispatch."""

        now = utc_now().isoformat()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM same_job_continuation_admissions WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Same-Job continuation requires explicit admission")
            if (
                str(row["request_snapshot_hash"]) != request_snapshot_hash
                or str(row["graph_digest"]) != graph_digest
            ):
                raise ValueError("Same-Job continuation receipt does not match Kernel entry")
            if str(row["status"]) != "PENDING":
                raise ValueError("Same-Job continuation receipt was already claimed")
            conn.execute(
                """
                UPDATE same_job_continuation_admissions
                SET status = 'CLAIMED', claimed_at = ?
                WHERE job_id = ? AND status = 'PENDING'
                """,
                (now, job_id),
            )
            claimed = conn.execute(
                "SELECT * FROM same_job_continuation_admissions WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        assert claimed is not None
        return dict(claimed)

    def authorize_partial_job_continuation(
        self,
        *,
        job_id: str,
        request_snapshot_hash: str,
        graph_digest: str,
        completed_run_ids: tuple[str, ...],
        completed_results_digest: str,
    ) -> dict[str, Any]:
        """Persist one read-only, receipt-bound partial continuation admission.

        The receipt does not copy result content and cannot claim in-flight
        work.  It is intentionally usable only by the explicit Kernel entry
        added with the continuation runtime; its CAS status prevents two
        machines sharing a coordination store from consuming the same progress.
        """

        if (
            len(request_snapshot_hash) != 64
            or len(graph_digest) != 64
            or len(completed_results_digest) != 64
            or not completed_run_ids
            or len(set(completed_run_ids)) != len(completed_run_ids)
            or any(not run_id or len(run_id) > 256 for run_id in completed_run_ids)
        ):
            raise ValueError("Partial continuation identity is invalid")
        now = utc_now().isoformat()
        normalized_run_ids = tuple(sorted(completed_run_ids))
        with self._transaction() as conn:
            self._job_snapshot_row(conn, job_id)
            if conn.execute(
                "SELECT 1 FROM job_terminal_events WHERE job_id = ?", (job_id,)
            ).fetchone() is not None:
                raise ValueError("Terminal ACTIVE JOB cannot be continued")
            existing = conn.execute(
                "SELECT * FROM partial_job_continuation_admissions WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            payload = (
                request_snapshot_hash,
                graph_digest,
                _json(normalized_run_ids),
                completed_results_digest,
            )
            if existing is not None:
                existing_payload = (
                    str(existing["request_snapshot_hash"]),
                    str(existing["graph_digest"]),
                    str(existing["completed_run_ids_json"]),
                    str(existing["completed_results_digest"]),
                )
                if existing_payload != payload:
                    raise ValueError("Partial continuation receipt conflicts")
                if str(existing["status"]) != "PENDING":
                    raise ValueError("Partial continuation receipt was already claimed")
                return dict(existing)
            conn.execute(
                """
                INSERT INTO partial_job_continuation_admissions(
                    job_id, request_snapshot_hash, graph_digest,
                    completed_run_ids_json, completed_results_digest, status,
                    admitted_at, claimed_at
                ) VALUES(?,?,?,?,?,'PENDING',?,NULL)
                """,
                (job_id, *payload, now),
            )
            admitted = conn.execute(
                "SELECT * FROM partial_job_continuation_admissions WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        assert admitted is not None
        return dict(admitted)

    def claim_partial_job_continuation(
        self,
        *,
        job_id: str,
        request_snapshot_hash: str,
        graph_digest: str,
    ) -> list[dict[str, Any]]:
        """Atomically consume a partial receipt and reopen its local results."""

        now = utc_now().isoformat()
        with self._transaction() as conn:
            handoff = conn.execute(
                """
                SELECT target_device_id FROM partial_job_continuation_handoffs WHERE job_id = ?
                UNION ALL
                SELECT target_device_id FROM partial_job_continuation_handoff_preparations WHERE job_id = ?
                LIMIT 1
                """,
                (job_id, job_id),
            ).fetchone()
            if handoff is not None:
                raise ValueError("Partial continuation was handed off to another device")
            row = conn.execute(
                "SELECT * FROM partial_job_continuation_admissions WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Partial continuation requires explicit admission")
            if (
                str(row["request_snapshot_hash"]) != request_snapshot_hash
                or str(row["graph_digest"]) != graph_digest
            ):
                raise ValueError("Partial continuation receipt does not match Kernel entry")
            if str(row["status"]) != "PENDING":
                raise ValueError("Partial continuation receipt was already claimed")
            expected_attempt_ids = tuple(_loads(str(row["completed_run_ids_json"]), ()))
            receipts = conn.execute(
                """
                SELECT * FROM job_dependency_result_receipts
                WHERE job_id = ? ORDER BY task_id, attempt_id
                """,
                (job_id,),
            ).fetchall()
            actual_attempt_ids = tuple(sorted(str(item["attempt_id"]) for item in receipts))
            if actual_attempt_ids != expected_attempt_ids:
                raise RuntimeError("Partial continuation dependency receipts changed after admission")
            digest_rows: list[tuple[str, str, str]] = []
            for receipt in receipts:
                payload = _loads(str(receipt["result_json"]), {})
                if not isinstance(payload, Mapping) or _digest_json(payload) != str(receipt["result_digest"]):
                    raise RuntimeError("Partial continuation dependency receipt integrity mismatch")
                digest_rows.append(
                    (
                        str(receipt["task_id"]),
                        str(receipt["attempt_id"]),
                        str(receipt["result_digest"]),
                    )
                )
            if _digest_json(tuple(sorted(digest_rows))) != str(row["completed_results_digest"]):
                raise RuntimeError("Partial continuation result digest mismatch")
            updated = conn.execute(
                """
                UPDATE partial_job_continuation_admissions
                SET status = 'CLAIMED', claimed_at = ?
                WHERE job_id = ? AND status = 'PENDING'
                """,
                (now, job_id),
            )
            if updated.rowcount != 1:
                raise RuntimeError("Partial continuation receipt claim lost its compare-and-swap")
        return self.list_job_dependency_result_receipts(job_id)

    def prepare_partial_job_continuation_handoff(
        self,
        *,
        job_id: str,
        target_device_id: str,
        request_snapshot_hash: str,
        graph_digest: str,
        completed_results_digest: str,
    ) -> dict[str, Any]:
        """Durably block a source claim before attempting remote handoff."""

        values = (request_snapshot_hash, graph_digest, completed_results_digest)
        if not target_device_id or len(target_device_id) > 256 or any(len(value) != 64 for value in values):
            raise ValueError("Partial continuation handoff identity is invalid")
        now = utc_now().isoformat()
        with self._transaction() as conn:
            admission = conn.execute(
                "SELECT * FROM partial_job_continuation_admissions WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if admission is None or str(admission["status"]) != "PENDING":
                raise ValueError("Only an unclaimed partial continuation can be handed off")
            expected = (
                str(admission["request_snapshot_hash"]),
                str(admission["graph_digest"]),
                str(admission["completed_results_digest"]),
            )
            if expected != values:
                raise ValueError("Partial continuation handoff does not match admission")
            existing = conn.execute(
                "SELECT * FROM partial_job_continuation_handoff_preparations WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["target_device_id"]),
                    str(existing["request_snapshot_hash"]),
                    str(existing["graph_digest"]),
                    str(existing["completed_results_digest"]),
                ) != (target_device_id, *values):
                    raise ValueError("Partial continuation handoff preparation conflicts")
                return dict(existing)
            conn.execute(
                """
                INSERT INTO partial_job_continuation_handoff_preparations(
                    job_id, target_device_id, request_snapshot_hash, graph_digest,
                    completed_results_digest, prepared_at
                ) VALUES(?,?,?,?,?,?)
                """,
                (job_id, target_device_id, *values, now),
            )
            prepared = conn.execute(
                "SELECT * FROM partial_job_continuation_handoff_preparations WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        assert prepared is not None
        return dict(prepared)

    def cancel_partial_job_continuation_handoff_preparation(
        self,
        *,
        job_id: str,
        target_device_id: str,
        request_snapshot_hash: str,
        graph_digest: str,
        completed_results_digest: str,
    ) -> bool:
        """Remove only a known-unsubmitted/failed local handoff preparation."""

        with self._transaction() as conn:
            deleted = conn.execute(
                """
                DELETE FROM partial_job_continuation_handoff_preparations
                WHERE job_id = ? AND target_device_id = ? AND request_snapshot_hash = ?
                  AND graph_digest = ? AND completed_results_digest = ?
                """,
                (job_id, target_device_id, request_snapshot_hash, graph_digest,
                 completed_results_digest),
            )
        return deleted.rowcount == 1

    def record_partial_job_continuation_handoff(
        self,
        *,
        job_id: str,
        target_device_id: str,
        request_snapshot_hash: str,
        graph_digest: str,
        completed_results_digest: str,
    ) -> dict[str, Any]:
        """Persist a source-local receipt after a remote pre-claim handoff.

        This is deliberately not a cross-device result transfer.  It only
        makes the local source fail closed when it later tries to consume the
        continuation while offline.
        """

        if (
            not target_device_id
            or len(target_device_id) > 256
            or any(len(value) != 64 for value in (
                request_snapshot_hash, graph_digest, completed_results_digest
            ))
        ):
            raise ValueError("Partial continuation handoff identity is invalid")
        now = utc_now().isoformat()
        with self._transaction() as conn:
            admission = conn.execute(
                "SELECT * FROM partial_job_continuation_admissions WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if admission is None or str(admission["status"]) != "PENDING":
                raise ValueError("Only an unclaimed partial continuation can be handed off")
            expected = (
                str(admission["request_snapshot_hash"]),
                str(admission["graph_digest"]),
                str(admission["completed_results_digest"]),
            )
            if expected != (request_snapshot_hash, graph_digest, completed_results_digest):
                raise ValueError("Partial continuation handoff does not match admission")
            existing = conn.execute(
                "SELECT * FROM partial_job_continuation_handoffs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["target_device_id"]),
                    str(existing["request_snapshot_hash"]),
                    str(existing["graph_digest"]),
                    str(existing["completed_results_digest"]),
                ) != (target_device_id, request_snapshot_hash, graph_digest, completed_results_digest):
                    raise ValueError("Partial continuation handoff conflicts")
                return dict(existing)
            conn.execute(
                """
                INSERT INTO partial_job_continuation_handoffs(
                    job_id, target_device_id, request_snapshot_hash, graph_digest,
                    completed_results_digest, handed_off_at
                ) VALUES(?,?,?,?,?,?)
                """,
                (job_id, target_device_id, request_snapshot_hash, graph_digest,
                 completed_results_digest, now),
            )
            receipt = conn.execute(
                "SELECT * FROM partial_job_continuation_handoffs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        assert receipt is not None
        return dict(receipt)

    def save_job_dependency_result_receipt(
        self,
        *,
        job_id: str,
        attempt_id: str,
        result: EmployeeRunResult,
    ) -> dict[str, Any]:
        """Persist the bounded local result needed by a resumed dependency.

        Only successful terminal results can become a dependency receipt.  The
        append-only Job attempt remains the provenance authority; this is a
        user-local materialization indexed by that immutable attempt identity.
        """

        if result.job_id != job_id or result.status is not RunStatus.SUCCEEDED:
            raise ValueError("Only successful same-Job results may become dependency receipts")
        if not result.task_id or not attempt_id:
            raise ValueError("Dependency receipt identity is incomplete")
        safe_result = redact_runtime_value(to_primitive(result))
        result_json = _json(safe_result)
        result_digest = _digest_json(safe_result)
        now = utc_now().isoformat()
        with self._transaction() as conn:
            attempt = conn.execute(
                "SELECT task_id, status FROM job_attempts WHERE attempt_id = ? AND job_id = ?",
                (attempt_id, job_id),
            ).fetchone()
            if attempt is None or str(attempt["task_id"]) != result.task_id:
                raise ValueError("Dependency receipt attempt does not match the Job result")
            if str(attempt["status"]) != RunStatus.SUCCEEDED.value:
                raise ValueError("Dependency receipt requires a successful task attempt")
            existing = conn.execute(
                "SELECT * FROM job_dependency_result_receipts WHERE job_id = ? AND task_id = ?",
                (job_id, result.task_id),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["attempt_id"]) == attempt_id
                    and str(existing["result_digest"]) == result_digest
                ):
                    return dict(existing)
                raise ValueError("Dependency receipt conflicts with an existing task result")
            conn.execute(
                """
                INSERT INTO job_dependency_result_receipts(
                    job_id, task_id, attempt_id, result_json, result_digest, created_at
                ) VALUES(?,?,?,?,?,?)
                """,
                (job_id, result.task_id, attempt_id, result_json, result_digest, now),
            )
            saved = conn.execute(
                "SELECT * FROM job_dependency_result_receipts WHERE job_id = ? AND task_id = ?",
                (job_id, result.task_id),
            ).fetchone()
        assert saved is not None
        return dict(saved)

    def list_job_dependency_result_receipts(self, job_id: str) -> list[dict[str, Any]]:
        """Return verified local continuation receipts in deterministic order."""

        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM job_dependency_result_receipts
                WHERE job_id = ? ORDER BY task_id, attempt_id
                """,
                (job_id,),
            ).fetchall()
        receipts: list[dict[str, Any]] = []
        for row in rows:
            payload = _loads(str(row["result_json"]), {})
            if not isinstance(payload, Mapping) or _digest_json(payload) != str(row["result_digest"]):
                raise RuntimeError("Dependency receipt integrity mismatch")
            receipts.append({**dict(row), "result": result_from_dict(payload)})
        return receipts
