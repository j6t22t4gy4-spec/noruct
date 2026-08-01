from __future__ import annotations

import asyncio
import io
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dynamic_firm.kernel.graph import graph_from_proposal
from dynamic_firm.cli import EXIT_OK, main
from dynamic_firm.kernel.models import (
    EmployeeRecord,
    GraphMutationLease,
    GraphPatch,
    GraphPatchProposalStatus,
    SemanticOperation,
)
from dynamic_firm.company.frontdoor import (
    AuthoritySnapshotIdentity,
    WorkOrderBudgetSnapshot,
    normalize_work_order,
)
from dynamic_firm.kernel.mutation import (
    content_digest,
    frozen_snapshot_digest,
    graph_patch_proposal_event,
)
from dynamic_firm.kernel.service import FirmKernel
from dynamic_firm.kernel.testing import ScriptedEmployeeExecutionPort, ScriptedOutcome
from dynamic_firm.runtime.job_ledger import (
    ActiveJobAuditStatus,
    ActiveJobInspector,
    SQLiteActiveJobLedger,
)
from dynamic_firm.runtime.models import (
    ContextBundle,
    EmployeeRunResult,
    EventType,
    Failure,
    FailureCategory,
    RunStatus,
    SignalCode,
    Usage,
    to_primitive,
)
from dynamic_firm.runtime.store import SCHEMA_VERSION, RunStore
from tests.kernel.helpers import company_request, task
from tests.runtime.test_approval_lifecycle import _request, _stage_waiting_approval


class _DropTerminalLedger(SQLiteActiveJobLedger):
    def finish_job(self, job_id, result) -> None:  # type: ignore[no-untyped-def]
        return None


