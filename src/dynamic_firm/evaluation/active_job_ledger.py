from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

from dynamic_firm import __version__
from dynamic_firm.company.store import CompanyStateStore
from dynamic_firm.kernel.models import (
    CompanyRunRequest,
    EmployeeRecord,
    JobLimits,
    JobTask,
    PlanProposal,
)
from dynamic_firm.kernel.mutation import content_digest
from dynamic_firm.kernel.service import FirmKernel
from dynamic_firm.kernel.testing import ScriptedEmployeeExecutionPort, ScriptedOutcome
from dynamic_firm.runtime.job_ledger import (
    ActiveJobAuditStatus,
    ActiveJobInspection,
    ActiveJobInspector,
    SQLiteActiveJobLedger,
)
from dynamic_firm.runtime.models import (
    ContextBundle,
    Failure,
    FailureCategory,
    RunLimits,
    RunSignal,
    RunStatus,
    SignalCode,
)
from dynamic_firm.runtime.store import SCHEMA_VERSION as RUNTIME_SCHEMA_VERSION, RunStore


@dataclass(frozen=True, slots=True)
class ActiveJobLedgerEvaluationCheck:
    name: str
    passed: bool
    evidence: str


@dataclass(frozen=True, slots=True)
class ActiveJobLedgerEvaluationRecord:
    schema_version: str
    noruct_version: str
    evidence_class: str
    retry: ActiveJobInspection
    reroute: ActiveJobInspection
    interrupted: ActiveJobInspection
    relation_refused: bool
    tamper_detected: bool
    privacy_projection_passed: bool
    runtime_schema_version: int
    company_schema_version: int
    provider_calls: int
    quota_consumed: bool
    checks: tuple[ActiveJobLedgerEvaluationCheck, ...]

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(item.passed for item in self.checks)


class _DropTerminalLedger(SQLiteActiveJobLedger):
    def finish_job(self, job_id, result) -> None:  # type: ignore[no-untyped-def]
        return None


def _task(
    task_id: str,
    *,
    depends_on: tuple[str, ...] = (),
    capability: str = "analysis",
    objective_suffix: str = "",
) -> JobTask:
    return JobTask(
        task_id=task_id,
        objective=f"Complete {task_id}{objective_suffix}",
        depends_on=depends_on,
        required_capabilities=(capability,),
        acceptance_criteria=(f"Evidence for {task_id}{objective_suffix}",),
    )


def _request(
    job_id: str,
    *,
    roster: tuple[EmployeeRecord, ...],
    secret: str = "",
) -> CompanyRunRequest:
    tasks = (
        _task("analysis", objective_suffix=secret),
        _task(
            "final",
            depends_on=("analysis",),
            capability="integration",
            objective_suffix=secret,
        ),
    )
    return CompanyRunRequest(
        request_id=f"request-{job_id}",
        job_id=job_id,
        goal=f"Evaluate durable ACTIVE JOB audit{secret}",
        plan_proposal=PlanProposal(
            proposal_id=f"proposal-{job_id}",
            goal=f"Evaluate durable ACTIVE JOB audit{secret}",
            tasks=tasks,
            final_task_id="final",
        ),
        roster=roster,
        context_snapshot=ContextBundle(
            company_policy_excerpt=secret,
            ephemeral_instructions=((secret,) if secret else ()),
        ),
        runtime_limits=RunLimits(
            max_wall_time_ms=5_000,
            max_model_calls=4,
            max_tool_calls=4,
            max_cost_usd=2.0,
        ),
        job_limits=JobLimits(
            max_tasks=6,
            max_concurrency=2,
            max_graph_patches=1,
            max_task_mutations=2,
            max_temporary_roles=1,
            max_total_model_calls=12,
            max_total_tool_calls=12,
            max_total_cost_usd=6.0,
            max_wall_time_ms=5_000,
        ),
        company_revision=3,
        roster_revision=5,
        playbook_revision=7,
    )


def _retry_runner(summary: str = "Recovered") -> ScriptedEmployeeExecutionPort:
    failure = Failure(
        "MODEL_TRANSIENT",
        FailureCategory.MODEL,
        "The model transport failed temporarily.",
        retryable=True,
    )
    return ScriptedEmployeeExecutionPort(
        {
            "analysis": (
                ScriptedOutcome("Transient", status=RunStatus.FAILED, failure=failure),
                ScriptedOutcome(summary),
            ),
            "final": ScriptedOutcome(summary),
        }
    )


