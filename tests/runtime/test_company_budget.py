from __future__ import annotations

import asyncio
import io
import json
import math
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from dynamic_firm.kernel.models import EmployeeRecord, JobLimits, JobStatus
from dynamic_firm.cli import EXIT_INPUT, EXIT_OK, main
from dynamic_firm.kernel.service import FirmKernel
from dynamic_firm.kernel.testing import ScriptedEmployeeExecutionPort, ScriptedOutcome
from dynamic_firm.runtime.company_budget import (
    CompanyCostBudgetPolicy,
    SQLiteCompanyBudgetAuthority,
)
from dynamic_firm.runtime.job_ledger import ActiveJobAuditStatus, ActiveJobInspector, SQLiteActiveJobLedger
from dynamic_firm.runtime.models import Usage
from dynamic_firm.runtime.store import RunStore
from tests.kernel.helpers import company_request, task


class CompanyBudgetTests(unittest.TestCase):
    @staticmethod
    def _request(*, job_id: str, request_id: str, max_cost: float):
        base = company_request(
            (task("final"),),
            final_task_id="final",
            roster=(EmployeeRecord("analyst", "Analyst", ("analysis",)),),
            limits=JobLimits(max_total_cost_usd=max_cost, max_wall_time_ms=5_000),
        )
        return replace(
            base,
            job_id=job_id,
            request_id=request_id,
            plan_proposal=replace(base.plan_proposal, proposal_id=f"proposal-{job_id}"),
            company_revision=4,
        )

    def test_company_policy_defaults_disabled_and_versions_explicitly(self) -> None:
        from dynamic_firm.company.store import CompanyStateStore

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            with CompanyStateStore(path) as company:
                self.assertEqual(
                    company.company_cost_budget_policy(),
                    {"max_total_cost_usd": 0.0, "window_kind": "lifetime"},
                )
                before = company.company().revision
                changed, applied = company.set_company_cost_budget_policy(
                    {"max_total_cost_usd": 1.25, "window_kind": "lifetime"},
                    actor="operator:test",
                )
                self.assertTrue(applied)
                self.assertEqual(changed.revision, before + 1)
                self.assertEqual(
                    company.company_cost_budget_policy(),
                    {"max_total_cost_usd": 1.25, "window_kind": "lifetime"},
                )
                event = company.list_company_policy_events()[-1]
                self.assertEqual(event["policy_name"], "company_cost_budget")
                self.assertEqual(event["actor"], "operator:test")

    def test_cli_versions_budget_policy_only_after_explicit_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            denied = io.StringIO()
            self.assertEqual(
                main(
                    [
                        "company",
                        "budget-policy-set",
                        "--max-total-cost-usd",
                        "2.5",
                        "--state",
                        str(path),
                    ],
                    stderr=denied,
                ),
                EXIT_INPUT,
            )
            self.assertIn("requires --confirm", denied.getvalue())
            output = io.StringIO()
            self.assertEqual(
                main(
                    [
                        "company",
                        "budget-policy-set",
                        "--max-total-cost-usd",
                        "2.5",
                        "--state",
                        str(path),
                        "--confirm",
                        "--json",
                    ],
                    stdout=output,
                ),
                EXIT_OK,
            )
            payload = json.loads(output.getvalue())
            self.assertEqual(
                payload["budget"]["policy"],
                {"max_total_cost_usd": 2.5, "window_kind": "lifetime"},
            )
            status = io.StringIO()
            self.assertEqual(
                main(["company", "budget-status", "--state", str(path), "--json"], stdout=status),
                EXIT_OK,
            )
            self.assertFalse(json.loads(status.getvalue())["paused"])

    def test_conservative_admission_persists_pause_and_requires_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            store = RunStore(path)
            authority = SQLiteCompanyBudgetAuthority(
                store, CompanyCostBudgetPolicy(max_total_cost_usd=1.0)
            )
            first_request = self._request(
                job_id="budget-first", request_id="budget-request-first", max_cost=0.6
            )
            first_runner = ScriptedEmployeeExecutionPort(
                {"final": ScriptedOutcome("done", usage=Usage(model_calls=1, cost_usd=0.4))}
            )
            first = asyncio.run(
                FirmKernel(
                    employee_execution=first_runner,
                    active_job_ledger=SQLiteActiveJobLedger(store),
                    company_budget_authority=authority,
                ).run(first_request)
            )
            self.assertEqual(first.status, JobStatus.SUCCEEDED)

            second_request = self._request(
                job_id="budget-second", request_id="budget-request-second", max_cost=0.7
            )
            denied_runner = ScriptedEmployeeExecutionPort({"final": ScriptedOutcome("must not run")})
            denied = asyncio.run(
                FirmKernel(
                    employee_execution=denied_runner,
                    active_job_ledger=SQLiteActiveJobLedger(store),
                    company_budget_authority=authority,
                ).run(second_request)
            )
            self.assertEqual(denied.status, JobStatus.BUDGET_EXHAUSTED)
            self.assertEqual(denied_runner.requests, [])
            self.assertIn("explicit operator budget resolution", denied.summary.lower())
            self.assertEqual(
                ActiveJobInspector(store).inspect(second_request.job_id).audit_status,
                ActiveJobAuditStatus.TERMINAL,
            )
            paused = authority.status()
            self.assertTrue(paused["paused"])
            incident = paused["incident"]
            self.assertIsNotNone(incident)

            store.close()
            reopened = RunStore(path)
            restarted = SQLiteCompanyBudgetAuthority(
                reopened, CompanyCostBudgetPolicy(max_total_cost_usd=1.0)
            )
            self.assertTrue(restarted.status()["paused"])
            resolved = restarted.resolve_incident(incident.incident_id, actor="operator:test")
            self.assertEqual(resolved.status, "RESOLVED")
            self.assertFalse(restarted.status()["paused"])
            reopened.close()

    def test_settlement_at_limit_opens_hard_stop_after_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory) / "state.db")
            authority = SQLiteCompanyBudgetAuthority(
                store, CompanyCostBudgetPolicy(max_total_cost_usd=1.0)
            )
            request = self._request(
                job_id="budget-limit", request_id="budget-request-limit", max_cost=1.0
            )
            runner = ScriptedEmployeeExecutionPort(
                {"final": ScriptedOutcome("done", usage=Usage(model_calls=1, cost_usd=1.0))}
            )
            result = asyncio.run(
                FirmKernel(
                    employee_execution=runner,
                    active_job_ledger=SQLiteActiveJobLedger(store),
                    company_budget_authority=authority,
                ).run(request)
            )
            self.assertEqual(result.status, JobStatus.SUCCEEDED)
            status = authority.status()
            self.assertTrue(status["paused"])
            self.assertEqual(status["observed_cost_usd"], 1.0)
            self.assertEqual(status["incident"].requested_cost_usd, 0.0)
            store.close()

    def test_company_settlement_includes_compiler_and_employee_cost(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory) / "state.db")
            authority = SQLiteCompanyBudgetAuthority(
                store, CompanyCostBudgetPolicy(max_total_cost_usd=2.0)
            )
            request = replace(
                self._request(
                    job_id="budget-compiler",
                    request_id="budget-request-compiler",
                    max_cost=1.5,
                ),
                planning_mode="DYNAMIC",
                planning_reason="VALID_DYNAMIC",
                compiler_usage=Usage(model_calls=1, cost_usd=0.35),
                compiler_provider_request_id="compiler-budget-request",
            )
            result = asyncio.run(
                FirmKernel(
                    employee_execution=ScriptedEmployeeExecutionPort(
                        {
                            "final": ScriptedOutcome(
                                "done",
                                usage=Usage(model_calls=1, cost_usd=0.40),
                            )
                        }
                    ),
                    active_job_ledger=SQLiteActiveJobLedger(store),
                    company_budget_authority=authority,
                ).run(request)
            )

            self.assertEqual(result.status, JobStatus.SUCCEEDED)
            self.assertAlmostEqual(result.metrics.usage.cost_usd, 0.75)
            self.assertAlmostEqual(authority.status()["observed_cost_usd"], 0.75)
            self.assertTrue(
                ActiveJobInspector(store).inspect(request.job_id).replay_matches
            )
            store.close()

    def test_forfeit_terminalizes_active_lease_at_reserved_cost_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            store = RunStore(path)
            authority = SQLiteCompanyBudgetAuthority(
                store, CompanyCostBudgetPolicy(max_total_cost_usd=5.0)
            )
            request = self._request(
                job_id="budget-forfeit",
                request_id="budget-request-forfeit",
                max_cost=0.6,
            )
            admission = authority.admit_job(request)
            self.assertTrue(admission.allowed)
            self.assertIsNotNone(admission.lease)
            assert admission.lease is not None

            forfeited = authority.forfeit_job(
                admission.lease,
                reason="DIRECT_RUN_CANCELLED",
            )
            repeated = authority.forfeit_job(
                admission.lease,
                reason="DIRECT_RUN_CANCELLED",
            )

            self.assertEqual(repeated, forfeited)
            self.assertAlmostEqual(forfeited.charged_cost_usd, 0.6)
            self.assertEqual(forfeited.reason, "DIRECT_RUN_CANCELLED")
            status = authority.status()
            self.assertAlmostEqual(status["observed_cost_usd"], 0.6)
            self.assertAlmostEqual(status["reserved_cost_usd"], 0.0)
            self.assertFalse(status["paused"])
            with self.assertRaisesRegex(ValueError, "forfeited"):
                store.settle_company_budget_job(
                    admission.lease,
                    actual_cost_usd=0.6,
                )
            with self.assertRaisesRegex(ValueError, "another reason"):
                authority.forfeit_job(
                    admission.lease,
                    reason="DIRECT_RUN_ABORTED",
                )
            with sqlite3.connect(path) as conn:
                lease = conn.execute(
                    "SELECT status, actual_cost_usd FROM company_budget_leases WHERE job_id = ?",
                    (request.job_id,),
                ).fetchone()
                audit = conn.execute(
                    "SELECT reason, charged_cost_usd FROM company_budget_forfeits WHERE job_id = ?",
                    (request.job_id,),
                ).fetchone()
            self.assertEqual(lease, ("SETTLED", 0.6))
            self.assertEqual(audit, ("DIRECT_RUN_CANCELLED", 0.6))
            store.close()

    def test_interrupted_reconciliation_settles_known_or_forfeits_unknown_usage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory) / "state.db")
            authority = SQLiteCompanyBudgetAuthority(
                store, CompanyCostBudgetPolicy(max_total_cost_usd=5.0)
            )
            known = authority.admit_job(
                self._request(job_id="budget-reconcile-known", request_id="budget-reconcile-known-request", max_cost=0.7)
            ).lease
            assert known is not None
            settled = authority.reconcile_interrupted_job(known, observed_cost_usd=0.25)
            self.assertAlmostEqual(settled.actual_cost_usd, 0.25)
            unknown = authority.admit_job(
                self._request(job_id="budget-reconcile-unknown", request_id="budget-reconcile-unknown-request", max_cost=0.6)
            ).lease
            assert unknown is not None
            forfeited = authority.reconcile_interrupted_job(unknown, observed_cost_usd=None)
            self.assertAlmostEqual(forfeited.charged_cost_usd, 0.6)
            self.assertEqual(forfeited.reason, "INTERRUPTED_USAGE_UNCERTAIN")
            store.close()

    def test_active_lease_readmission_requires_exact_frozen_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory) / "state.db")
            authority = SQLiteCompanyBudgetAuthority(
                store, CompanyCostBudgetPolicy(max_total_cost_usd=5.0)
            )
            request = self._request(
                job_id="budget-exact-readmission",
                request_id="budget-request-exact-readmission",
                max_cost=0.6,
            )
            first = authority.admit_job(request)
            self.assertTrue(first.allowed)

            for changed in (
                replace(request, company_revision=request.company_revision + 1),
                replace(
                    request,
                    job_limits=replace(
                        request.job_limits,
                        max_total_cost_usd=0.9,
                    ),
                ),
            ):
                with self.subTest(changed=changed):
                    with self.assertRaisesRegex(ValueError, "identity conflicts"):
                        authority.admit_job(changed)
            store.close()

    def test_compiler_timeout_forfeits_indeterminate_company_reservation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory) / "state.db")
            authority = SQLiteCompanyBudgetAuthority(
                store, CompanyCostBudgetPolicy(max_total_cost_usd=5.0)
            )
            request = replace(
                self._request(
                    job_id="budget-compiler-timeout",
                    request_id="budget-request-compiler-timeout",
                    max_cost=0.8,
                ),
                planning_mode="SOLO_FALLBACK",
                planning_reason="COMPILER_WALL_TIME_EXHAUSTED",
                compiler_usage=Usage(model_calls=2),
            )
            runner = ScriptedEmployeeExecutionPort(
                {"final": ScriptedOutcome("must not execute")}
            )

            result = asyncio.run(
                FirmKernel(
                    employee_execution=runner,
                    company_budget_authority=authority,
                ).run(request)
            )

            self.assertEqual(result.status, JobStatus.BUDGET_EXHAUSTED)
            self.assertEqual(runner.requests, [])
            self.assertAlmostEqual(authority.status()["observed_cost_usd"], 0.8)
            with sqlite3.connect(store.path) as conn:
                audit = conn.execute(
                    "SELECT reason FROM company_budget_forfeits WHERE job_id = ?",
                    (request.job_id,),
                ).fetchone()
            self.assertEqual(audit, ("COMPILER_USAGE_UNCERTAIN",))
            store.close()

    def test_compiler_provider_failure_forfeits_indeterminate_company_reservation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory) / "state.db")
            authority = SQLiteCompanyBudgetAuthority(
                store, CompanyCostBudgetPolicy(max_total_cost_usd=5.0)
            )
            request = replace(
                self._request(
                    job_id="budget-compiler-provider-failure",
                    request_id="budget-request-compiler-provider-failure",
                    max_cost=0.8,
                ),
                planning_mode="SOLO_FALLBACK",
                planning_reason="COMPILER_PROVIDER_FAILURE",
                compiler_usage=Usage(model_calls=2),
            )

            asyncio.run(
                FirmKernel(
                    employee_execution=ScriptedEmployeeExecutionPort(
                        {"final": ScriptedOutcome("reported", usage=Usage(cost_usd=0.1))}
                    ),
                    company_budget_authority=authority,
                ).run(request)
            )

            self.assertAlmostEqual(authority.status()["observed_cost_usd"], 0.8)
            with sqlite3.connect(store.path) as conn:
                audit = conn.execute(
                    "SELECT reason FROM company_budget_forfeits WHERE job_id = ?",
                    (request.job_id,),
                ).fetchone()
            self.assertEqual(audit, ("COMPILER_USAGE_UNCERTAIN",))
            store.close()

    def test_company_budget_rejects_non_finite_costs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory) / "state.db")
            authority = SQLiteCompanyBudgetAuthority(
                store, CompanyCostBudgetPolicy(max_total_cost_usd=5.0)
            )
            request = self._request(
                job_id="budget-finite", request_id="budget-finite-request", max_cost=0.8
            )
            admission = authority.admit_job(request)
            assert admission.lease is not None

            for invalid in (math.nan, math.inf, -math.inf):
                with self.subTest(invalid=invalid):
                    with self.assertRaisesRegex(ValueError, "finite"):
                        store.settle_company_budget_job(
                            admission.lease,
                            actual_cost_usd=invalid,
                        )
            store.close()

    def test_managed_job_cancellation_forfeits_its_active_lease(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory) / "state.db")
            authority = SQLiteCompanyBudgetAuthority(
                store, CompanyCostBudgetPolicy(max_total_cost_usd=5.0)
            )
            request = self._request(
                job_id="budget-managed-cancel",
                request_id="budget-request-managed-cancel",
                max_cost=0.7,
            )
            runner = ScriptedEmployeeExecutionPort(
                {"final": ScriptedOutcome("must be cancelled", delay_seconds=30.0)}
            )

            async def scenario() -> None:
                execution = asyncio.create_task(
                    FirmKernel(
                        employee_execution=runner,
                        company_budget_authority=authority,
                    ).run(request)
                )
                while not runner.requests:
                    await asyncio.sleep(0)
                execution.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await execution

            asyncio.run(scenario())

            status = authority.status()
            self.assertAlmostEqual(status["observed_cost_usd"], 0.7)
            self.assertAlmostEqual(status["reserved_cost_usd"], 0.0)
            with sqlite3.connect(store.path) as conn:
                audit = conn.execute(
                    "SELECT reason FROM company_budget_forfeits WHERE job_id = ?",
                    (request.job_id,),
                ).fetchone()
            self.assertEqual(audit, ("MANAGED_JOB_CANCELLED",))
            store.close()

    def test_managed_wall_timeout_forfeits_indeterminate_employee_usage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory) / "state.db")
            authority = SQLiteCompanyBudgetAuthority(
                store, CompanyCostBudgetPolicy(max_total_cost_usd=5.0)
            )
            request = self._request(
                job_id="budget-managed-timeout",
                request_id="budget-request-managed-timeout",
                max_cost=0.7,
            )
            request = replace(
                request,
                job_limits=replace(request.job_limits, max_wall_time_ms=5),
            )
            runner = ScriptedEmployeeExecutionPort(
                {"final": ScriptedOutcome("too late", delay_seconds=30.0)}
            )

            result = asyncio.run(
                FirmKernel(
                    employee_execution=runner,
                    company_budget_authority=authority,
                ).run(request)
            )

            self.assertEqual(result.status, JobStatus.BUDGET_EXHAUSTED)
            self.assertAlmostEqual(authority.status()["observed_cost_usd"], 0.7)
            with sqlite3.connect(store.path) as conn:
                audit = conn.execute(
                    "SELECT reason FROM company_budget_forfeits WHERE job_id = ?",
                    (request.job_id,),
                ).fetchone()
            self.assertEqual(audit, ("MANAGED_RUN_USAGE_UNCERTAIN",))
            store.close()

    def test_disabled_policy_never_hides_an_open_company_pause(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory) / "state.db")
            enabled = SQLiteCompanyBudgetAuthority(
                store, CompanyCostBudgetPolicy(max_total_cost_usd=1.0)
            )
            denied = enabled.admit_job(
                self._request(
                    job_id="budget-hidden-pause",
                    request_id="budget-hidden-pause-request",
                    max_cost=1.1,
                )
            )
            self.assertFalse(denied.allowed)
            disabled = SQLiteCompanyBudgetAuthority(store, CompanyCostBudgetPolicy())
            status = disabled.status()
            self.assertFalse(status["enabled"])
            self.assertTrue(status["paused"])
            self.assertIsNotNone(status["incident"])
            self.assertEqual(status["incident"].window_kind, "lifetime")
            store.close()


if __name__ == "__main__":
    unittest.main()
