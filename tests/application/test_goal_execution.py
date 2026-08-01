from __future__ import annotations

import argparse
import asyncio
import unittest
from types import SimpleNamespace

from dynamic_firm.application import GoalExecutionServices


class GoalExecutionServicesTests(unittest.TestCase):
    def _services(self, *, approvals_available: bool, frozen_route_composition_for=None):
        calls: list[tuple[str, object]] = []

        def config_for(args: argparse.Namespace, settings):
            calls.append(("config", args.goal))
            return SimpleNamespace(goal=args.goal, permission_mode="ask")

        def roster_for(config):
            calls.append(("roster", config.goal))
            return {"employee": "employee-generalist"}

        def provider_config_for(config):
            calls.append(("provider-config", config.goal))
            return {"model": "test-model"}

        def provider_factory(provider_config):
            calls.append(("provider", provider_config["model"]))
            return "provider-instance"

        def coding_worker_for(provider_config, config):
            calls.append(("coding-worker", config.goal))
            return "coding-worker-instance"

        async def runner(config, provider, **kwargs):
            calls.append(("runner", kwargs))
            return {"goal": config.goal, "provider": provider, "kwargs": kwargs}

        return (
            GoalExecutionServices(
                config_for=config_for,
                roster_for=roster_for,
                provider_config_for=provider_config_for,
                provider_factory=provider_factory,
                coding_worker_for=coding_worker_for,
                approval_available_for=lambda _config: approvals_available,
                runner=runner,
                frozen_route_composition_for=frozen_route_composition_for,
            ),
            calls,
        )

    def test_prepare_uses_one_config_roster_provider_and_worker_assembly(self) -> None:
        services, calls = self._services(approvals_available=True)

        prepared = services.prepare(
            argparse.Namespace(goal="Inspect the repository"),
            {"provider": {}},
        )

        self.assertEqual(prepared.config.goal, "Inspect the repository")
        self.assertEqual(prepared.roster_snapshot["employee"], "employee-generalist")
        self.assertEqual(prepared.provider, "provider-instance")
        self.assertEqual(prepared.coding_worker, "coding-worker-instance")
        self.assertEqual(
            [name for name, _value in calls],
            ["config", "roster", "provider-config", "provider", "coding-worker"],
        )

    def test_execute_preserves_frozen_dependencies_and_blocks_unavailable_approval(self) -> None:
        services, calls = self._services(approvals_available=False)
        prepared = services.prepare(argparse.Namespace(goal="Make a plan"), {})

        result = asyncio.run(
            services.execute(
                prepared,
                approval_port="approval-port",
                event_sink=lambda _event: None,
                prior_context=("prior turn",),
                route="COMPANY_GOAL",
                session_key="session-1",
            )
        )

        runner_kwargs = result["kwargs"]
        self.assertIsNone(runner_kwargs["approval_port"])
        self.assertEqual(runner_kwargs["coding_worker"], "coding-worker-instance")
        self.assertEqual(runner_kwargs["roster_snapshot"], prepared.roster_snapshot)
        self.assertEqual(runner_kwargs["prior_context"], ("prior turn",))
        self.assertEqual(runner_kwargs["session_key"], "session-1")
        self.assertEqual(calls[-1][0], "runner")

    def test_execute_passes_approval_only_when_the_config_allows_it(self) -> None:
        services, _calls = self._services(approvals_available=True)
        prepared = services.prepare(argparse.Namespace(goal="Make a plan"), {})

        result = asyncio.run(services.execute(prepared, approval_port="approval-port"))

        self.assertEqual(result["kwargs"]["approval_port"], "approval-port")

    def test_prepare_binds_frozen_route_composition_before_provider_assembly(self) -> None:
        calls: list[tuple[str, object]] = []

        def composition_for(config, roster):
            calls.append(("composition", (config.goal, roster["employee"])))
            return "frozen-composition"

        services, service_calls = self._services(
            approvals_available=True,
            frozen_route_composition_for=composition_for,
        )
        prepared = services.prepare(argparse.Namespace(goal="Route a goal"), {})
        calls.extend(service_calls)

        self.assertEqual(prepared.frozen_route_composition, "frozen-composition")
        self.assertEqual(
            [name for name, _value in calls],
            ["composition", "config", "roster", "provider-config", "provider", "coding-worker"],
        )
        result = asyncio.run(services.execute(prepared))
        self.assertEqual(result["kwargs"]["frozen_route_composition"], "frozen-composition")


if __name__ == "__main__":
    unittest.main()