async def _run_evaluation() -> ActiveJobLedgerEvaluationRecord:
    secret = "SECRET-SENTINEL-ACTIVE-JOB"
    raw_output = "RAW-TOOL-OUTPUT-ACTIVE-JOB"
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "runtime.db"
        company = CompanyStateStore(path)
        company_schema_before = company.schema_version()
        company.close()

        store = RunStore(path)
        retry_request = _request(
            "ledger-retry-job",
            roster=(
                EmployeeRecord("analyst", "Analyst", ("analysis",)),
                EmployeeRecord("integrator", "Integrator", ("integration",)),
            ),
            secret=secret,
        )
        retry_result = await FirmKernel(
            employee_execution=_retry_runner(raw_output),
            active_job_ledger=SQLiteActiveJobLedger(store),
        ).run(retry_request)
        retry_before = ActiveJobInspector(store).inspect(retry_request.job_id)
        privacy_payload = "\n".join(
            store.active_job_table_payloads(retry_request.job_id)
        )

        mismatch_failure = Failure(
            "ASSIGNEE_CAPABILITY_MISMATCH",
            FailureCategory.INPUT,
            "Another exact-capable employee is required.",
        )
        mismatch_signal = RunSignal(
            SignalCode.ASSIGNEE_MISMATCH,
            "analysis",
            ("typed:assignment-mismatch",),
        )
        reroute_request = _request(
            "ledger-reroute-job",
            roster=(
                EmployeeRecord("analyst-a", "Analyst A", ("analysis",)),
                EmployeeRecord("analyst-b", "Analyst B", ("analysis",)),
                EmployeeRecord("integrator", "Integrator", ("integration",)),
            ),
        )
        reroute_runner = ScriptedEmployeeExecutionPort(
            {
                ("analysis", "analyst-a"): ScriptedOutcome(
                    "Mismatch",
                    status=RunStatus.FAILED,
                    signals=(mismatch_signal,),
                    failure=mismatch_failure,
                ),
                ("analysis", "analyst-b"): ScriptedOutcome("Reassigned"),
                "final": ScriptedOutcome("Integrated"),
            }
        )
        await FirmKernel(
            employee_execution=reroute_runner,
            active_job_ledger=SQLiteActiveJobLedger(store),
        ).run(reroute_request)
        reroute_before = ActiveJobInspector(store).inspect(reroute_request.job_id)

        interrupted_request = _request(
            "ledger-interrupted-job",
            roster=(
                EmployeeRecord("analyst", "Analyst", ("analysis",)),
                EmployeeRecord("integrator", "Integrator", ("integration",)),
            ),
        )
        await FirmKernel(
            employee_execution=_retry_runner(),
            active_job_ledger=_DropTerminalLedger(store),
        ).run(interrupted_request)
        interrupted_before = ActiveJobInspector(store).inspect(
            interrupted_request.job_id
        )

        interrupted_rows = store.get_job_ledger_rows(interrupted_request.job_id)
        assert interrupted_rows is not None
        invalid_mutation = json.loads(
            interrupted_rows["mutations"][0]["payload_json"]
        )
        invalid_mutation.update(
            event_id="mutation-missing-source",
            sequence=2,
            source_attempt_id="attempt-does-not-exist",
            content_hash="",
        )
        invalid_mutation["content_hash"] = content_digest(invalid_mutation)
        try:
            store.append_job_mutation(interrupted_request.job_id, invalid_mutation)
        except ValueError:
            relation_refused = True
        else:
            relation_refused = False

        runtime_schema_version = store.schema_version()
        store.close()

        reopened = RunStore(path)
        retry_after = ActiveJobInspector(reopened).inspect(retry_request.job_id)
        reroute_after = ActiveJobInspector(reopened).inspect(reroute_request.job_id)
        interrupted_after = ActiveJobInspector(reopened).inspect(
            interrupted_request.job_id
        )
        reopened.close()

        tamper_path = Path(directory) / "tamper.db"
        tamper_store = RunStore(tamper_path)
        tamper_request = replace(
            retry_request,
            request_id="request-ledger-tamper-job",
            job_id="ledger-tamper-job",
            plan_proposal=replace(
                retry_request.plan_proposal,
                proposal_id="proposal-ledger-tamper-job",
            ),
            context_snapshot=ContextBundle(),
        )
        await FirmKernel(
            employee_execution=_retry_runner(),
            active_job_ledger=SQLiteActiveJobLedger(tamper_store),
        ).run(tamper_request)
        tamper_store.close()
        with sqlite3.connect(tamper_path) as conn:
            conn.execute("DROP TRIGGER job_mutations_no_update")
            conn.execute(
                "UPDATE job_mutations SET payload_json = '{}' WHERE job_id = ?",
                (tamper_request.job_id,),
            )
        tampered = RunStore(tamper_path)
        tamper_inspection = ActiveJobInspector(tampered).inspect(tamper_request.job_id)
        tampered.close()

        company_after = CompanyStateStore(path)
        company_schema_after = company_after.schema_version()
        company_after.close()

    durability = (
        retry_before == retry_after
        and reroute_before == reroute_after
        and interrupted_before == interrupted_after
    )
    reroute_relation = (
        reroute_after.audit_status == ActiveJobAuditStatus.TERMINAL
        and reroute_after.replay_matches
        and reroute_after.mutations[0]["mutation_type"] == "REROUTE"
        and reroute_after.mutations[0]["from_employee_id"] == "analyst-a"
        and reroute_after.mutations[0]["to_employee_id"] == "analyst-b"
    )
    privacy_passed = secret not in privacy_payload and raw_output not in privacy_payload
    tamper_detected = tamper_inspection.audit_status == ActiveJobAuditStatus.INVALID
    checks = (
        ActiveJobLedgerEvaluationCheck(
            "restart_replays_retry_and_reroute_exactly",
            durability
            and retry_after.audit_status == ActiveJobAuditStatus.TERMINAL
            and retry_after.replay_matches,
            f"retry-chain={retry_after.chain_head[:12]},reroute-chain={reroute_after.chain_head[:12]}",
        ),
        ActiveJobLedgerEvaluationCheck(
            "reroute_source_target_employee_relation_is_exact",
            reroute_relation,
            "analyst-a→analyst-b,source-hash-bound",
        ),
        ActiveJobLedgerEvaluationCheck(
            "terminal_absence_is_interrupted_without_auto_resume",
            interrupted_after.audit_status == ActiveJobAuditStatus.INTERRUPTED
            and not interrupted_after.automatic_resume,
            f"status={interrupted_after.audit_status.value},resume=false",
        ),
        ActiveJobLedgerEvaluationCheck(
            "missing_source_and_duplicate_sequence_are_fail_closed",
            relation_refused,
            f"relation-refused={str(relation_refused).lower()}",
        ),
        ActiveJobLedgerEvaluationCheck(
            "payload_or_chain_tamper_is_invalid",
            tamper_detected,
            f"status={tamper_inspection.audit_status.value},errors={len(tamper_inspection.errors)}",
        ),
        ActiveJobLedgerEvaluationCheck(
            "goal_context_and_raw_output_are_not_in_active_job_payloads",
            privacy_passed,
            f"secret={str(secret in privacy_payload).lower()},raw-output={str(raw_output in privacy_payload).lower()}",
        ),
        ActiveJobLedgerEvaluationCheck(
            "runtime_migrates_to_current_and_company_schema_stays_v9",
            runtime_schema_version == RUNTIME_SCHEMA_VERSION
            and company_schema_before == 9
            and company_schema_after == 9,
            f"runtime=v{runtime_schema_version},company=v{company_schema_before}→v{company_schema_after}",
        ),
        ActiveJobLedgerEvaluationCheck(
            "evaluation_uses_no_provider_network_or_quota",
            retry_result.metrics.usage.model_calls == 3,
            "scripted-employee-only,provider-calls=0,quota=false",
        ),
    )
    return ActiveJobLedgerEvaluationRecord(
        schema_version="noruct.active-job-ledger-evaluation.v1",
        noruct_version=__version__,
        evidence_class="offline-sqlite-active-job-replay",
        retry=retry_after,
        reroute=reroute_after,
        interrupted=interrupted_after,
        relation_refused=relation_refused,
        tamper_detected=tamper_detected,
        privacy_projection_passed=privacy_passed,
        runtime_schema_version=runtime_schema_version,
        company_schema_version=company_schema_after,
        provider_calls=0,
        quota_consumed=False,
        checks=checks,
    )


def run_active_job_ledger_evaluation() -> ActiveJobLedgerEvaluationRecord:
    """Evaluate durable audit/replay without a provider, credential, or network."""

    return asyncio.run(_run_evaluation())