class ActiveJobLedgerTests(unittest.TestCase):
    def test_pending_and_resolved_graph_proposals_share_candidate_identity(self) -> None:
        request = company_request(
            (task("final"),),
            final_task_id="final",
            roster=(EmployeeRecord("analyst", "Analyst", ("analysis",)),),
        )
        graph = graph_from_proposal(
            request.plan_proposal,
            max_tasks=request.job_limits.max_tasks,
        )
        patch = GraphPatch(
            patch_id="candidate-identity",
            base_graph_version=graph.version,
            trigger_task_id="final",
            semantic_operation=SemanticOperation.INSERT,
            rationale="bounded test candidate",
            expected_gain="bounded test gain",
            operations=(),
        )
        pending = graph_patch_proposal_event(
            patch=patch,
            before=graph,
            after=graph,
            proposed_lease=GraphMutationLease(),
            status=GraphPatchProposalStatus.PENDING,
        )
        approved = graph_patch_proposal_event(
            patch=patch,
            before=graph,
            after=graph,
            proposed_lease=GraphMutationLease(),
            status=GraphPatchProposalStatus.APPROVED,
        )

        self.assertEqual(pending.proposal_id, approved.proposal_id)
        self.assertNotEqual(pending.event_id, approved.event_id)
        self.assertNotEqual(pending.content_hash, approved.content_hash)

    def test_pending_graph_proposal_persists_and_resolves_once(self) -> None:
        request = company_request(
            (task("final"),),
            final_task_id="final",
            roster=(EmployeeRecord("analyst", "Analyst", ("analysis",)),),
        )
        graph = graph_from_proposal(
            request.plan_proposal,
            max_tasks=request.job_limits.max_tasks,
        )
        patch = GraphPatch(
            patch_id="durable-proposal",
            base_graph_version=graph.version,
            trigger_task_id="final",
            semantic_operation=SemanticOperation.INSERT,
            rationale="bounded durable proposal",
            expected_gain="approval lifecycle coverage",
            operations=(),
        )
        pending = graph_patch_proposal_event(
            patch=patch,
            before=graph,
            after=graph,
            proposed_lease=GraphMutationLease(),
            status=GraphPatchProposalStatus.PENDING,
        )
        approved = graph_patch_proposal_event(
            patch=patch,
            before=graph,
            after=graph,
            proposed_lease=GraphMutationLease(),
            status=GraphPatchProposalStatus.APPROVED,
        )
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory) / "runtime.db")
            ledger = SQLiteActiveJobLedger(store)
            ledger.start_job(request, graph, frozen_snapshot_digest(request))
            ledger.append_graph_proposal(request.job_id, pending)
            ledger.hold_graph_proposal(request.job_id, pending)
            self.assertEqual(
                ledger.pending_graph_proposal(request.job_id, pending.proposal_id),
                pending,
            )
            ledger.resolve_graph_proposal(request.job_id, approved)
            ledger.claim_approved_graph_proposal(request.job_id, approved)
            ledger.claim_approved_graph_proposal(request.job_id, approved)
            rows = store.get_job_ledger_rows(request.job_id)
            assert rows is not None
            self.assertEqual(
                [str(row["status"]) for row in rows["graph_proposals"]],
                ["PENDING", "APPROVED"],
            )
            lifecycle = store.get_job_lifecycle(request.job_id)
            assert lifecycle is not None
            self.assertEqual(lifecycle["state"], "PAUSED")
            with self.assertRaisesRegex(ValueError, "already been resolved"):
                ledger.resolve_graph_proposal(request.job_id, approved)
            store.close()

    def test_graph_proposal_store_migrates_legacy_status_constraint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.db"
            store = RunStore(path)
            store.close()
            connection = sqlite3.connect(path)
            try:
                connection.executescript(
                    """
                    DROP TRIGGER IF EXISTS job_graph_proposals_no_update;
                    DROP TRIGGER IF EXISTS job_graph_proposals_no_delete;
                    DROP INDEX IF EXISTS job_graph_proposals_job_seq_idx;
                    DROP TABLE job_graph_proposals;
                    CREATE TABLE job_graph_proposals (
                        event_id TEXT PRIMARY KEY,
                        job_id TEXT NOT NULL REFERENCES job_snapshots(job_id),
                        ledger_seq INTEGER NOT NULL,
                        decision_sequence INTEGER NOT NULL,
                        status TEXT NOT NULL CHECK(status IN ('APPROVED','REJECTED','UNAVAILABLE')),
                        semantic_operation TEXT NOT NULL,
                        base_graph_version INTEGER NOT NULL,
                        payload_json TEXT NOT NULL,
                        payload_hash TEXT NOT NULL,
                        previous_chain_hash TEXT NOT NULL,
                        chain_hash TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE(job_id, ledger_seq),
                        UNIQUE(job_id, decision_sequence)
                    );
                    UPDATE runtime_meta SET value = '19' WHERE key = 'schema_version';
                    """
                )
                connection.commit()
            finally:
                connection.close()
            migrated = RunStore(path)
            try:
                definition = migrated._conn.execute(  # noqa: SLF001 - migration contract
                    "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'job_graph_proposals'"
                ).fetchone()[0]
                columns = {
                    row[1]
                    for row in migrated._conn.execute(  # noqa: SLF001 - migration contract
                        "PRAGMA table_info(job_graph_proposals)"
                    )
                }
                self.assertIn("proposal_id", columns)
                self.assertIn("'PENDING'", definition)
                self.assertEqual(migrated.schema_version(), SCHEMA_VERSION)
            finally:
                migrated.close()

    def test_terminal_projects_propose_decisions_without_promoting_them_to_lineage(self) -> None:
        request = company_request(
            (task("final"),),
            final_task_id="final",
            roster=(EmployeeRecord("analyst", "Analyst", ("analysis",)),),
        )
        graph = graph_from_proposal(
            request.plan_proposal,
            max_tasks=request.job_limits.max_tasks,
        )
        event = graph_patch_proposal_event(
            patch=GraphPatch(
                patch_id="private-proposal-id",
                base_graph_version=graph.version,
                trigger_task_id="final",
                semantic_operation=SemanticOperation.INSERT,
                rationale="Never expose this graph rationale in the audit surface.",
                expected_gain="Never expose this expected gain either.",
                operations=(),
            ),
            before=graph,
            after=graph,
            proposed_lease=GraphMutationLease(
                model_calls=1,
                tool_calls=2,
                cost_usd=0.03,
            ),
            status=GraphPatchProposalStatus.REJECTED,
        )

        class ProposalLedger(SQLiteActiveJobLedger):
            def finish_job(self, job_id, result) -> None:  # type: ignore[no-untyped-def]
                self.append_graph_proposal(job_id, event)
                super().finish_job(
                    job_id,
                    replace(result, graph_patch_proposal_events=(event,)),
                )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.db"
            store = RunStore(path)
            result = asyncio.run(
                FirmKernel(
                    employee_execution=ScriptedEmployeeExecutionPort(
                        {"final": ScriptedOutcome("done")}
                    ),
                    active_job_ledger=ProposalLedger(store),
                ).run(request)
            )
            self.assertEqual(result.metrics.graph_patch_count, 0)
            terminal = store.get_job_ledger_rows(request.job_id)
            assert terminal is not None and terminal["terminal"] is not None
            terminal_text = str(terminal["terminal"]["payload_json"])
            inspection = ActiveJobInspector(store).inspect(request.job_id)
            store.close()

        self.assertTrue(inspection.replay_matches)
        self.assertEqual(inspection.graph_patch_count, 0)
        self.assertEqual(
            inspection.graph_proposal_decisions,
            (
                {
                    "sequence": 1,
                    "ledger_sequence": 2,
                    "status": "REJECTED",
                    "operation": "INSERT",
                    "base_graph_version": 1,
                    "proposed_lease": {
                        "model_calls": 1,
                        "tool_calls": 2,
                        "cost_usd": 0.03,
                    },
                },
            ),
        )
        self.assertNotIn("Never expose", terminal_text)
        self.assertNotIn("private-proposal-id", terminal_text)

    def test_planning_provenance_and_compiler_usage_survive_replay(self) -> None:
        compiler_usage = Usage(
            model_calls=1,
            input_tokens=90,
            cached_input_tokens=10,
            output_tokens=20,
            cost_usd=0.15,
        )
        employee_usage = Usage(
            model_calls=1,
            tool_calls=2,
            input_tokens=40,
            output_tokens=15,
            cost_usd=0.35,
        )
        request = replace(
            company_request(
                (task("final"),),
                final_task_id="final",
                roster=(EmployeeRecord("analyst", "Analyst", ("analysis",)),),
            ),
            planning_mode="DYNAMIC",
            planning_reason="VALID_DYNAMIC",
            compiler_usage=compiler_usage,
            compiler_provider_request_id="provider-plan-17",
            work_order_id="work-order-plan-17",
            work_order_digest="a" * 64,
            work_order_authority_digest="b" * 64,
            firm_admission_digest="f" * 64,
            company_work_mode="TEAM_JOB",
            coordination_policy="PLAN_FIRST",
            requested_effect="READ",
            operating_reason="STRUCTURED_MULTI_WORKSTREAM",
            graph_blueprint_id="pricing-investigation",
            graph_blueprint_version=3,
            graph_blueprint_digest="c" * 64,
            graph_mutation_policy="LOCKED",
            graph_constraints_digest="d" * 64,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.db"
            store = RunStore(path)
            result = asyncio.run(
                FirmKernel(
                    employee_execution=ScriptedEmployeeExecutionPort(
                        {"final": ScriptedOutcome("done", usage=employee_usage)}
                    ),
                    active_job_ledger=SQLiteActiveJobLedger(store),
                ).run(request)
            )
            rows = store.get_job_ledger_rows(request.job_id)
            assert rows is not None and rows["terminal"] is not None
            snapshot = json.loads(rows["snapshot"]["payload_json"])
            terminal = json.loads(rows["terminal"]["payload_json"])
            store.close()

            reopened = RunStore(path)
            inspection = ActiveJobInspector(reopened).inspect(request.job_id)
            reopened.close()

        self.assertEqual(result.metrics.usage, compiler_usage.plus(employee_usage))
        self.assertEqual(snapshot["planning"], terminal["planning"])
        self.assertEqual(inspection.planning_mode, "DYNAMIC")
        self.assertEqual(inspection.planning_reason, "VALID_DYNAMIC")
        self.assertEqual(inspection.compiler_usage, compiler_usage)
        self.assertEqual(
            inspection.compiler_provider_request_id,
            "provider-plan-17",
        )
        self.assertEqual(inspection.work_order_id, "work-order-plan-17")
        self.assertEqual(inspection.work_order_digest, "a" * 64)
        self.assertEqual(inspection.work_order_authority_digest, "b" * 64)
        self.assertEqual(inspection.firm_admission_digest, "f" * 64)
        self.assertEqual(snapshot["graph_blueprint"], {
            "blueprint_id": "pricing-investigation",
            "blueprint_version": 3,
            "blueprint_digest": "c" * 64,
            "mutation_policy": "LOCKED",
            "constraints_digest": "d" * 64,
            "constraints": {
                "pinned_employee_ids": [],
                "excluded_employee_ids": [],
                "require_independent_review": False,
                "max_concurrency": None,
                "max_cost_usd": None,
                "max_wall_time_ms": None,
            },
            "initial_graph_digest": snapshot["graph_blueprint"]["initial_graph_digest"],
        })
        self.assertEqual(
            terminal["graph_blueprint"],
            {
                "blueprint_id": "pricing-investigation",
                "blueprint_version": 3,
                "blueprint_digest": "c" * 64,
                "mutation_policy": "LOCKED",
                "constraints_digest": "d" * 64,
                "constraints": {
                    "pinned_employee_ids": [],
                    "excluded_employee_ids": [],
                    "require_independent_review": False,
                    "max_concurrency": None,
                    "max_cost_usd": None,
                    "max_wall_time_ms": None,
                },
            },
        )
        self.assertEqual(inspection.graph_blueprint_id, "pricing-investigation")
        self.assertEqual(inspection.graph_blueprint_version, 3)
        self.assertEqual(inspection.graph_blueprint_digest, "c" * 64)
        self.assertEqual(inspection.graph_mutation_policy, "LOCKED")
        self.assertEqual(inspection.graph_constraints_digest, "d" * 64)
        self.assertEqual(len(inspection.initial_graph_digest), 64)
        self.assertTrue(inspection.replay_matches)

    def _retry_request(self):
        return replace(
            company_request(
                (
                    task("analysis"),
                    task("final", depends_on=("analysis",), capabilities=("integration",)),
                ),
                final_task_id="final",
                roster=(
                    EmployeeRecord("analyst", "Analyst", ("analysis",)),
                    EmployeeRecord("integrator", "Integrator", ("integration",)),
                ),
            ),
            company_revision=3,
            roster_revision=5,
            playbook_revision=7,
        )

    def _retry_runner(self, *, secret_summary: str = "Recovered"):
        transient = Failure(
            "MODEL_TRANSIENT",
            FailureCategory.MODEL,
            "The model transport failed temporarily.",
            retryable=True,
        )
        return ScriptedEmployeeExecutionPort(
            {
                "analysis": (
                    ScriptedOutcome(
                        "Transient",
                        status=RunStatus.FAILED,
                        failure=transient,
                    ),
                    ScriptedOutcome(secret_summary),
                ),
                "final": ScriptedOutcome(secret_summary),
            }
        )

    def test_retry_ledger_reopens_with_exact_chain_and_privacy_projection(self) -> None:
        redaction_marker = "SECRET-SENTINEL-DO-NOT-PERSIST"
        tool_output = "RAW-TOOL-OUTPUT-DO-NOT-PERSIST"
        base = self._retry_request()
        request = replace(
            base,
            goal=redaction_marker,
            plan_proposal=replace(
                base.plan_proposal,
                goal=redaction_marker,
                tasks=tuple(
                    replace(item, objective=f"{item.objective} {redaction_marker}")
                    for item in base.plan_proposal.tasks
                ),
            ),
            context_snapshot=ContextBundle(
                company_policy_excerpt=redaction_marker,
                ephemeral_instructions=(redaction_marker,),
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.db"
            store = RunStore(path)
            result = asyncio.run(
                FirmKernel(
                    employee_execution=self._retry_runner(secret_summary=tool_output),
                    active_job_ledger=SQLiteActiveJobLedger(store),
                ).run(request)
            )
            first = ActiveJobInspector(store).inspect(request.job_id)
            persisted = "\n".join(store.active_job_table_payloads(request.job_id))
            store.close()

            reopened = RunStore(path)
            second = ActiveJobInspector(reopened).inspect(request.job_id)

            self.assertEqual(first, second)
            self.assertEqual(second.audit_status, ActiveJobAuditStatus.TERMINAL)
            self.assertTrue(second.replay_matches)
            self.assertEqual(second.job_status, "SUCCEEDED")
            self.assertEqual(second.attempt_count, 3)
            self.assertEqual(second.mutation_count, 1)
            self.assertEqual(
                second.mutations[0]["source_attempt_content_hash"],
                second.attempts[0]["content_hash"],
            )
            self.assertEqual(result.metrics.task_mutation_count, 1)
            self.assertNotIn(redaction_marker, persisted)
            self.assertNotIn(tool_output, persisted)
            reopened.close()

    def test_checkpoint_history_is_parent_linked_and_never_a_resume_token(self) -> None:
        request = self._retry_request()
        store = RunStore()
        result = asyncio.run(
            FirmKernel(
                employee_execution=self._retry_runner(),
                active_job_ledger=SQLiteActiveJobLedger(store),
            ).run(request)
        )

        history = ActiveJobInspector(store).checkpoints(request.job_id)

        self.assertEqual(result.status.value, "SUCCEEDED")
        self.assertEqual(
            tuple(item.event_type for item in history.checkpoints),
            ("ADMITTED", "ATTEMPT", "MUTATION", "ATTEMPT", "ATTEMPT", "TERMINAL"),
        )
        self.assertEqual(history.checkpoint_count, len(history.checkpoints))
        self.assertFalse(history.automatic_resume)
        self.assertTrue(all(not item.resumable for item in history.checkpoints))
        self.assertIsNone(history.checkpoints[0].parent_checkpoint_id)
        self.assertTrue(
            all(
                current.parent_checkpoint_id == previous.checkpoint_id
                for previous, current in zip(history.checkpoints, history.checkpoints[1:])
            )
        )
        self.assertEqual(history.checkpoints[2].changed_task_ids, ("analysis",))
        final = history.checkpoints[-1]
        self.assertEqual(final.event_type, "TERMINAL")
        self.assertEqual(
            {item["task_id"]: item["status"] for item in final.task_states},
            {"analysis": "SUCCEEDED", "final": "SUCCEEDED"},
        )
        store.close()

    def test_job_checkpoints_cli_exposes_a_read_only_projection(self) -> None:
        request = self._retry_request()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.db"
            store = RunStore(path)
            asyncio.run(
                FirmKernel(
                    employee_execution=self._retry_runner(),
                    active_job_ledger=SQLiteActiveJobLedger(store),
                ).run(request)
            )
            store.close()

            output = io.StringIO()
            errors = io.StringIO()
            code = main(
                [
                    "job",
                    "checkpoints",
                    request.job_id,
                    "--state",
                    str(path),
                    "--json",
                ],
                stdout=output,
                stderr=errors,
            )

        self.assertEqual(code, EXIT_OK, errors.getvalue())
        payload = json.loads(output.getvalue())
        self.assertFalse(payload["automatic_resume"])
        self.assertEqual(payload["checkpoints"][0]["event_type"], "ADMITTED")
        self.assertTrue(
            all(not checkpoint["resumable"] for checkpoint in payload["checkpoints"])
        )

    def test_company_operating_identity_is_frozen_and_replayed(self) -> None:
        request = replace(
            self._retry_request(),
            company_work_mode="TEAM_JOB",
            coordination_policy="PLAN_FIRST",
            requested_effect="WORKSPACE_CHANGE",
            operating_reason="STRUCTURED_MULTI_WORKSTREAM",
        )
        frozen_hash = frozen_snapshot_digest(request)
        self.assertNotEqual(
            frozen_hash,
            frozen_snapshot_digest(
                replace(request, requested_effect="READ")
            ),
        )
        self.assertNotEqual(
            frozen_hash,
            frozen_snapshot_digest(
                replace(request, work_order_digest="c" * 64)
            ),
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.db"
            store = RunStore(path)
            result = asyncio.run(
                FirmKernel(
                    employee_execution=self._retry_runner(),
                    active_job_ledger=SQLiteActiveJobLedger(store),
                ).run(request)
            )
            before = ActiveJobInspector(store).inspect(request.job_id)
            rows = store.get_job_ledger_rows(request.job_id)
            assert rows is not None and rows["terminal"] is not None
            snapshot = json.loads(rows["snapshot"]["payload_json"])
            terminal = json.loads(rows["terminal"]["payload_json"])
            store.close()

            reopened = RunStore(path)
            after = ActiveJobInspector(reopened).inspect(request.job_id)
            reopened.close()

        self.assertEqual(before, after)
        self.assertTrue(after.replay_matches)
        self.assertEqual(after.initial_company_work_mode, "TEAM_JOB")
        self.assertEqual(after.company_work_mode, "TEAM_JOB")
        self.assertEqual(after.coordination_policy, "PLAN_FIRST")
        self.assertEqual(after.requested_effect, "WORKSPACE_CHANGE")
        self.assertEqual(after.operating_reason, "STRUCTURED_MULTI_WORKSTREAM")
        self.assertEqual(result.initial_company_work_mode, "TEAM_JOB")
        self.assertEqual(result.company_work_mode, "TEAM_JOB")
        self.assertEqual(
            snapshot["operating_decision"],
            terminal["operating_decision"],
        )

    def test_v1_snapshot_remains_a_valid_legacy_interrupted_audit(self) -> None:
        request = self._retry_request()
        graph = graph_from_proposal(
            request.plan_proposal,
            max_tasks=request.job_limits.max_tasks,
        )
        store = RunStore()
        store.create_job_snapshot(
            {
                "schema_version": "noruct.active-job-snapshot.v1",
                "job_id": request.job_id,
                "request_id": request.request_id,
                "proposal_id": request.plan_proposal.proposal_id,
                "graph_version": graph.version,
                "final_task_id": graph.final_task_id,
                "company_revision": request.company_revision,
                "roster_revision": request.roster_revision,
                "playbook_revision": request.playbook_revision,
                "frozen_snapshot_hash": "legacy-frozen-snapshot",
                "tasks": tuple(
                    {
                        "task_id": item.task_id,
                        "depends_on": item.depends_on,
                        "required_capabilities": item.required_capabilities,
                        "risk_level": item.risk_level,
                    }
                    for item in graph.tasks
                ),
            }
        )

        inspection = ActiveJobInspector(store).inspect(request.job_id)

        self.assertEqual(inspection.audit_status, ActiveJobAuditStatus.INTERRUPTED)
        self.assertTrue(inspection.replay_matches)
        self.assertEqual(inspection.initial_company_work_mode, "UNSPECIFIED")
        self.assertEqual(inspection.company_work_mode, "TEAM_JOB")
        self.assertEqual(inspection.coordination_policy, "PRECOMPILED")
        self.assertEqual(inspection.requested_effect, "UNSPECIFIED")
        self.assertEqual(inspection.operating_reason, "LEGACY_ACTIVE_JOB_V1")
        store.close()

    def test_plan_first_solo_admission_preserves_initial_and_effective_modes(self) -> None:
        request = replace(
            company_request(
                (task("final"),),
                final_task_id="final",
                roster=(EmployeeRecord("analyst", "Analyst", ("analysis",)),),
            ),
            company_work_mode="TEAM_JOB",
            coordination_policy="PLAN_FIRST",
            requested_effect="READ",
            operating_reason="STRUCTURED_MULTI_WORKSTREAM",
        )
        store = RunStore()
        result = asyncio.run(
            FirmKernel(
                employee_execution=ScriptedEmployeeExecutionPort(
                    {"final": ScriptedOutcome("Completed solo")}
                ),
                active_job_ledger=SQLiteActiveJobLedger(store),
            ).run(request)
        )
        inspection = ActiveJobInspector(store).inspect(request.job_id)

        self.assertEqual(result.initial_company_work_mode, "TEAM_JOB")
        self.assertEqual(result.company_work_mode, "SOLO_JOB")
        self.assertEqual(inspection.initial_company_work_mode, "TEAM_JOB")
        self.assertEqual(inspection.company_work_mode, "SOLO_JOB")
        self.assertEqual(inspection.coordination_policy, "PLAN_FIRST")
        self.assertTrue(inspection.replay_matches)
        store.close()

    def test_terminal_absence_is_interrupted_and_never_auto_resumed(self) -> None:
        request = self._retry_request()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.db"
            store = RunStore(path)
            asyncio.run(
                FirmKernel(
                    employee_execution=self._retry_runner(),
                    active_job_ledger=_DropTerminalLedger(store),
                ).run(request)
            )
            store.close()

            reopened = RunStore(path)
            inspection = ActiveJobInspector(reopened).inspect(request.job_id)
            self.assertEqual(inspection.audit_status, ActiveJobAuditStatus.INTERRUPTED)
            self.assertIsNone(inspection.job_status)
            self.assertFalse(inspection.automatic_resume)
            self.assertEqual(inspection.attempt_count, 3)
            self.assertEqual(inspection.mutation_count, 1)
            reopened.close()

    def test_interrupted_recovery_advice_requires_new_kernel_attempt_and_is_read_only(self) -> None:
        request = replace(
            self._retry_request(),
            work_order_digest="a" * 64,
            work_order_authority_digest="b" * 64,
            firm_admission_digest="c" * 64,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.db"
            store = RunStore(path)
            asyncio.run(
                FirmKernel(
                    employee_execution=self._retry_runner(),
                    active_job_ledger=_DropTerminalLedger(store),
                ).run(request)
            )
            before = store.active_job_table_payloads(request.job_id)
            advice = ActiveJobInspector(store).recovery_advice(request.job_id)
            after = store.active_job_table_payloads(request.job_id)

            self.assertEqual(advice.audit_status, ActiveJobAuditStatus.INTERRUPTED)
            self.assertEqual(
                advice.recovery_state,
                "INTERRUPTED_NEW_KERNEL_ATTEMPT_REQUIRED",
            )
            self.assertTrue(advice.requires_new_kernel_attempt)
            self.assertEqual(advice.runtime_run_statuses, ())
            self.assertIsNotNone(advice.local_continuation_candidate)
            assert advice.local_continuation_candidate is not None
            self.assertFalse(advice.local_continuation_candidate["dispatch_allowed"])
            self.assertEqual(
                advice.local_continuation_candidate["required_checks"],
                ("source_hashes", "approval_receipts", "budget_lease", "active_job_audit"),
            )
            self.assertTrue(
                any("new Company job" in item for item in advice.recommended_actions)
            )
            self.assertTrue(
                any("Do not resume" in item for item in advice.prohibited_actions)
            )
            self.assertEqual(before, after)
            store.close()

    def test_work_order_recovery_preparation_revalidates_external_authority(self) -> None:
        authority = AuthoritySnapshotIdentity(
            company_id="company-local",
            company_revision=3,
            roster_revision=5,
            playbook_revision=7,
            action_policy_digest="policy-fixture",
        )
        work_order = normalize_work_order(
            "Complete the fixture goal",
            work_order_id="work-order-recovery-fixture",
            authority_snapshot=authority,
            budget_snapshot=WorkOrderBudgetSnapshot(
                max_model_calls=64,
                max_tool_calls=128,
                max_cost_usd=20.0,
                max_wall_time_ms=5_000,
            ),
            requested_at=datetime.now(timezone.utc),
        )
        request = replace(
            self._retry_request(),
            work_order_id=work_order.work_order_id,
            work_order_digest=work_order.content_digest,
            work_order_authority_digest=authority.identity_digest,
            firm_admission_digest="f" * 64,
        )
        store = RunStore()
        asyncio.run(
            FirmKernel(
                employee_execution=self._retry_runner(),
                active_job_ledger=_DropTerminalLedger(store),
            ).run(request)
        )
        preparation = ActiveJobInspector(store).prepare_work_order_recovery(
            request.job_id,
            work_order=work_order,
            source_references={"firm_admission_digest": "f" * 64},
        )
        self.assertEqual(preparation.work_order_digest, work_order.content_digest)
        self.assertEqual(preparation.continuation_authority, "NEW_KERNEL_ATTEMPT_REQUIRED")
        self.assertIn("final", preparation.completed_task_ids)
        with self.assertRaisesRegex(ValueError, "does not match"):
            ActiveJobInspector(store).prepare_work_order_recovery(
                request.job_id,
                work_order=replace(work_order, work_order_id="other-work-order"),
                source_references={"firm_admission_digest": "f" * 64},
            )
        store.close()

    def test_explicit_same_job_fresh_start_continuation_is_one_shot_and_read_only(self) -> None:
        authority = AuthoritySnapshotIdentity(
            company_id="company-local",
            company_revision=3,
            roster_revision=5,
            playbook_revision=7,
            action_policy_digest="read-only-policy-fixture",
        )
        work_order = normalize_work_order(
            "Complete the fixture goal",
            work_order_id="work-order-same-job-fresh-start",
            authority_snapshot=authority,
            budget_snapshot=WorkOrderBudgetSnapshot(
                max_model_calls=64,
                max_tool_calls=128,
                max_cost_usd=20.0,
                max_wall_time_ms=5_000,
            ),
            requested_at=datetime.now(timezone.utc),
        )
        request = replace(
            self._retry_request(),
            request_id="request-same-job-fresh-start",
            job_id="job-same-job-fresh-start",
            work_order_id=work_order.work_order_id,
            work_order_digest=work_order.content_digest,
            work_order_authority_digest=authority.identity_digest,
            firm_admission_digest="f" * 64,
            requested_effect="READ",
        )
        store = RunStore()
        ledger = SQLiteActiveJobLedger(store)
        graph = graph_from_proposal(
            request.plan_proposal,
            max_tasks=request.job_limits.max_tasks,
        )
        # Simulate a process loss immediately after durable admission: there
        # is a snapshot, but no Employee run, attempt, mutation, or output.
        ledger.start_job(request, graph, frozen_snapshot_digest(request))

        continuation = ActiveJobInspector(store).authorize_same_job_fresh_start(
            request.job_id,
            request=request,
            work_order=work_order,
            source_references={"firm_admission_digest": "f" * 64},
        )
        self.assertEqual(
            continuation.continuation_authority,
            "SAME_JOB_FRESH_START_ALLOWED",
        )
        self.assertIn("exact_initial_graph", continuation.required_checks)

        result = asyncio.run(
            FirmKernel(
                employee_execution=ScriptedEmployeeExecutionPort(
                    {"analysis": ScriptedOutcome("analysis"), "final": ScriptedOutcome("final")}
                ),
                active_job_ledger=ledger,
            ).continue_same_job(request)
        )
        self.assertEqual(result.status.value, "SUCCEEDED")
        inspection = ActiveJobInspector(store).inspect(request.job_id)
        self.assertEqual(inspection.audit_status, ActiveJobAuditStatus.TERMINAL)
        with self.assertRaisesRegex(ValueError, "interrupted|Interrupted"):
            ActiveJobInspector(store).authorize_same_job_fresh_start(
                request.job_id,
                request=request,
                work_order=work_order,
                source_references={"firm_admission_digest": "f" * 64},
            )
        store.close()

    def test_same_job_continuation_rejects_any_prior_execution_history(self) -> None:
        authority = AuthoritySnapshotIdentity(
            company_id="company-local",
            company_revision=3,
            roster_revision=5,
            playbook_revision=7,
            action_policy_digest="read-only-policy-fixture",
        )
        work_order = normalize_work_order(
            "Complete the fixture goal",
            work_order_id="work-order-same-job-history",
            authority_snapshot=authority,
            budget_snapshot=WorkOrderBudgetSnapshot(
                max_model_calls=64,
                max_tool_calls=128,
                max_cost_usd=20.0,
                max_wall_time_ms=5_000,
            ),
            requested_at=datetime.now(timezone.utc),
        )
        request = replace(
            self._retry_request(),
            request_id="request-same-job-history",
            job_id="job-same-job-history",
            work_order_id=work_order.work_order_id,
            work_order_digest=work_order.content_digest,
            work_order_authority_digest=authority.identity_digest,
            firm_admission_digest="f" * 64,
            requested_effect="READ",
        )
        store = RunStore()
        asyncio.run(
            FirmKernel(
                employee_execution=self._retry_runner(),
                active_job_ledger=_DropTerminalLedger(store),
            ).run(request)
        )
        with self.assertRaisesRegex(ValueError, "no execution history"):
            ActiveJobInspector(store).authorize_same_job_fresh_start(
                request.job_id,
                request=request,
                work_order=work_order,
                source_references={"firm_admission_digest": "f" * 64},
            )
        store.close()

    def test_partial_read_only_continuation_binds_success_receipts_without_copying_results(self) -> None:
        class SharedContinuationAuthority:
            def __init__(self) -> None:
                self.admissions: dict[str, tuple[object, ...]] = {}
                self.claimed: set[str] = set()

            def authorize_partial_continuation(self, **value):  # type: ignore[no-untyped-def]
                key = str(value["job_id"])
                frozen = tuple(sorted(value.items()))
                existing = self.admissions.get(key)
                if existing is not None and existing != frozen:
                    raise RuntimeError("remote continuation conflict")
                self.admissions[key] = frozen

            def claim_partial_continuation(self, **value):  # type: ignore[no-untyped-def]
                key = str(value["job_id"])
                if key not in self.admissions or key in self.claimed:
                    return False
                self.claimed.add(key)
                return True

            def handoff_partial_continuation(self, **value):  # type: ignore[no-untyped-def]
                key = str(value["job_id"])
                if key not in self.admissions or key in self.claimed:
                    raise RuntimeError("remote continuation is unavailable")
                self.claimed.add(key)

        coordination = SharedContinuationAuthority()
        authority = AuthoritySnapshotIdentity(
            company_id="company-local",
            company_revision=3,
            roster_revision=5,
            playbook_revision=7,
            action_policy_digest="read-only-policy-fixture",
        )
        work_order = normalize_work_order(
            "Complete the fixture goal",
            work_order_id="work-order-partial-read-only",
            authority_snapshot=authority,
            budget_snapshot=WorkOrderBudgetSnapshot(
                max_model_calls=64,
                max_tool_calls=128,
                max_cost_usd=20.0,
                max_wall_time_ms=5_000,
            ),
            requested_at=datetime.now(timezone.utc),
        )
        class AbortBeforeFinalLedger(_DropTerminalLedger):
            def append_attempt(self, job_id, record) -> None:  # type: ignore[no-untyped-def]
                if record.task_id == "final":
                    raise RuntimeError("fixture interruption before final audit append")
                super().append_attempt(job_id, record)
        request = replace(
            self._retry_request(),
            request_id="request-partial-read-only",
            job_id="job-partial-read-only",
            work_order_id=work_order.work_order_id,
            work_order_digest=work_order.content_digest,
            work_order_authority_digest=authority.identity_digest,
            firm_admission_digest="f" * 64,
            requested_effect="READ",
            graph_mutation_policy="LOCKED",
        )
        store = RunStore()
        with self.assertRaisesRegex(RuntimeError, "interruption"):
            asyncio.run(
                FirmKernel(
                    employee_execution=ScriptedEmployeeExecutionPort(
                        {
                            "analysis": ScriptedOutcome("analysis complete"),
                            "final": ScriptedOutcome("unrecorded final"),
                        }
                    ),
                    active_job_ledger=AbortBeforeFinalLedger(store),
                ).run(request)
            )
        continuation = ActiveJobInspector(
            store,
            company_coordination=coordination,  # type: ignore[arg-type]
        ).authorize_partial_read_only_continuation(
            request.job_id,
            request=request,
            work_order=work_order,
            source_references={"firm_admission_digest": "f" * 64},
        )
        self.assertEqual(
            continuation.continuation_authority,
            "PARTIAL_READ_ONLY_CONTINUATION_ALLOWED",
        )
        self.assertEqual(continuation.completed_task_ids, ("analysis",))
        self.assertEqual(len(continuation.completed_run_ids), 1)
        self.assertEqual(len(continuation.completed_results_digest), 64)
        handoff = ActiveJobInspector(
            store,
            company_coordination=coordination,  # type: ignore[arg-type]
        ).handoff_partial_read_only_continuation(
            request.job_id,
            request=request,
            work_order=work_order,
            source_references={"firm_admission_digest": "f" * 64},
            target_device_id="device-laptop-b",
        )
        self.assertEqual(handoff.completed_run_ids, continuation.completed_run_ids)
        self.assertIn(request.job_id, coordination.claimed)
        with self.assertRaisesRegex(ValueError, "handed off"):
            store.claim_partial_job_continuation(
                job_id=request.job_id,
                request_snapshot_hash="0" * 64,
                graph_digest=continuation.graph_digest,
            )
        with self.assertRaisesRegex(ValueError, "already claimed|conflicts|Terminal"):
            store.authorize_partial_job_continuation(
                job_id=request.job_id,
                request_snapshot_hash="0" * 64,
                graph_digest=continuation.graph_digest,
                completed_run_ids=continuation.completed_run_ids,
                completed_results_digest=continuation.completed_results_digest,
            )
        self.assertEqual(
            store.record_partial_job_continuation_handoff(
                job_id=request.job_id,
                target_device_id="device-laptop-b",
                request_snapshot_hash=store.job_frozen_snapshot_hash(request.job_id),
                graph_digest=continuation.graph_digest,
                completed_results_digest=continuation.completed_results_digest,
            )["target_device_id"],
            "device-laptop-b",
        )
        store.close()

    def test_terminal_and_invalid_recovery_advice_refuse_automatic_execution(self) -> None:
        request = self._retry_request()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.db"
            store = RunStore(path)
            asyncio.run(
                FirmKernel(
                    employee_execution=self._retry_runner(),
                    active_job_ledger=SQLiteActiveJobLedger(store),
                ).run(request)
            )
            terminal = ActiveJobInspector(store).recovery_advice(request.job_id)
            self.assertEqual(terminal.recovery_state, "TERMINAL_NO_RECOVERY")
            self.assertFalse(terminal.requires_new_kernel_attempt)
            store.close()

            with sqlite3.connect(path) as conn:
                conn.execute("DROP TRIGGER job_attempts_no_update")
                conn.execute(
                    "UPDATE job_attempts SET payload_json = '{}' WHERE job_id = ? AND ledger_seq = 1",
                    (request.job_id,),
                )
            reopened = RunStore(path)
            invalid = ActiveJobInspector(reopened).recovery_advice(request.job_id)
            self.assertEqual(
                invalid.recovery_state,
                "AUDIT_INVALID_MANUAL_INVESTIGATION",
            )
            self.assertFalse(invalid.requires_new_kernel_attempt)
            self.assertTrue(
                any("Do not resume" in item for item in invalid.prohibited_actions)
            )
            reopened.close()

    def test_missing_source_is_refused_and_exact_append_reentry_is_idempotent(self) -> None:
        request = self._retry_request()
        store = RunStore()
        asyncio.run(
            FirmKernel(
                employee_execution=self._retry_runner(),
                active_job_ledger=_DropTerminalLedger(store),
            ).run(request)
        )
        rows = store.get_job_ledger_rows(request.job_id)
        self.assertIsNotNone(rows)
        assert rows is not None
        first_attempt = json.loads(rows["attempts"][0]["payload_json"])
        replayed = store.append_job_attempt(request.job_id, first_attempt)
        self.assertEqual(replayed["attempt_id"], first_attempt["attempt_id"])
        changed_attempt = dict(first_attempt)
        changed_attempt["failure_detail"] = "different immutable payload"
        changed_unhashed = dict(changed_attempt)
        changed_unhashed["content_hash"] = ""
        changed_attempt["content_hash"] = content_digest(changed_unhashed)
        with self.assertRaisesRegex(ValueError, "Duplicate task attempt"):
            store.append_job_attempt(request.job_id, changed_attempt)

        mutation = json.loads(rows["mutations"][0]["payload_json"])
        mutation.update(
            event_id="mutation-invalid-source",
            sequence=2,
            source_attempt_id="attempt-missing",
            content_hash="",
        )
        mutation["content_hash"] = content_digest(mutation)
        with self.assertRaisesRegex(ValueError, "source attempt does not exist"):
            store.append_job_mutation(request.job_id, mutation)
        store.close()

    def test_snapshot_and_terminal_reentry_are_exactly_once(self) -> None:
        request = self._retry_request()
        store = RunStore()
        ledger = SQLiteActiveJobLedger(store)
        graph = graph_from_proposal(
            request.plan_proposal,
            max_tasks=request.job_limits.max_tasks,
        )
        frozen = frozen_snapshot_digest(request)
        ledger.start_job(request, graph, frozen)
        # A caller that loses its reply after the durable write can safely
        # repeat the same admission; a changed immutable request remains a
        # conflict rather than silently replacing the audit root.
        ledger.start_job(request, graph, frozen)
        with self.assertRaisesRegex(ValueError, "snapshot already exists"):
            ledger.start_job(replace(request, request_id="different-request"), graph, frozen)

        with self.assertRaisesRegex(ValueError, "explicit same-Job continuation"):
            asyncio.run(
                FirmKernel(
                    employee_execution=self._retry_runner(),
                    active_job_ledger=_DropTerminalLedger(store),
                ).run(request)
            )
        store.close()

    def test_append_only_trigger_and_hash_replay_detect_privileged_tamper(self) -> None:
        request = self._retry_request()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.db"
            store = RunStore(path)
            asyncio.run(
                FirmKernel(
                    employee_execution=self._retry_runner(),
                    active_job_ledger=SQLiteActiveJobLedger(store),
                ).run(request)
            )
            with sqlite3.connect(path) as conn:
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        "UPDATE job_attempts SET payload_json = '{}' WHERE job_id = ?",
                        (request.job_id,),
                    )
            store.close()

            with sqlite3.connect(path) as conn:
                conn.execute("DROP TRIGGER job_attempts_no_update")
                conn.execute(
                    "UPDATE job_attempts SET payload_json = '{}' WHERE job_id = ? AND ledger_seq = 1",
                    (request.job_id,),
                )
            reopened = RunStore(path)
            inspection = ActiveJobInspector(reopened).inspect(request.job_id)
            self.assertEqual(inspection.audit_status, ActiveJobAuditStatus.INVALID)
            self.assertTrue(
                any("attempt payload hash mismatch" in item for item in inspection.errors)
            )
            reopened.close()

    def test_runtime_schema_v1_is_migrated_additively_to_current_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.db"
            store = RunStore(path)
            store.close()
            with sqlite3.connect(path) as conn:
                conn.execute(
                    "UPDATE runtime_meta SET value = '1' WHERE key = 'schema_version'"
                )
            migrated = RunStore(path)
            with sqlite3.connect(path) as conn:
                version = conn.execute(
                    "SELECT value FROM runtime_meta WHERE key = 'schema_version'"
                ).fetchone()[0]
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
            self.assertEqual(version, str(SCHEMA_VERSION))
            self.assertTrue(
                {
                    "job_snapshots",
                    "job_attempts",
                    "job_mutations",
                    "job_terminal_events",
                    "company_budget_leases",
                    "company_budget_forfeits",
                    "company_budget_incidents",
                    "company_budget_pause_state",
                    "employee_session_state",
                    "employee_session_leases",
                    "effect_resource_leases",
                    "local_resume_envelopes",
                }
                .issubset(tables)
            )
            migrated.close()

    def test_runtime_schema_v15_migrates_to_continuation_receipt_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.db"
            store = RunStore(path)
            store.close()
            with sqlite3.connect(path) as conn:
                conn.execute(
                    "UPDATE runtime_meta SET value = '15' WHERE key = 'schema_version'"
                )
            migrated = RunStore(path)
            self.assertEqual(migrated.schema_version(), SCHEMA_VERSION)
            with sqlite3.connect(path) as conn:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
            self.assertIn("same_job_continuation_admissions", tables)
            migrated.close()

    def test_empty_snapshot_is_interrupted(self) -> None:
        request = self._retry_request()
        store = RunStore()
        ledger = SQLiteActiveJobLedger(store)
        graph = graph_from_proposal(request.plan_proposal, max_tasks=16)
        ledger.start_job(request, graph, frozen_snapshot_digest(request))

        inspection = ActiveJobInspector(store).inspect(request.job_id)

        self.assertEqual(inspection.audit_status, ActiveJobAuditStatus.INTERRUPTED)
        self.assertEqual(inspection.attempt_count, 0)
        self.assertEqual(inspection.mutation_count, 0)
        store.close()

    def test_durable_lifecycle_records_admission_and_terminalization(self) -> None:
        request = self._retry_request()
        store = RunStore()
        result = asyncio.run(
            FirmKernel(
                employee_execution=self._retry_runner(),
                active_job_ledger=SQLiteActiveJobLedger(store),
            ).run(request)
        )
        lifecycle = store.get_job_lifecycle(request.job_id)
        assert lifecycle is not None

        self.assertEqual(result.status.value, "SUCCEEDED")
        self.assertEqual(lifecycle["state"], "TERMINAL")
        self.assertEqual(lifecycle["revision"], 2)
        self.assertEqual(
            tuple(event["operation"] for event in lifecycle["events"]),
            ("ADMIT", "TERMINALIZE"),
        )
        store.close()

    def test_operator_lifecycle_transition_is_cas_protected(self) -> None:
        request = self._retry_request()
        store = RunStore()
        ledger = SQLiteActiveJobLedger(store)
        ledger.start_job(
            request,
            graph_from_proposal(request.plan_proposal, max_tasks=request.job_limits.max_tasks),
            frozen_snapshot_digest(request),
        )
        deferred = store.transition_job_lifecycle(
            job_id=request.job_id,
            operation="DEFER",
            reason="WAITING_FOR_CAPABILITY",
            expected_revision=1,
        )
        self.assertEqual(deferred["state"], "DEFERRED")
        paused = store.transition_job_lifecycle(
            job_id=request.job_id,
            operation="PAUSE",
            reason="OPERATOR_REVIEW",
            expected_revision=2,
        )
        self.assertEqual(paused["state"], "PAUSED")
        with self.assertRaisesRegex(ValueError, "revision conflicts"):
            store.transition_job_lifecycle(
                job_id=request.job_id,
                operation="RESUME",
                reason="STALE",
                expected_revision=2,
            )
        resumed = store.transition_job_lifecycle(
            job_id=request.job_id,
            operation="RESUME",
            reason="OPERATOR_RESUME",
            expected_revision=3,
        )
        self.assertEqual(resumed["state"], "ADMITTED")
        cancelled = store.transition_job_lifecycle(
            job_id=request.job_id,
            operation="CANCEL",
            reason="USER_CANCELLED",
            expected_revision=4,
        )
        self.assertEqual(cancelled["state"], "CANCELLED")
        store.close()

    def test_cli_job_control_requires_confirmation_and_records_hold(self) -> None:
        request = self._retry_request()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.db"
            store = RunStore(path)
            SQLiteActiveJobLedger(store).start_job(
                request,
                graph_from_proposal(
                    request.plan_proposal,
                    max_tasks=request.job_limits.max_tasks,
                ),
                frozen_snapshot_digest(request),
            )
            store.close()
            errors = io.StringIO()
            denied = main(
                ["job", "control", request.job_id, "pause", "--reason", "OPERATOR_HOLD", "--state", str(path)],
                stdout=io.StringIO(),
                stderr=errors,
            )
            self.assertNotEqual(denied, 0)
            output = io.StringIO()
            accepted = main(
                [
                    "job", "control", request.job_id, "pause",
                    "--reason", "OPERATOR_HOLD", "--revision", "1",
                    "--confirm", "--state", str(path), "--json",
                ],
                stdout=output,
                stderr=io.StringIO(),
            )
            self.assertEqual(accepted, EXIT_OK)
            self.assertEqual(json.loads(output.getvalue())["state"], "PAUSED")
            settlement_output = io.StringIO()
            settlement = main(
                [
                    "job", "settle-unknown", request.job_id,
                    "--reason", "WORKER_EXITED", "--confirm",
                    "--state", str(path), "--json",
                ],
                stdout=settlement_output,
                stderr=io.StringIO(),
            )
            self.assertEqual(settlement, EXIT_OK)
            self.assertEqual(
                json.loads(settlement_output.getvalue())["reusable_capacity"],
                False,
            )

    def test_graph_mutation_lease_is_durable_idempotent_and_hard_capped(self) -> None:
        request = self._retry_request()
        store = RunStore()
        ledger = SQLiteActiveJobLedger(store)
        ledger.start_job(
            request,
            graph_from_proposal(request.plan_proposal, max_tasks=request.job_limits.max_tasks),
            frozen_snapshot_digest(request),
        )
        first = store.reserve_job_lifecycle_lease(
            job_id=request.job_id,
            lease_id="graph-patch-event-1",
            lease={"model_calls": 1, "tool_calls": 2, "cost_usd": 0.25},
            reason="GRAPH_PATCH_1",
        )
        replay = store.reserve_job_lifecycle_lease(
            job_id=request.job_id,
            lease_id="graph-patch-event-1",
            lease={"model_calls": 1, "tool_calls": 2, "cost_usd": 0.25},
            reason="GRAPH_PATCH_1",
        )
        self.assertEqual(first["lease_id"], replay["lease_id"])
        store.release_job_lifecycle_lease(
            job_id=request.job_id,
            lease_id="graph-patch-event-1",
            reason="GRAPH_PATCH_AUDIT_REJECTED",
        )
        released = store.get_job_lifecycle(request.job_id)
        assert released is not None
        self.assertEqual(released["leases"][0]["status"], "RELEASED")
        store.reserve_job_lifecycle_lease(
            job_id=request.job_id,
            lease_id="graph-patch-event-3",
            lease={"model_calls": 1, "tool_calls": 2, "cost_usd": 0.25},
            reason="GRAPH_PATCH_3",
        )
        with self.assertRaisesRegex(ValueError, "hard cap"):
            store.reserve_job_lifecycle_lease(
                job_id=request.job_id,
                lease_id="graph-patch-event-2",
                lease={"model_calls": 64, "tool_calls": 0, "cost_usd": 0.0},
                reason="GRAPH_PATCH_2",
            )
        store.transition_job_lifecycle(
            job_id=request.job_id,
            operation="TERMINALIZE",
            reason="JOB_SUCCEEDED",
        )
        store.settle_job_lifecycle_leases(
            job_id=request.job_id,
            reason="JOB_SUCCEEDED",
        )
        lifecycle = store.get_job_lifecycle(request.job_id)
        assert lifecycle is not None
        self.assertEqual(lifecycle["leases"][0]["status"], "RELEASED")
        self.assertEqual(lifecycle["leases"][1]["status"], "SETTLED")
        store.close()

    def test_unknown_interrupted_mutation_capacity_is_forfeited_not_reused(self) -> None:
        request = self._retry_request()
        store = RunStore()
        ledger = SQLiteActiveJobLedger(store)
        ledger.start_job(
            request,
            graph_from_proposal(request.plan_proposal, max_tasks=request.job_limits.max_tasks),
            frozen_snapshot_digest(request),
        )
        store.reserve_job_lifecycle_lease(
            job_id=request.job_id,
            lease_id="unknown-usage-patch",
            lease={"model_calls": 64, "tool_calls": 0, "cost_usd": 0.0},
            reason="GRAPH_PATCH_1",
        )
        store.transition_job_lifecycle(
            job_id=request.job_id,
            operation="PAUSE",
            reason="INTERRUPTED_PROCESS",
        )
        self.assertEqual(
            store.forfeit_interrupted_job_lifecycle_leases(
                job_id=request.job_id,
                reason="WORKER_EXITED_WITHOUT_TERMINAL_RECEIPT",
            ),
            1,
        )
        store.transition_job_lifecycle(
            job_id=request.job_id,
            operation="RESUME",
            reason="OPERATOR_REVIEW_COMPLETED",
        )
        with self.assertRaisesRegex(ValueError, "hard cap"):
            store.reserve_job_lifecycle_lease(
                job_id=request.job_id,
                lease_id="later-patch",
                lease={"model_calls": 1, "tool_calls": 0, "cost_usd": 0.0},
                reason="GRAPH_PATCH_2",
            )
        store.close()

    def test_user_correction_is_durable_and_consumed_once_at_task_boundary(self) -> None:
        request = self._retry_request()
        store = RunStore()
        ledger = SQLiteActiveJobLedger(store)
        ledger.start_job(
            request,
            graph_from_proposal(request.plan_proposal, max_tasks=request.job_limits.max_tasks),
            frozen_snapshot_digest(request),
        )
        submitted = store.submit_job_user_correction(
            job_id=request.job_id,
            target_task_id="analysis",
            reference="decision-ledger:pricing-correction-v2",
        )
        self.assertEqual(submitted["status"], "PENDING")
        signals = ledger.consume_operator_signals(
            job_id=request.job_id,
            task_id="analysis",
        )
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].code, SignalCode.USER_CORRECTION)
        self.assertEqual(signals[0].value, "decision-ledger:pricing-correction-v2")
        self.assertEqual(
            ledger.consume_operator_signals(job_id=request.job_id, task_id="analysis"),
            (),
        )
        store.close()

    def test_continuation_preflight_refusal_is_append_only_and_inspectable(self) -> None:
        request = self._retry_request()
        store = RunStore()
        ledger = SQLiteActiveJobLedger(store)
        ledger.start_job(
            request,
            graph_from_proposal(request.plan_proposal, max_tasks=request.job_limits.max_tasks),
            frozen_snapshot_digest(request),
        )
        receipt = store.append_job_continuation_preflight_refusal(
            job_id=request.job_id,
            continuation_kind="READ_ONLY_PARTIAL",
            code="CAPABILITY_MANIFEST_MISMATCH",
        )
        self.assertEqual(
            store.append_job_continuation_preflight_refusal(
                job_id=request.job_id,
                continuation_kind="READ_ONLY_PARTIAL",
                code="CAPABILITY_MANIFEST_MISMATCH",
            )["receipt_id"],
            receipt["receipt_id"],
        )
        inspection = ActiveJobInspector(store).inspect(request.job_id)
        self.assertEqual(inspection.audit_status, ActiveJobAuditStatus.INTERRUPTED)
        self.assertEqual(len(inspection.continuation_preflight_receipts), 1)
        self.assertEqual(
            inspection.continuation_preflight_receipts[0]["code"],
            "CAPABILITY_MANIFEST_MISMATCH",
        )
        with self.assertRaisesRegex(Exception, "append-only"):
            store._conn.execute(
                "DELETE FROM job_continuation_preflight_receipts WHERE receipt_id = ?",
                (receipt["receipt_id"],),
            )
        store.close()

    def test_existing_paused_job_cannot_enter_through_ordinary_kernel_run(self) -> None:
        request = self._retry_request()
        store = RunStore()
        ledger = SQLiteActiveJobLedger(store)
        graph = graph_from_proposal(request.plan_proposal, max_tasks=request.job_limits.max_tasks)
        ledger.start_job(request, graph, frozen_snapshot_digest(request))
        store.transition_job_lifecycle(
            job_id=request.job_id,
            operation="PAUSE",
            reason="OPERATOR_HOLD",
            expected_revision=1,
        )
        runner = self._retry_runner()
        with self.assertRaisesRegex(ValueError, "explicit same-Job continuation"):
            asyncio.run(
                FirmKernel(employee_execution=runner, active_job_ledger=ledger).run(request)
            )
        self.assertEqual(runner.requests, [])
        store.close()

    def test_job_inspection_projects_live_waiting_approval_without_request_content(self) -> None:
        redaction_marker = "SECRET-SENTINEL-LIVE-APPROVAL-PROJECTION"
        company_request = self._retry_request()
        store = RunStore()
        graph = graph_from_proposal(
            company_request.plan_proposal,
            max_tasks=company_request.job_limits.max_tasks,
        )
        SQLiteActiveJobLedger(store).start_job(
            company_request,
            graph,
            frozen_snapshot_digest(company_request),
        )
        employee_request = _request("live-approval-projection")
        employee_request = replace(
            employee_request,
            task=replace(
                employee_request.task,
                job_id=company_request.job_id,
                task_id="analysis",
            ),
            context=replace(
                employee_request.context,
                company_policy_excerpt=redaction_marker,
            ),
        )
        handle, _, _ = _stage_waiting_approval(store, employee_request)

        inspection = ActiveJobInspector(store).inspect(company_request.job_id)
        recovery = ActiveJobInspector(store).recovery_advice(company_request.job_id)
        projected = inspection.runtime_runs
        rendered = json.dumps(to_primitive(inspection), ensure_ascii=False)

        self.assertEqual(inspection.audit_status, ActiveJobAuditStatus.INTERRUPTED)
        self.assertEqual(len(projected), 1)
        self.assertEqual(projected[0].run_id, handle.run_id)
        self.assertEqual(projected[0].task_id, "analysis")
        self.assertEqual(projected[0].status, RunStatus.WAITING_APPROVAL.value)
        self.assertEqual(projected[0].pending_approval_count, 1)
        self.assertEqual(len(inspection.tool_receipts), 1)
        self.assertEqual(inspection.tool_receipts[0]["effect"], "UNKNOWN")
        self.assertEqual(inspection.tool_receipts[0]["status"], "INTENT_RECORDED")
        self.assertEqual(
            recovery.recovery_state,
            "INTERRUPTED_NEW_KERNEL_ATTEMPT_REQUIRED",
        )
        self.assertTrue(recovery.requires_new_kernel_attempt)
        self.assertEqual(recovery.runtime_run_statuses, (RunStatus.WAITING_APPROVAL.value,))
        self.assertIsNotNone(recovery.interruption_evidence)
        assert recovery.interruption_evidence is not None
        self.assertEqual(recovery.interruption_evidence.nonterminal_runtime_run_count, 1)
        self.assertTrue(
            any("interrupted evidence" in item for item in recovery.recommended_actions)
        )
        self.assertTrue(
            any("Do not resolve an approval" in item for item in recovery.prohibited_actions)
        )
        self.assertNotIn(redaction_marker, rendered)
        self.assertNotIn("workspace:repo:src/app.py", rendered)
        store.close()

    def test_recovery_advice_projects_timeout_as_replacement_only(self) -> None:
        company_request = self._retry_request()
        store = RunStore()
        SQLiteActiveJobLedger(store).start_job(
            company_request,
            graph_from_proposal(
                company_request.plan_proposal,
                max_tasks=company_request.job_limits.max_tasks,
            ),
            frozen_snapshot_digest(company_request),
        )
        base_employee_request = _request("timeout-recovery")
        employee_request = replace(
            base_employee_request,
            task=replace(
                base_employee_request.task,
                job_id=company_request.job_id,
                task_id="analysis",
            ),
        )
        handle, _ = store.create_run(employee_request)
        store.begin_run(handle.run_id)
        timed_out = EmployeeRunResult(
            run_id=handle.run_id,
            request_id=handle.request_id,
            job_id=employee_request.task.job_id,
            task_id=employee_request.task.task_id,
            employee_id=employee_request.employee.employee_id,
            status=RunStatus.BUDGET_EXHAUSTED,
            summary="Run limit reached: max_wall_time_ms",
            output_artifact_refs=(),
            acceptance_evidence=(),
            unresolved_issues=("Replacement decision required.",),
            observations=(),
            suggested_followups=(),
            signals=(),
            partial_result=True,
            usage=Usage(),
            last_event_seq=0,
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
            failure=Failure(
                "RUN_BUDGET_EXHAUSTED",
                FailureCategory.TIMEOUT,
                "Run limit reached: max_wall_time_ms",
                retryable=True,
                origin="employee-foundation",
            ),
        )
        store.terminalize(timed_out, EventType.RUN_BUDGET_EXHAUSTED, {})

        advice = ActiveJobInspector(store).recovery_advice(company_request.job_id)

        self.assertEqual(
            advice.recovery_state,
            "INTERRUPTED_TIMEOUT_REPLACEMENT_REQUIRED",
        )
        self.assertIsNotNone(advice.interruption_evidence)
        assert advice.interruption_evidence is not None
        self.assertEqual(advice.interruption_evidence.timeout_terminal_run_count, 1)
        self.assertTrue(
            any("timeout" in item for item in advice.recommended_actions)
        )
        store.close()

    def test_recovery_advice_projects_provider_cancellation_as_replacement_only(self) -> None:
        company_request = self._retry_request()
        store = RunStore()
        SQLiteActiveJobLedger(store).start_job(
            company_request,
            graph_from_proposal(
                company_request.plan_proposal,
                max_tasks=company_request.job_limits.max_tasks,
            ),
            frozen_snapshot_digest(company_request),
        )
        base_employee_request = _request("provider-cancellation-recovery")
        employee_request = replace(
            base_employee_request,
            task=replace(
                base_employee_request.task,
                job_id=company_request.job_id,
                task_id="analysis",
            ),
        )
        handle, _ = store.create_run(employee_request)
        store.begin_run(handle.run_id)
        store.append_event(
            handle.run_id,
            EventType.MODEL_CALL_CANCELLED,
            {"call_index": 1, "provider_request_id": "provider-request-opaque"},
        )
        cancelled = EmployeeRunResult(
            run_id=handle.run_id,
            request_id=handle.request_id,
            job_id=employee_request.task.job_id,
            task_id=employee_request.task.task_id,
            employee_id=employee_request.employee.employee_id,
            status=RunStatus.CANCELLED,
            summary="Cancelled after provider request was observed.",
            output_artifact_refs=(),
            acceptance_evidence=(),
            unresolved_issues=("Replacement decision required.",),
            observations=(),
            suggested_followups=(),
            signals=(),
            partial_result=True,
            usage=Usage(),
            last_event_seq=0,
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
            failure=Failure(
                "RUN_CANCELLED",
                FailureCategory.CANCEL,
                "Cancelled by operator.",
                retryable=True,
                origin="test",
            ),
        )
        store.terminalize(cancelled, EventType.RUN_CANCELLED, {})

        advice = ActiveJobInspector(store).recovery_advice(company_request.job_id)
        rendered = json.dumps(to_primitive(advice), ensure_ascii=False)

        self.assertEqual(
            advice.recovery_state,
            "INTERRUPTED_PROVIDER_CANCELLATION_REPLACEMENT_REQUIRED",
        )
        self.assertTrue(advice.requires_new_kernel_attempt)
        self.assertIsNotNone(advice.interruption_evidence)
        assert advice.interruption_evidence is not None
        self.assertEqual(
            advice.interruption_evidence.provider_cancellation_receipt_count,
            1,
        )
        self.assertEqual(
            advice.interruption_evidence.timeout_terminal_run_count,
            0,
        )
        self.assertNotIn("provider-request-opaque", rendered)
        self.assertTrue(
            any("zero usage" in item for item in advice.prohibited_actions)
        )
        store.close()

    def test_job_inspect_cli_projects_live_approval_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.db"
            company_request = self._retry_request()
            store = RunStore(path)
            SQLiteActiveJobLedger(store).start_job(
                company_request,
                graph_from_proposal(
                    company_request.plan_proposal,
                    max_tasks=company_request.job_limits.max_tasks,
                ),
                frozen_snapshot_digest(company_request),
            )
            employee_request = _request("live-approval-cli-projection")
            employee_request = replace(
                employee_request,
                task=replace(
                    employee_request.task,
                    job_id=company_request.job_id,
                    task_id="analysis",
                ),
            )
            handle, _, _ = _stage_waiting_approval(store, employee_request)
            before = ActiveJobInspector(store).inspect(company_request.job_id)
            store.close()

            output = io.StringIO()
            errors = io.StringIO()
            exit_code = main(
                [
                    "job",
                    "inspect",
                    company_request.job_id,
                    "--state",
                    str(path),
                    "--json",
                ],
                stdout=output,
                stderr=errors,
            )
            payload = json.loads(output.getvalue())

            self.assertEqual(exit_code, EXIT_OK, errors.getvalue())
            self.assertEqual(payload["runtime_runs"][0]["run_id"], handle.run_id)
            self.assertEqual(payload["runtime_runs"][0]["status"], "WAITING_APPROVAL")
            self.assertEqual(payload["runtime_runs"][0]["pending_approval_count"], 1)
            reopened = RunStore(path)
            self.assertEqual(
                ActiveJobInspector(reopened).inspect(company_request.job_id),
                before,
            )
            reopened.close()

    def test_job_list_and_inspect_cli_are_read_only(self) -> None:
        request = self._retry_request()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.db"
            store = RunStore(path)
            asyncio.run(
                FirmKernel(
                    employee_execution=self._retry_runner(),
                    active_job_ledger=SQLiteActiveJobLedger(store),
                ).run(request)
            )
            before = ActiveJobInspector(store).inspect(request.job_id)
            store.close()

            listed = io.StringIO()
            inspected = io.StringIO()
            timeline = io.StringIO()
            recovery = io.StringIO()
            errors = io.StringIO()
            list_code = main(
                ["job", "list", "--state", str(path), "--json"],
                stdout=listed,
                stderr=errors,
            )
            inspect_code = main(
                ["job", "inspect", request.job_id, "--state", str(path), "--json"],
                stdout=inspected,
                stderr=errors,
            )
            timeline_code = main(
                [
                    "job",
                    "timeline",
                    request.job_id,
                    "--state",
                    str(path),
                    "--limit",
                    "1",
                    "--json",
                ],
                stdout=timeline,
                stderr=errors,
            )
            recovery_code = main(
                [
                    "job",
                    "recovery",
                    request.job_id,
                    "--state",
                    str(path),
                    "--json",
                ],
                stdout=recovery,
                stderr=errors,
            )

            self.assertEqual(list_code, EXIT_OK, errors.getvalue())
            self.assertEqual(inspect_code, EXIT_OK, errors.getvalue())
            self.assertEqual(timeline_code, EXIT_OK, errors.getvalue())
            self.assertEqual(recovery_code, EXIT_OK, errors.getvalue())
            self.assertEqual(json.loads(listed.getvalue())[0]["job_id"], request.job_id)
            self.assertEqual(
                json.loads(inspected.getvalue())["audit_status"],
                "TERMINAL",
            )
            timeline_payload = json.loads(timeline.getvalue())
            self.assertEqual(timeline_payload["job_id"], request.job_id)
            self.assertEqual(timeline_payload["event_limit"], 1)
            self.assertEqual(timeline_payload["event_count"], 0)
            self.assertFalse(timeline_payload["truncated"])
            recovery_payload = json.loads(recovery.getvalue())
            self.assertEqual(recovery_payload["recovery_state"], "TERMINAL_NO_RECOVERY")
            self.assertFalse(recovery_payload["requires_new_kernel_attempt"])
            reopened = RunStore(path)
            self.assertEqual(
                ActiveJobInspector(reopened).inspect(request.job_id),
                before,
            )
            reopened.close()

    def test_job_timeline_is_window_bounded_and_excludes_event_payloads(self) -> None:
        redaction_marker = "TIMELINE-SECRET-SENTINEL-DO-NOT-RENDER"
        request = self._retry_request()
        request = replace(
            request,
            goal=redaction_marker,
            plan_proposal=replace(request.plan_proposal, goal=redaction_marker),
        )
        store = RunStore()
        SQLiteActiveJobLedger(store).start_job(
            request,
            graph_from_proposal(request.plan_proposal, max_tasks=request.job_limits.max_tasks),
            frozen_snapshot_digest(request),
        )
        employee_request = _request("timeline-window-projection")
        employee_request = replace(
            employee_request,
            task=replace(
                employee_request.task,
                job_id=request.job_id,
                task_id="analysis",
            ),
            context=replace(employee_request.context, company_policy_excerpt=redaction_marker),
        )
        _stage_waiting_approval(store, employee_request)
        now = datetime.now(timezone.utc)
        timeline = ActiveJobInspector(store).timeline(
            request.job_id,
            from_at=now - timedelta(days=90),
            to_at=now,
            limit=1,
            now=now,
        )
        rendered = json.dumps(to_primitive(timeline), ensure_ascii=False)

        self.assertTrue(timeline.window_capped)
        self.assertEqual(timeline.event_limit, 1)
        self.assertEqual(timeline.event_count, 1)
        self.assertTrue(timeline.truncated)
        self.assertGreaterEqual(timeline.runtime_run_count, 1)
        self.assertNotIn(redaction_marker, rendered)
        self.assertNotIn("payload", rendered)
        store.close()


if __name__ == "__main__":
    unittest.main()
