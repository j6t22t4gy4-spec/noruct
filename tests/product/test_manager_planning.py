from __future__ import annotations

import json
import unittest

from dynamic_firm.product.manager_planning import build_manager_planning_brief
from dynamic_firm.runtime.models import TaskEvidenceItem, TaskEvidencePack, VersionedContent


class _Episode:
    def __init__(self, *, context: str, success: bool, safe: bool, effect: bool) -> None:
        self.context_fingerprint = context
        self.success = success
        self.safety_passed = safe
        self.effect_passed = effect


def _skill(*, key: str = "manager_planning", purpose: str = "Select a small staffing shape") -> VersionedContent:
    return VersionedContent(
        content_id=f"employee-skill:manager:{key}:context",
        revision="2",
        content=json.dumps({"procedure": {"skill_key": key, "purpose": purpose}}),
        content_hash="a" * 64,
    )


class ManagerPlanningBriefTests(unittest.TestCase):
    def test_projects_only_selected_skills_and_aggregated_matching_outcomes(self) -> None:
        brief = build_manager_planning_brief(
            company_revision=4,
            company_purpose=" Build a durable company ",
            work_order_constraints=("Keep effects approval gated.",),
            manager_skill_snapshots=(_skill(),),
            recent_episodes=(
                _Episode(context="matching", success=True, safe=True, effect=True),
                _Episode(context="other", success=True, safe=True, effect=True),
                _Episode(context="matching", success=False, safe=False, effect=False),
            ),
            workflow_context_fingerprint="matching",
        )

        self.assertEqual(brief.company_purpose, "Build a durable company")
        self.assertEqual(brief.skills[0].skill_key, "manager_planning")
        self.assertEqual(brief.outcome_summary.observed_count, 2)
        self.assertEqual(brief.outcome_summary.succeeded_count, 1)
        self.assertEqual(brief.outcome_summary.safety_passed_count, 1)
        self.assertEqual(brief.outcome_summary.effect_passed_count, 1)

    def test_rejects_unparseable_or_unhashed_skill_snapshots(self) -> None:
        brief = build_manager_planning_brief(
            company_revision=1,
            company_purpose="Company",
            work_order_constraints=(),
            manager_skill_snapshots=(
                VersionedContent("bad", "1", "not json", "b" * 64),
                VersionedContent("unhashed", "1", _skill().content, ""),
            ),
            recent_episodes=(),
            workflow_context_fingerprint="",
        )

        self.assertEqual(brief.skills, ())
        self.assertEqual(brief.outcome_summary.observed_count, 0)

    def test_projects_only_content_free_knowledge_citations(self) -> None:
        content = "Private source body must not enter Manager planning."
        evidence = TaskEvidencePack(
            pack_id="pack-knowledge-brief",
            revision=1,
            pack_digest="a" * 64,
            delivery_digest="",
            access_scope="private",
            items=(
                TaskEvidenceItem(
                    citation_id="evidence-knowledge-brief",
                    source_id="folder-entry-private",
                    source_revision="folder-entry-r1:source",
                    title="private.md",
                    content=content,
                    source_hash="b" * 64,
                    content_hash="",
                    location={"relative_path": "private.md"},
                ),
            ),
        )
        # Construct the self-authenticating fixture through the existing delivery primitive.
        item = evidence.items[0]
        import hashlib
        from dataclasses import replace
        evidence = replace(
            evidence,
            items=(replace(item, content_hash=hashlib.sha256(content.encode()).hexdigest()),),
        )
        evidence = replace(evidence, delivery_digest=evidence.computed_delivery_digest())
        brief = build_manager_planning_brief(
            company_revision=1,
            company_purpose="Company",
            work_order_constraints=(),
            manager_skill_snapshots=(),
            recent_episodes=(),
            workflow_context_fingerprint="",
            task_evidence=evidence,
        )
        self.assertEqual(brief.knowledge_pack_id, "pack-knowledge-brief")
        self.assertEqual(brief.knowledge_citations[0].citation_id, "evidence-knowledge-brief")
        self.assertNotIn(content, brief.content_digest)
