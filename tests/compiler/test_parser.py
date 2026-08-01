from __future__ import annotations

import unittest

from dynamic_firm.compiler.parser import (
    PlanOutputError,
    PlanProposalError,
    parse_plan_proposal,
)


def task(
    task_id: str,
    *,
    depends_on=(),
    capability: str = "repository_analysis",
    risk: str = "LOW",
) -> dict:
    return {
        "task_id": task_id,
        "objective": f"Complete {task_id}",
        "depends_on": list(depends_on),
        "required_capabilities": [capability],
        "acceptance_criteria": [f"Evidence for {task_id}"],
        "risk_level": risk,
    }


def plan(mode: str, tasks: list[dict], final_task_id: str) -> dict:
    return {
        "mode": mode,
        "rationale": "Use the smallest useful plan.",
        "assumptions": [],
        "tasks": tasks,
        "final_task_id": final_task_id,
    }


def parse(value: dict, *, max_temporary_roles: int = 2):
    return parse_plan_proposal(
        value,
        proposal_id="proposal-test",
        goal="Inspect the repository",
        max_tasks=6,
        available_capabilities=("repository_analysis", "evidence_synthesis"),
        max_temporary_roles=max_temporary_roles,
    )


class PlanParserTests(unittest.TestCase):
    def test_accepts_typed_value_gated_candidate_replicas(self) -> None:
        candidate_a = task("candidate_a")
        candidate_b = task("candidate_b")
        replica_base = {
            "group_id": "candidate_group",
            "strategy": "CANDIDATE",
            "scope": "same bounded proposal",
            "aggregation_task_id": "final",
            "aggregation": "VALIDATOR_SELECT",
            "marginal_value_reason": (
                "Multiple candidates are useful because a separate validator can select one."
            ),
        }
        candidate_a["execution_replica"] = {
            **replica_base,
            "replica_id": "candidate_a",
        }
        candidate_b["execution_replica"] = {
            **replica_base,
            "replica_id": "candidate_b",
        }
        final = task(
            "final",
            depends_on=("candidate_a", "candidate_b"),
            capability="validation",
        )
        final["execution_replica"] = None

        parsed = parse(
            plan("GRAPH", [candidate_a, candidate_b, final], "final")
        )

        spec = parsed.proposal.tasks[0].execution_replica
        self.assertIsNotNone(spec)
        assert spec is not None
        self.assertEqual(spec.strategy.value, "CANDIDATE")
        self.assertEqual(spec.aggregation.value, "VALIDATOR_SELECT")

    def test_accepts_exact_solo_and_graph_contracts(self) -> None:
        solo = parse(plan("SOLO", [task("analyze")], "analyze"))
        graph = parse(
            plan(
                "GRAPH",
                [
                    task("code", capability="code_analysis"),
                    task("tests", capability="test_analysis"),
                    task(
                        "final",
                        depends_on=("code", "tests"),
                        capability="evidence_synthesis",
                    ),
                ],
                "final",
            )
        )

        self.assertEqual(solo.source_mode, "SOLO")
        self.assertEqual(len(solo.proposal.tasks), 1)
        self.assertEqual(graph.source_mode, "GRAPH")
        self.assertEqual(graph.proposal.tasks[-1].depends_on, ("code", "tests"))

    def test_rejects_unknown_fields_wrong_mode_and_duplicates_without_repair(self) -> None:
        unknown = plan("SOLO", [task("analyze")], "analyze")
        unknown["agent"] = "researcher"
        with self.assertRaisesRegex(PlanOutputError, "unknown agent"):
            parse(unknown)

        with self.assertRaisesRegex(PlanProposalError, "SOLO"):
            parse(plan("SOLO", [task("a"), task("b")], "b"))

        duplicate = task("analyze")
        duplicate["required_capabilities"] = ["repository_analysis", "repository_analysis"]
        with self.assertRaisesRegex(PlanOutputError, "duplicates"):
            parse(plan("SOLO", [duplicate], "analyze"))

    def test_rejects_cycle_disconnected_work_and_non_low_risk(self) -> None:
        with self.assertRaisesRegex(PlanProposalError, "cycle"):
            parse(
                plan(
                    "GRAPH",
                    [
                        task("a", depends_on=("b",)),
                        task("b", depends_on=("a",)),
                    ],
                    "b",
                )
            )

        with self.assertRaisesRegex(PlanProposalError, "disconnected"):
            parse(
                plan(
                    "GRAPH",
                    [
                        task("useful"),
                        task("unused"),
                        task("final", depends_on=("useful",)),
                    ],
                    "final",
                )
            )

        with self.assertRaisesRegex(PlanProposalError, "LOW"):
            parse(plan("SOLO", [task("deploy", risk="HIGH")], "deploy"))

    def test_rejects_more_missing_capabilities_than_temporary_role_limit(self) -> None:
        with self.assertRaisesRegex(PlanProposalError, "temporary capabilities"):
            parse(
                plan(
                    "GRAPH",
                    [
                        task("code", capability="code_analysis"),
                        task("tests", capability="test_analysis"),
                        task("final", depends_on=("code", "tests")),
                    ],
                    "final",
                ),
                max_temporary_roles=1,
            )


if __name__ == "__main__":
    unittest.main()
