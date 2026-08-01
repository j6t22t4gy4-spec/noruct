from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from dynamic_firm.company.store import CompanyStateStore
from dynamic_firm.knowledge.models import DecisionStatus
from dynamic_firm.knowledge.store import KnowledgeStore, knowledge_state_path
from dynamic_firm.runtime.manager_tools import ManagerRuntimeTools, is_manager_tool
from dynamic_firm.runtime.models import ToolEffect
from dynamic_firm.runtime.ports import CancellationToken
from dynamic_firm.runtime.store import RunStore


class ManagerRuntimeToolTests(unittest.TestCase):
    def test_catalog_is_read_only_bounded_and_authority_free(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime_path = root / "runtime.db"
            company = CompanyStateStore(runtime_path)
            runs = RunStore(runtime_path)
            try:
                company.ensure_roster_baseline(
                    (
                        {
                            "employee_id": "employee-manager",
                            "role": "Executive Manager",
                            "capabilities": ("company_management",),
                            "active": True,
                            "temporary": False,
                            "model_profile": "fixture",
                        },
                        {
                            "employee_id": "employee-analyst",
                            "role": "Analyst",
                            "capabilities": ("analysis",),
                            "active": True,
                            "temporary": False,
                            "model_profile": "fixture",
                        },
                    )
                )
                tools = {
                    item.name: item
                    for item in ManagerRuntimeTools(
                        company_store=company,
                        run_store=runs,
                        runtime_state_path=runtime_path,
                        current_job_id="job-direct",
                    ).definitions()
                }

                self.assertEqual(
                    set(tools),
                    {
                        "manager_inspect_company",
                        "manager_inspect_current_job",
                        "manager_read_intent_brief",
                        "manager_review_recent_outcomes",
                    },
                )
                self.assertTrue(all(is_manager_tool(name) for name in tools))
                self.assertTrue(
                    all(item.effect == ToolEffect.READ and not item.requires_approval for item in tools.values())
                )

                company_payload = json.loads(
                    asyncio.run(
                        tools["manager_inspect_company"].handler(
                            tools["manager_inspect_company"].validator({}),
                            CancellationToken(),
                        )
                    )
                )
                self.assertFalse(company_payload["authority_granted"])
                self.assertEqual(company_payload["roster"]["revision"], 2)
                self.assertEqual(len(company_payload["roster"]["employees"]), 2)
                self.assertNotIn("model_profile", company_payload["roster"]["employees"][0])

                direct_job = json.loads(
                    asyncio.run(
                        tools["manager_inspect_current_job"].handler(
                            tools["manager_inspect_current_job"].validator({}),
                            CancellationToken(),
                        )
                    )
                )
                self.assertFalse(direct_job["tracked_active_job"])
                self.assertFalse(direct_job["authority_granted"])

                empty_outcomes = json.loads(
                    asyncio.run(
                        tools["manager_review_recent_outcomes"].handler(
                            tools["manager_review_recent_outcomes"].validator({}),
                            CancellationToken(),
                        )
                    )
                )
                self.assertEqual(empty_outcomes["outcomes"], [])
                self.assertFalse(empty_outcomes["raw_employee_output_included"])
            finally:
                runs.close()
                company.close()

    def test_intent_brief_uses_the_separate_control_plane_without_evidence_bodies(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime_path = root / "runtime.db"
            company = CompanyStateStore(runtime_path)
            runs = RunStore(runtime_path)
            knowledge = KnowledgeStore(knowledge_state_path(runtime_path))
            try:
                intent = knowledge.create_intent(
                    goal="Choose the September pricing strategy",
                    constraints=("Keep gross margin above the threshold",),
                    acceptance_criteria=("Decision has a review date",),
                )
                decision = knowledge.create_decision(
                    statement="Keep the current price until review",
                    rationale="Private evidence rationale must not be projected.",
                    status=DecisionStatus.ACCEPTED,
                    intent_id=intent.intent_id,
                    review_at="2026-08-20T00:00:00+00:00",
                )
                definition = next(
                    item
                    for item in ManagerRuntimeTools(
                        company_store=company,
                        run_store=runs,
                        runtime_state_path=runtime_path,
                        current_job_id="job-managed",
                    ).definitions()
                    if item.name == "manager_read_intent_brief"
                )
                payload = json.loads(
                    asyncio.run(
                        definition.handler(
                            definition.validator({"limit": 2}),
                            CancellationToken(),
                        )
                    )
                )

                self.assertEqual(payload["intents"][0]["intent_id"], intent.intent_id)
                self.assertEqual(payload["decisions"][0]["decision_id"], decision.decision_id)
                self.assertNotIn("rationale", payload["decisions"][0])
                self.assertNotIn("evidence_pack_id", payload["decisions"][0])
                self.assertFalse(payload["authority_granted"])
            finally:
                knowledge.close()
                runs.close()
                company.close()

    def test_validator_rejects_unbounded_or_unknown_queries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            company = CompanyStateStore(root / "runtime.db")
            runs = RunStore(root / "runtime.db")
            try:
                tools = {
                    item.name: item
                    for item in ManagerRuntimeTools(
                        company_store=company,
                        run_store=runs,
                        runtime_state_path=root / "runtime.db",
                        current_job_id="job",
                    ).definitions()
                }
                with self.assertRaisesRegex(Exception, "between 1 and 8"):
                    tools["manager_read_intent_brief"].validator({"limit": 100})
                with self.assertRaisesRegex(Exception, "unknown argument"):
                    tools["manager_review_recent_outcomes"].validator({"job_id": "other"})
                with self.assertRaisesRegex(Exception, "does not accept arguments"):
                    tools["manager_inspect_current_job"].validator({"job_id": "other"})
            finally:
                runs.close()
                company.close()


if __name__ == "__main__":
    unittest.main()
