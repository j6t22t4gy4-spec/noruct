from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from dynamic_firm._vendor.paperclip_runtime.approval_resolution import (
    classify_approval_transition,
)
from dynamic_firm._vendor.paperclip_runtime.run_summary import summarize_terminal_result

from .models import (
    ApprovalDecision,
    ApprovalRecord,
    ApprovalRequest,
    ApprovalResolutionReceipt,
    ApprovalResumeState,
    EmployeeRunRequest,
    EmployeeRunResult,
    EventType,
    Failure,
    FailureCategory,
    ModelMessage,
    RunEvent,
    RunHandle,
    RunStatus,
    ToolCall,
    ToolEffect,
    ToolResult,
    ToolRisk,
    Usage,
    result_from_dict,
    to_primitive,
    usage_from_dict,
    utc_now,
)
from .redaction import redact_prompt_text, redact_runtime_value
from .store_employee_session import (
    EMPLOYEE_SESSION_FORMAT_VERSION,
    EmployeeSessionConflict,
    EmployeeSessionSnapshot,
    EmployeeSessionUpdate,
    RunStoreEmployeeSessionMixin,
)
from .store_effect_recovery import RunStoreEffectRecoveryMixin
from .store_frozen_route import RunStoreFrozenRouteMixin
from .store_model_invocation_receipt import RunStoreModelInvocationReceiptMixin
from .store_remote_effect_coordination import RunStoreRemoteEffectCoordinationMixin
from .store_job_audit import RunStoreJobAuditMixin
from .store_job_outcome import RunStoreJobOutcomeMixin
from .store_job_lifecycle import RunStoreJobLifecycleMixin
from .store_graph_proposal_continuation import (
    RunStoreGraphProposalContinuationMixin,
)
from .store_company_budget import RunStoreCompanyBudgetMixin
from .store_company_budget_lifecycle import RunStoreCompanyBudgetLifecycleMixin
from .store_continuation_preflight import RunStoreContinuationPreflightMixin
from .store_read import RunStoreReadProjectionMixin
from .store_recovery import RunStoreRecoveryMixin
from .store_run_lifecycle import RunStoreRunLifecycleMixin
from .store_run_primitives import (
    employee_session_namespace,
)
from .store_schema import RunStoreSchemaMigrationMixin
from .store_ledger_primitives import job_chain_digest
from .store_tool_approval import ApprovalConflict, RunStoreToolApprovalMixin


SCHEMA_VERSION = 30


