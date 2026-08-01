from __future__ import annotations

from pathlib import Path
import sys
from unittest import IsolatedAsyncioTestCase, mock

sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from dynamic_firm.application import goal_runtime_resources


class _SyncResource:
    def __init__(self, name: str, events: list[str]) -> None:
        self.name = name
        self.events = events
        self.close_count = 0

    def close(self) -> None:
        self.close_count += 1
        self.events.append(self.name)


class _AsyncResource(_SyncResource):
    async def close(self) -> None:
        self.close_count += 1
        self.events.append(self.name)


class GoalRuntimeResourcesTests(IsolatedAsyncioTestCase):
    async def test_close_is_once_and_keeps_job_resource_order(self) -> None:
        events: list[str] = []
        company = _SyncResource("company", events)
        graph = _SyncResource("graph", events)
        run = _SyncResource("run", events)
        service = _AsyncResource("employee", events)
        session = _SyncResource("session", events)
        resources = goal_runtime_resources._JobRuntimeResources(
            company_store=company,
            graph_blueprint_registry=graph,
            run_store=run,
            employee_service=service,
            session_recall_store=session,
        )

        await resources.close()
        await resources.close()

        self.assertEqual(events, ["employee", "run", "session", "graph", "company"])
        self.assertEqual(
            [item.close_count for item in (service, run, session, graph, company)],
            [1, 1, 1, 1, 1],
        )

    async def test_graph_acquisition_failure_closes_company(self) -> None:
        events: list[str] = []
        company = _SyncResource("company", events)
        with (
            mock.patch.object(goal_runtime_resources.cli, "CompanyStateStore", return_value=company),
            mock.patch.object(
                goal_runtime_resources.cli,
                "SQLiteGraphBlueprintRegistry",
                side_effect=RuntimeError("graph open failed"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "graph open failed"):
                goal_runtime_resources._JobRuntimeResources.acquire(Path("state.db"))

        self.assertEqual(events, ["company"])
        self.assertEqual(company.close_count, 1)

    async def test_run_store_acquisition_failure_closes_existing_resources(self) -> None:
        events: list[str] = []
        resources = goal_runtime_resources._JobRuntimeResources(
            company_store=_SyncResource("company", events),
            graph_blueprint_registry=_SyncResource("graph", events),
        )
        with mock.patch.object(
            goal_runtime_resources.cli,
            "RunStore",
            side_effect=RuntimeError("run open failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "run open failed"):
                await resources.acquire_run_store(Path("state.db"))

        self.assertEqual(events, ["graph", "company"])
