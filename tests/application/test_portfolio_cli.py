from __future__ import annotations

import io
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from dynamic_firm.application.portfolio_cli import run_portfolio_command
from dynamic_firm.cli import build_parser, main
from dynamic_firm.company import (
    AuthoritySnapshotIdentity,
    PortfolioEntry,
    PortfolioPolicy,
    PortfolioStatus,
    WorkOrderBudgetSnapshot,
    WorkOrderPortfolioStore,
    normalize_work_order,
)


class PortfolioCliTests(unittest.TestCase):
    def _args(self, *values: str):
        return build_parser().parse_args(["portfolio", *values])

    def test_policy_requires_confirmation_and_persists_only_future_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "runtime.db"
            output = io.StringIO()
            with self.assertRaisesRegex(ValueError, "requires --confirm"):
                run_portfolio_command(
                    self._args(
                        "policy", "set", "--state", str(state_path),
                        "--max-active-jobs", "2", "--max-reserved-cost-usd", "4",
                        "--capability-slot", "gpu=1",
                    ),
                    state_path=state_path,
                    output=output,
                    company_episodes=lambda _: (),
                )
            run_portfolio_command(
                self._args(
                    "policy", "set", "--state", str(state_path),
                    "--max-active-jobs", "2", "--max-reserved-cost-usd", "4",
                    "--capability-slot", "gpu=1",
                    "--confirm", "--json",
                ),
                state_path=state_path,
                output=output,
                company_episodes=lambda _: (),
            )
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["max_active_jobs"], 2)
            self.assertEqual(payload["max_reserved_cost_usd"], 4.0)
            self.assertEqual(payload["capability_slots"], {"gpu": 1})
            with WorkOrderPortfolioStore(state_path.with_name("runtime.work-orders.db")) as store:
                self.assertEqual(
                    store.portfolio_policy(),
                    PortfolioPolicy(
                        max_active_jobs=2,
                        max_reserved_cost_usd=4.0,
                        capability_slots=(("gpu", 1),),
                    ),
                )
                self.assertEqual(store.operator_projection(), ())

    def test_submit_requires_confirmation_and_does_not_execute(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "runtime.db"
            entry = PortfolioEntry(
                work_order_id="work-order-submitted",
                work_order_digest="a" * 64,
                job_id=None,
                priority=70,
                reserved_cost_usd=3.0,
                status=PortfolioStatus.QUEUED,
                reason="SUBMITTED",
                created_at=datetime(2026, 7, 30, tzinfo=UTC).isoformat(),
                updated_at=datetime(2026, 7, 30, tzinfo=UTC).isoformat(),
            )
            with self.assertRaisesRegex(ValueError, "requires --confirm"):
                run_portfolio_command(
                    self._args("submit", "prepare report", "--state", str(state_path)),
                    state_path=state_path,
                    output=io.StringIO(),
                    company_episodes=lambda _: (),
                    submit=lambda *_: entry,
                )
            output = io.StringIO()
            run_portfolio_command(
                self._args("submit", "prepare report", "--state", str(state_path), "--confirm", "--json"),
                state_path=state_path,
                output=output,
                company_episodes=lambda _: (),
                submit=lambda priority, reserve, dependencies, deadline, capabilities: (
                    entry
                    if (priority, reserve, dependencies, deadline, capabilities)
                    == (50, None, (), None, ())
                    else None
                ),
            )
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["execution"], "NOT_STARTED")
            self.assertEqual(payload["entry"]["status"], "QUEUED")

    def test_reestimate_cli_keeps_runtime_unchanged_and_requires_an_explicit_choice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "runtime.db"
            with WorkOrderPortfolioStore(state_path.with_name("runtime.work-orders.db")) as store:
                order = normalize_work_order(
                    "estimate notice",
                    work_order_id="work-order-estimate",
                    authority_snapshot=AuthoritySnapshotIdentity(
                        company_id="company", company_revision=1, roster_revision=1,
                        playbook_revision=1, action_policy_digest="read-only",
                    ),
                    budget_snapshot=WorkOrderBudgetSnapshot(4, 4, 4.0, 10_000),
                    requested_at=datetime(2026, 7, 31, tzinfo=UTC),
                )
                store.submit(order, reserved_cost_usd=2.0)
            output = io.StringIO()
            run_portfolio_command(
                self._args(
                    "reestimate", "report", "work-order-estimate", "--proposed-reserved-cost-usd", "3.5",
                    "--reason", "PROVIDER_SCOPE_CHANGE", "--confirm", "--state", str(state_path), "--json",
                ),
                state_path=state_path,
                output=output,
                company_episodes=lambda _: (),
            )
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["runtime_action"], "NONE")
            reestimate_id = payload["notice"]["reestimate_id"]
            output = io.StringIO()
            run_portfolio_command(
                self._args(
                    "reestimate", "decide", reestimate_id, "--choice", "CONTINUE",
                    "--reason", "OPERATOR_CONTINUE", "--confirm", "--state", str(state_path), "--json",
                ),
                state_path=state_path,
                output=output,
                company_episodes=lambda _: (),
            )
            decision = json.loads(output.getvalue())
            self.assertEqual(decision["notice"]["choice"], "CONTINUE")
            self.assertEqual(decision["next_action"], "NO_RUNTIME_MUTATION")

    def test_preview_is_non_executing_when_the_queue_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "runtime.db"
            output = io.StringIO()
            code = run_portfolio_command(
                self._args("preview", "--state", str(state_path), "--json"),
                state_path=state_path,
                output=output,
                company_episodes=lambda _: (),
            )
            payload = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(payload["execution"], "NOT_STARTED")
            self.assertIsNone(payload["next_entry"])
            self.assertIn("no_unbound_admitted_work_order", payload["reasons"])

    def test_submit_forwards_only_explicit_local_scheduling_facts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "runtime.db"
            captured: list[object] = []
            entry = PortfolioEntry(
                work_order_id="work-order-scheduled",
                work_order_digest="b" * 64,
                job_id=None,
                priority=75,
                reserved_cost_usd=1.0,
                status=PortfolioStatus.QUEUED,
                reason="SUBMITTED",
                created_at=datetime(2026, 7, 31, tzinfo=UTC).isoformat(),
                updated_at=datetime(2026, 7, 31, tzinfo=UTC).isoformat(),
            )

            def submit(priority, reserve, dependencies, deadline, capabilities):  # type: ignore[no-untyped-def]
                captured.extend((priority, reserve, dependencies, deadline, capabilities))
                return entry

            code = run_portfolio_command(
                self._args(
                    "submit",
                    "scheduled goal",
                    "--state",
                    str(state_path),
                    "--priority",
                    "75",
                    "--reserved-cost-usd",
                    "1",
                    "--depends-on",
                    "work-order-prerequisite",
                    "--deadline",
                    "2099-01-01T00:00:00Z",
                    "--requires-capability",
                    "gpu",
                    "--confirm",
                    "--json",
                ),
                state_path=state_path,
                output=io.StringIO(),
                company_episodes=lambda _: (),
                submit=submit,
            )
            self.assertEqual(code, 0)
            self.assertEqual(captured[0:3], [75, 1.0, ("work-order-prerequisite",)])
            self.assertEqual(captured[3].isoformat(), "2099-01-01T00:00:00+00:00")
            self.assertEqual(captured[4], ("gpu",))

    def test_empty_confirmed_drain_is_bounded_and_does_not_contact_a_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "runtime.db"
            output = io.StringIO()
            error = io.StringIO()
            code = main(
                [
                    "portfolio", "drain", "--state", str(state_path),
                    "--confirm", "--json",
                ],
                stdout=output,
                stderr=error,
            )
            payload = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(payload["result"]["waves"], 0)
            self.assertEqual(payload["result"]["dispatched_job_ids"], [])
            self.assertEqual(error.getvalue(), "")