def _json(value: Any) -> str:
    return json.dumps(to_primitive(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _safe_json(value: Any) -> str:
    return _json(redact_runtime_value(to_primitive(value)))


def _digest_json(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


class RunStore(
    RunStoreCompanyBudgetLifecycleMixin,
    RunStoreContinuationPreflightMixin,
    RunStoreJobLifecycleMixin,
    RunStoreJobOutcomeMixin,
    RunStoreRunLifecycleMixin,
    RunStoreGraphProposalContinuationMixin,
    RunStoreJobAuditMixin,
    RunStoreRemoteEffectCoordinationMixin,
    RunStoreEffectRecoveryMixin,
    RunStoreFrozenRouteMixin,
    RunStoreModelInvocationReceiptMixin,
    RunStoreToolApprovalMixin,
    RunStoreCompanyBudgetMixin,
    RunStoreEmployeeSessionMixin,
    RunStoreReadProjectionMixin,
    RunStoreRecoveryMixin,
    RunStoreSchemaMigrationMixin,
):
    """SQLite canonical store for Native Employee runs and ordered events."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._subscribers: list[Callable[[RunEvent], None]] = []
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._initialize()

    def _initialize(self) -> None:
        with self._transaction() as conn:
            self._initialize_run_event_session_schema(conn)
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS local_resume_envelopes (
                    job_id TEXT PRIMARY KEY,
                    work_order_digest TEXT NOT NULL,
                    graph_digest TEXT NOT NULL,
                    references_json TEXT NOT NULL,
                    integrity_digest TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                -- This is deliberately not a serialized continuation.  It is
                -- a one-shot, content-free operator receipt which lets the
                -- ACTIVE JOB ledger distinguish an explicitly revalidated
                -- fresh-start continuation from an accidental duplicate
                -- Kernel invocation after a crash.
                CREATE TABLE IF NOT EXISTS same_job_continuation_admissions (
                    job_id TEXT PRIMARY KEY REFERENCES job_snapshots(job_id),
                    request_snapshot_hash TEXT NOT NULL,
                    graph_digest TEXT NOT NULL,
                    status TEXT NOT NULL,
                    admitted_at TEXT NOT NULL,
                    claimed_at TEXT,
                    CHECK(status IN ('PENDING','CLAIMED'))
                );

                CREATE TABLE IF NOT EXISTS tool_actions (
                    action_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES employee_runs(run_id) ON DELETE CASCADE,
                    model_call_index INTEGER NOT NULL,
                    tool_call_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    effect TEXT,
                    idempotency_mode TEXT,
                    arguments_json TEXT NOT NULL,
                    arguments_hash TEXT NOT NULL,
                    resource_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT,
                    error_json TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT
                );

                CREATE INDEX IF NOT EXISTS tool_actions_run_idx
                    ON tool_actions(run_id, model_call_index);

                CREATE TABLE IF NOT EXISTS effect_resource_leases (
                    resource_digest TEXT PRIMARY KEY,
                    effect TEXT NOT NULL,
                    owner_action_id TEXT NOT NULL REFERENCES tool_actions(action_id) ON DELETE CASCADE,
                    owner_run_id TEXT NOT NULL REFERENCES employee_runs(run_id) ON DELETE CASCADE,
                    acquired_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS effect_resource_leases_owner_idx
                    ON effect_resource_leases(owner_action_id);

                -- A handler was entered but no trustworthy terminal effect
                -- receipt exists.  This case is immutable and continues to
                -- seal its resource even if the owning run is terminal.
                CREATE TABLE IF NOT EXISTS effect_recovery_cases (
                    action_id TEXT PRIMARY KEY REFERENCES tool_actions(action_id) ON DELETE CASCADE,
                    run_id TEXT NOT NULL REFERENCES employee_runs(run_id) ON DELETE CASCADE,
                    job_id TEXT NOT NULL,
                    resource_digest TEXT NOT NULL,
                    effect TEXT NOT NULL,
                    cause TEXT NOT NULL,
                    disposition TEXT NOT NULL,
                    detected_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS effect_recovery_cases_resource_idx
                    ON effect_recovery_cases(resource_digest, detected_at);
                CREATE INDEX IF NOT EXISTS effect_recovery_cases_job_idx
                    ON effect_recovery_cases(job_id, detected_at);

                -- Reconciliation never rewrites the original action or case.
                -- A sealed-unknown resolution remains resource-blocking;
                -- observed success/no-effect or compensation may release it.
                CREATE TABLE IF NOT EXISTS effect_recovery_resolutions (
                    action_id TEXT PRIMARY KEY REFERENCES effect_recovery_cases(action_id),
                    outcome TEXT NOT NULL,
                    evidence_digest TEXT,
                    resolved_by TEXT NOT NULL,
                    reason_safe TEXT NOT NULL,
                    resource_released INTEGER NOT NULL,
                    resolved_at TEXT NOT NULL,
                    CHECK(resource_released IN (0, 1))
                );

                -- Persist the exact secret-free remote owner before the
                -- network claim. A crash on either side of the HTTP response
                -- can then release only that original lease; no current
                -- settings value is substituted by inference.
                CREATE TABLE IF NOT EXISTS effect_remote_resource_claims (
                    action_id TEXT PRIMARY KEY REFERENCES tool_actions(action_id) ON DELETE CASCADE,
                    run_id TEXT NOT NULL REFERENCES employee_runs(run_id) ON DELETE CASCADE,
                    job_id TEXT NOT NULL,
                    authority_digest TEXT NOT NULL,
                    origin TEXT NOT NULL,
                    company_scope_digest TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    resource_digest TEXT NOT NULL,
                    lease_id TEXT NOT NULL,
                    prepared_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS effect_remote_resource_claims_job_idx
                    ON effect_remote_resource_claims(job_id, prepared_at);
                CREATE INDEX IF NOT EXISTS effect_remote_resource_claims_resource_idx
                    ON effect_remote_resource_claims(authority_digest, resource_digest);

                CREATE TABLE IF NOT EXISTS effect_remote_resource_releases (
                    action_id TEXT PRIMARY KEY REFERENCES effect_remote_resource_claims(action_id),
                    remote_status TEXT NOT NULL CHECK(remote_status IN ('RELEASED', 'MISSING')),
                    release_reason TEXT NOT NULL,
                    released_at TEXT NOT NULL
                );

                -- A releasing operator conclusion is frozen before network
                -- release. This prevents a concurrent SEALED_UNKNOWN decision
                -- from losing its remote hold between preflight and receipt.
                CREATE TABLE IF NOT EXISTS effect_remote_resolution_preparations (
                    action_id TEXT PRIMARY KEY REFERENCES effect_recovery_cases(action_id),
                    outcome TEXT NOT NULL,
                    evidence_digest TEXT NOT NULL,
                    resolved_by TEXT NOT NULL,
                    reason_safe TEXT NOT NULL,
                    prepared_at TEXT NOT NULL
                );

                -- A remote-only stranded claim (handler never entered, or a
                -- successful terminal action lost only its release response)
                -- needs its own append-only operator conclusion. It must not
                -- masquerade as an indeterminate handler outcome.
                CREATE TABLE IF NOT EXISTS effect_remote_resource_resolutions (
                    action_id TEXT PRIMARY KEY REFERENCES effect_remote_resource_claims(action_id),
                    outcome TEXT NOT NULL,
                    evidence_digest TEXT NOT NULL,
                    resolved_by TEXT NOT NULL,
                    reason_safe TEXT NOT NULL,
                    resolved_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS approval_requests (
                    action_id TEXT PRIMARY KEY REFERENCES tool_actions(action_id) ON DELETE CASCADE,
                    run_id TEXT NOT NULL REFERENCES employee_runs(run_id) ON DELETE CASCADE,
                    request_hash TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    decision TEXT,
                    decided_by TEXT,
                    resolved_at TEXT,
                    resume_claimed_at TEXT,
                    resume_completed_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS approval_requests_run_idx
                    ON approval_requests(run_id, created_at);

                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES employee_runs(run_id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    byte_length INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    storage_ref TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS job_snapshots (
                    job_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL UNIQUE,
                    proposal_id TEXT NOT NULL,
                    graph_version INTEGER NOT NULL,
                    final_task_id TEXT NOT NULL,
                    company_revision INTEGER NOT NULL,
                    roster_revision INTEGER NOT NULL,
                    playbook_revision INTEGER NOT NULL,
                    frozen_snapshot_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    chain_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                -- A partial continuation is deliberately a one-shot receipt,
                -- not a serialized worker or a hidden scheduler queue.  The
                -- result bodies stay in the user-local employee-run store;
                -- this table binds only their immutable identities and digest
                -- to an operator-approved, read-only continuation boundary.
                CREATE TABLE IF NOT EXISTS partial_job_continuation_admissions (
                    job_id TEXT PRIMARY KEY REFERENCES job_snapshots(job_id),
                    request_snapshot_hash TEXT NOT NULL,
                    graph_digest TEXT NOT NULL,
                    completed_run_ids_json TEXT NOT NULL,
                    completed_results_digest TEXT NOT NULL,
                    status TEXT NOT NULL,
                    admitted_at TEXT NOT NULL,
                    claimed_at TEXT,
                    CHECK(status IN ('PENDING','CLAIMED'))
                );

                -- A source-device receipt written only after the remote
                -- authority has transferred an unclaimed continuation.  It
                -- contains no result body and prevents an offline retry on
                -- the source from consuming progress after a handoff.
                CREATE TABLE IF NOT EXISTS partial_job_continuation_handoffs (
                    job_id TEXT PRIMARY KEY REFERENCES job_snapshots(job_id),
                    target_device_id TEXT NOT NULL,
                    request_snapshot_hash TEXT NOT NULL,
                    graph_digest TEXT NOT NULL,
                    completed_results_digest TEXT NOT NULL,
                    handed_off_at TEXT NOT NULL
                );

                -- Written before contacting the remote authority.  A crash
                -- after this point must prefer blocking the local source over
                -- risking a second continuation claim.  A failed remote
                -- request may cancel this exact preparation; an uncertain
                -- transport outcome intentionally requires operator review.
                CREATE TABLE IF NOT EXISTS partial_job_continuation_handoff_preparations (
                    job_id TEXT PRIMARY KEY REFERENCES job_snapshots(job_id),
                    target_device_id TEXT NOT NULL,
                    request_snapshot_hash TEXT NOT NULL,
                    graph_digest TEXT NOT NULL,
                    completed_results_digest TEXT NOT NULL,
                    prepared_at TEXT NOT NULL
                );

                -- User-local continuation material, deliberately separate
                -- from the content-free ACTIVE JOB chain.  It stores the
                -- redacted typed result required to satisfy a later dependent
                -- task, never prompt, tool-call, approval, or source payload.
                CREATE TABLE IF NOT EXISTS job_dependency_result_receipts (
                    job_id TEXT NOT NULL REFERENCES job_snapshots(job_id),
                    task_id TEXT NOT NULL,
                    attempt_id TEXT NOT NULL REFERENCES job_attempts(attempt_id),
                    result_json TEXT NOT NULL,
                    result_digest TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(job_id, task_id),
                    UNIQUE(attempt_id)
                );

                CREATE TABLE IF NOT EXISTS job_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES job_snapshots(job_id),
                    ledger_seq INTEGER NOT NULL,
                    task_id TEXT NOT NULL,
                    attempt_sequence INTEGER NOT NULL,
                    employee_id TEXT NOT NULL,
                    source_attempt_id TEXT REFERENCES job_attempts(attempt_id),
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    previous_chain_hash TEXT NOT NULL,
                    chain_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(job_id, ledger_seq),
                    UNIQUE(job_id, task_id, attempt_sequence)
                );

                CREATE INDEX IF NOT EXISTS job_attempts_job_seq_idx
                    ON job_attempts(job_id, ledger_seq);

                CREATE TABLE IF NOT EXISTS job_mutations (
                    event_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES job_snapshots(job_id),
                    ledger_seq INTEGER NOT NULL,
                    event_sequence INTEGER NOT NULL,
                    mutation_type TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    source_attempt_id TEXT NOT NULL REFERENCES job_attempts(attempt_id),
                    target_attempt_id TEXT NOT NULL,
                    from_employee_id TEXT NOT NULL,
                    to_employee_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    previous_chain_hash TEXT NOT NULL,
                    chain_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(job_id, ledger_seq),
                    UNIQUE(job_id, event_sequence),
                    UNIQUE(job_id, target_attempt_id)
                );

                CREATE INDEX IF NOT EXISTS job_mutations_job_seq_idx
                    ON job_mutations(job_id, ledger_seq);

                CREATE TABLE IF NOT EXISTS job_graph_patches (
                    event_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES job_snapshots(job_id),
                    ledger_seq INTEGER NOT NULL,
                    event_sequence INTEGER NOT NULL,
                    patch_id TEXT NOT NULL,
                    semantic_operation TEXT NOT NULL,
                    base_graph_version INTEGER NOT NULL,
                    target_graph_version INTEGER NOT NULL,
                    trigger_task_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    previous_chain_hash TEXT NOT NULL,
                    chain_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(job_id, ledger_seq),
                    UNIQUE(job_id, event_sequence),
                    UNIQUE(job_id, patch_id)
                );

                CREATE INDEX IF NOT EXISTS job_graph_patches_job_seq_idx
                    ON job_graph_patches(job_id, ledger_seq);

                CREATE TABLE IF NOT EXISTS job_graph_proposals (
                    event_id TEXT PRIMARY KEY,
                    proposal_id TEXT NOT NULL,
                    job_id TEXT NOT NULL REFERENCES job_snapshots(job_id),
                    ledger_seq INTEGER NOT NULL,
                    decision_sequence INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('PENDING','APPROVED','REJECTED','UNAVAILABLE')),
                    semantic_operation TEXT NOT NULL,
                    base_graph_version INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    previous_chain_hash TEXT NOT NULL,
                    chain_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(job_id, ledger_seq),
                    UNIQUE(job_id, decision_sequence),
                    UNIQUE(job_id, proposal_id, status)
                );

                CREATE INDEX IF NOT EXISTS job_graph_proposals_job_seq_idx
                    ON job_graph_proposals(job_id, ledger_seq);

                CREATE TABLE IF NOT EXISTS job_terminal_events (
                    job_id TEXT PRIMARY KEY REFERENCES job_snapshots(job_id),
                    ledger_seq INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    final_graph_version INTEGER NOT NULL,
                    task_attempt_count INTEGER NOT NULL,
                    task_mutation_count INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    previous_chain_hash TEXT NOT NULL,
                    chain_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(job_id, ledger_seq)
                );

                CREATE TABLE IF NOT EXISTS job_lifecycle_state (
                    job_id TEXT PRIMARY KEY REFERENCES job_snapshots(job_id),
                    request_id TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK(state IN ('ADMITTED','DEFERRED','PAUSED','CANCELLED','TERMINAL')),
                    CHECK(revision >= 1)
                );

                CREATE TABLE IF NOT EXISTS job_lifecycle_events (
                    event_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES job_lifecycle_state(job_id),
                    sequence INTEGER NOT NULL,
                    operation TEXT NOT NULL,
                    from_state TEXT,
                    to_state TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(job_id, sequence)
                );

                -- One exact candidate may be resumed once after an external
                -- Graph proposal decision.  This stores only frozen
                -- identities, never patch bodies or runtime result content.
                CREATE TABLE IF NOT EXISTS graph_proposal_continuations (
                    job_id TEXT NOT NULL REFERENCES job_snapshots(job_id),
                    proposal_id TEXT NOT NULL,
                    request_snapshot_hash TEXT NOT NULL,
                    before_graph_digest TEXT NOT NULL,
                    after_graph_digest TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('PENDING','CLAIMED')),
                    created_at TEXT NOT NULL,
                    claimed_at TEXT,
                    PRIMARY KEY(job_id, proposal_id)
                );

                -- A graph rewrite reserves its newly introduced execution
                -- capacity before the rewritten graph can be dispatched.
                -- This is deliberately separate from Company budget leases:
                -- it is a Job-local, replayable commitment with no authority
                -- to expand the already-admitted Company budget.
                CREATE TABLE IF NOT EXISTS job_lifecycle_leases (
                    lease_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES job_lifecycle_state(job_id),
                    kind TEXT NOT NULL,
                    model_calls INTEGER NOT NULL,
                    tool_calls INTEGER NOT NULL,
                    cost_usd REAL NOT NULL,
                    payload_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    settled_at TEXT,
                    CHECK(kind = 'GRAPH_MUTATION'),
                    CHECK(status IN ('ACTIVE','SETTLED','RELEASED')),
                    CHECK(model_calls >= 0),
                    CHECK(tool_calls >= 0),
                    CHECK(cost_usd >= 0)
                );

                CREATE INDEX IF NOT EXISTS job_lifecycle_leases_job_status_idx
                    ON job_lifecycle_leases(job_id, status);

                CREATE TABLE IF NOT EXISTS job_supervision_events (
                    event_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES job_snapshots(job_id),
                    attempt_id TEXT NOT NULL REFERENCES job_attempts(attempt_id),
                    sequence INTEGER NOT NULL,
                    task_id TEXT NOT NULL,
                    manager_employee_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    signal_code TEXT,
                    priority TEXT NOT NULL,
                    deadline_bucket TEXT NOT NULL,
                    capability_shortage_count INTEGER NOT NULL,
                    conflicting_outcome INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(job_id, sequence),
                    UNIQUE(job_id, attempt_id),
                    CHECK(action IN ('CONTINUE','SIGNAL')),
                    CHECK(deadline_bucket IN ('READY','NEAR','EXPIRED')),
                    CHECK(capability_shortage_count >= 0),
                    CHECK(conflicting_outcome IN (0,1))
                );

                CREATE INDEX IF NOT EXISTS job_supervision_events_job_idx
                    ON job_supervision_events(job_id, sequence);

                CREATE TABLE IF NOT EXISTS job_operator_signals (
                    signal_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES job_lifecycle_state(job_id),
                    target_task_id TEXT NOT NULL,
                    code TEXT NOT NULL,
                    reference TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    consumed_at TEXT,
                    CHECK(code IN ('USER_CORRECTION')),
                    CHECK(status IN ('PENDING','CONSUMED'))
                );

                CREATE INDEX IF NOT EXISTS job_operator_signals_pending_idx
                    ON job_operator_signals(job_id, target_task_id, status, created_at);

                CREATE TABLE IF NOT EXISTS company_budget_leases (
                    job_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL UNIQUE,
                    company_revision INTEGER NOT NULL,
                    window_kind TEXT NOT NULL,
                    window_start TEXT NOT NULL,
                    window_end TEXT NOT NULL,
                    budget_limit_usd REAL NOT NULL,
                    reserved_cost_usd REAL NOT NULL,
                    actual_cost_usd REAL,
                    status TEXT NOT NULL,
                    admitted_at TEXT NOT NULL,
                    settled_at TEXT,
                    CHECK(status IN ('ACTIVE', 'SETTLED')),
                    CHECK(budget_limit_usd > 0),
                    CHECK(reserved_cost_usd >= 0),
                    CHECK(actual_cost_usd IS NULL OR actual_cost_usd >= 0)
                );

                CREATE INDEX IF NOT EXISTS company_budget_leases_window_idx
                    ON company_budget_leases(status, settled_at, admitted_at);

                CREATE TABLE IF NOT EXISTS company_budget_forfeits (
                    job_id TEXT PRIMARY KEY REFERENCES company_budget_leases(job_id),
                    request_id TEXT NOT NULL UNIQUE,
                    company_revision INTEGER NOT NULL,
                    charged_cost_usd REAL NOT NULL,
                    reason TEXT NOT NULL,
                    forfeited_at TEXT NOT NULL,
                    CHECK(charged_cost_usd >= 0)
                );

                CREATE TABLE IF NOT EXISTS company_budget_incidents (
                    incident_id TEXT PRIMARY KEY,
                    company_revision INTEGER NOT NULL,
                    window_kind TEXT NOT NULL,
                    window_start TEXT NOT NULL,
                    window_end TEXT NOT NULL,
                    budget_limit_usd REAL NOT NULL,
                    observed_cost_usd REAL NOT NULL,
                    reserved_cost_usd REAL NOT NULL,
                    requested_cost_usd REAL NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT,
                    resolved_by TEXT,
                    CHECK(status IN ('OPEN', 'RESOLVED')),
                    CHECK(budget_limit_usd > 0),
                    CHECK(observed_cost_usd >= 0),
                    CHECK(reserved_cost_usd >= 0),
                    CHECK(requested_cost_usd >= 0)
                );

                CREATE INDEX IF NOT EXISTS company_budget_incidents_open_idx
                    ON company_budget_incidents(status, window_start, created_at);

                CREATE TABLE IF NOT EXISTS company_budget_pause_state (
                    scope TEXT PRIMARY KEY CHECK(scope = 'company'),
                    incident_id TEXT NOT NULL REFERENCES company_budget_incidents(incident_id),
                    paused_at TEXT NOT NULL
                );

                CREATE TRIGGER IF NOT EXISTS job_snapshots_no_update
                BEFORE UPDATE ON job_snapshots BEGIN
                    SELECT RAISE(ABORT, 'job_snapshots are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS job_snapshots_no_delete
                BEFORE DELETE ON job_snapshots BEGIN
                    SELECT RAISE(ABORT, 'job_snapshots are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS job_attempts_no_update
                BEFORE UPDATE ON job_attempts BEGIN
                    SELECT RAISE(ABORT, 'job_attempts are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS job_attempts_no_delete
                BEFORE DELETE ON job_attempts BEGIN
                    SELECT RAISE(ABORT, 'job_attempts are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS job_mutations_no_update
                BEFORE UPDATE ON job_mutations BEGIN
                    SELECT RAISE(ABORT, 'job_mutations are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS job_mutations_no_delete
                BEFORE DELETE ON job_mutations BEGIN
                    SELECT RAISE(ABORT, 'job_mutations are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS job_graph_patches_no_update
                BEFORE UPDATE ON job_graph_patches BEGIN
                    SELECT RAISE(ABORT, 'job_graph_patches are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS job_graph_patches_no_delete
                BEFORE DELETE ON job_graph_patches BEGIN
                    SELECT RAISE(ABORT, 'job_graph_patches are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS job_graph_proposals_no_update
                BEFORE UPDATE ON job_graph_proposals BEGIN
                    SELECT RAISE(ABORT, 'job_graph_proposals are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS job_graph_proposals_no_delete
                BEFORE DELETE ON job_graph_proposals BEGIN
                    SELECT RAISE(ABORT, 'job_graph_proposals are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS job_terminal_events_no_update
                BEFORE UPDATE ON job_terminal_events BEGIN
                    SELECT RAISE(ABORT, 'job_terminal_events are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS job_terminal_events_no_delete
                BEFORE DELETE ON job_terminal_events BEGIN
                    SELECT RAISE(ABORT, 'job_terminal_events are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS company_budget_forfeits_no_update
                BEFORE UPDATE ON company_budget_forfeits BEGIN
                    SELECT RAISE(ABORT, 'company_budget_forfeits are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS company_budget_forfeits_no_delete
                BEFORE DELETE ON company_budget_forfeits BEGIN
                    SELECT RAISE(ABORT, 'company_budget_forfeits are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS effect_recovery_cases_no_update
                BEFORE UPDATE ON effect_recovery_cases BEGIN
                    SELECT RAISE(ABORT, 'effect_recovery_cases are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS effect_recovery_cases_no_delete
                BEFORE DELETE ON effect_recovery_cases BEGIN
                    SELECT RAISE(ABORT, 'effect_recovery_cases are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS effect_recovery_resolutions_no_update
                BEFORE UPDATE ON effect_recovery_resolutions BEGIN
                    SELECT RAISE(ABORT, 'effect_recovery_resolutions are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS effect_recovery_resolutions_no_delete
                BEFORE DELETE ON effect_recovery_resolutions BEGIN
                    SELECT RAISE(ABORT, 'effect_recovery_resolutions are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS effect_remote_resource_claims_no_update
                BEFORE UPDATE ON effect_remote_resource_claims BEGIN
                    SELECT RAISE(ABORT, 'effect_remote_resource_claims are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS effect_remote_resource_claims_no_delete
                BEFORE DELETE ON effect_remote_resource_claims BEGIN
                    SELECT RAISE(ABORT, 'effect_remote_resource_claims are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS effect_remote_resource_releases_no_update
                BEFORE UPDATE ON effect_remote_resource_releases BEGIN
                    SELECT RAISE(ABORT, 'effect_remote_resource_releases are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS effect_remote_resource_releases_no_delete
                BEFORE DELETE ON effect_remote_resource_releases BEGIN
                    SELECT RAISE(ABORT, 'effect_remote_resource_releases are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS effect_remote_resolution_preparations_no_update
                BEFORE UPDATE ON effect_remote_resolution_preparations BEGIN
                    SELECT RAISE(ABORT, 'effect_remote_resolution_preparations are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS effect_remote_resolution_preparations_no_delete
                BEFORE DELETE ON effect_remote_resolution_preparations BEGIN
                    SELECT RAISE(ABORT, 'effect_remote_resolution_preparations are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS effect_remote_resource_resolutions_no_update
                BEFORE UPDATE ON effect_remote_resource_resolutions BEGIN
                    SELECT RAISE(ABORT, 'effect_remote_resource_resolutions are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS effect_remote_resource_resolutions_no_delete
                BEFORE DELETE ON effect_remote_resource_resolutions BEGIN
                    SELECT RAISE(ABORT, 'effect_remote_resource_resolutions are append-only');
                END;
                """
            )
            self._initialize_continuation_preflight_schema(conn)
            # V21 records the immutable execution contract beside each tool
            # intent.  Older receipts did not retain enough proof to support
            # effectful recovery, so their NULL values deliberately fail
            # closed in the recovery inspector.
            action_columns = {
                str(item["name"])
                for item in conn.execute("PRAGMA table_info(tool_actions)").fetchall()
            }
            if "effect" not in action_columns:
                conn.execute("ALTER TABLE tool_actions ADD COLUMN effect TEXT")
            if "idempotency_mode" not in action_columns:
                conn.execute("ALTER TABLE tool_actions ADD COLUMN idempotency_mode TEXT")
            reservation_columns = {
                str(item["name"])
                for item in conn.execute(
                    "PRAGMA table_info(employee_run_model_invocation_dispatch_reservations)"
                ).fetchall()
            }
            # V30 scopes in-flight invocation reservations to one local
            # service/store process.  Existing pre-epoch reservations are
            # deliberately treated as a prior process on their next dispatch.
            if reservation_columns and "dispatch_epoch" not in reservation_columns:
                conn.execute(
                    "ALTER TABLE employee_run_model_invocation_dispatch_reservations "
                    "ADD COLUMN dispatch_epoch TEXT NOT NULL DEFAULT 'legacy-unknown'"
                )
            self._migrate_graph_proposal_schema(conn)
            self._initialize_schema_version_and_sanitize(
                conn,
                schema_version=SCHEMA_VERSION,
                supported_versions=range(1, SCHEMA_VERSION + 1),
            )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()

    def subscribe(self, callback: Callable[[RunEvent], None]) -> None:
        self._subscribers.append(callback)

    def schema_version(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM runtime_meta WHERE key = 'schema_version'"
            ).fetchone()
        if row is None:
            raise RuntimeError("Runtime schema version is missing")
        return int(row["value"])

    def _notify(self, event: RunEvent) -> None:
        for callback in tuple(self._subscribers):
            try:
                callback(event)
            except Exception:
                continue

    def close(self) -> None:
        with self._lock:
            self._conn.close()
