"""Surface-neutral projection of persistent Manager context into planning.

The planning model gets a deliberately small summary.  Full employee Skills
remain runtime inputs, and raw Knowledge / session memory never cross this
boundary.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence

from dynamic_firm.compiler import (
    ManagerOutcomeSummary,
    ManagerKnowledgeCitation,
    ManagerPlanningBrief,
    ManagerPlanningSkill,
)
from dynamic_firm.runtime.models import VersionedContent
from dynamic_firm.runtime.models import TaskEvidencePack


def build_manager_planning_brief(
    *,
    company_revision: int,
    company_purpose: str,
    work_order_constraints: Sequence[str],
    manager_skill_snapshots: Sequence[VersionedContent],
    recent_episodes: Iterable[object],
    workflow_context_fingerprint: str,
    task_evidence: TaskEvidencePack | None = None,
) -> ManagerPlanningBrief:
    """Return a bounded Manager-only planning projection.

    ``recent_episodes`` intentionally uses only the small public observation
    shape shared by organization episodes.  Its contents are aggregated so a
    model cannot receive user artifacts, task output, or hidden employee
    reasoning through the outcome-history lane.
    """

    skills: list[ManagerPlanningSkill] = []
    for snapshot in manager_skill_snapshots:
        if len(skills) == 3:
            break
        item = _planning_skill(snapshot)
        if item is not None and all(item.skill_key != prior.skill_key for prior in skills):
            skills.append(item)

    matching = tuple(
        item
        for item in recent_episodes
        if getattr(item, "context_fingerprint", "") == workflow_context_fingerprint
    )[-24:]
    outcomes = ManagerOutcomeSummary(
        context_fingerprint=workflow_context_fingerprint,
        observed_count=len(matching),
        succeeded_count=sum(bool(getattr(item, "success", False)) for item in matching),
        safety_passed_count=sum(
            bool(getattr(item, "safety_passed", False)) for item in matching
        ),
        effect_passed_count=sum(
            bool(getattr(item, "effect_passed", False)) for item in matching
        ),
    )
    if task_evidence is not None:
        task_evidence.verify()
        knowledge_pack_id = task_evidence.pack_id
        knowledge_pack_digest = task_evidence.pack_digest
        knowledge_delivery_digest = task_evidence.delivery_digest
        knowledge_citations = tuple(
            ManagerKnowledgeCitation(
                citation_id=item.citation_id,
                source_id=item.source_id,
                source_revision=item.source_revision,
            )
            for item in task_evidence.items
        )
    else:
        knowledge_pack_id = knowledge_pack_digest = knowledge_delivery_digest = ""
        knowledge_citations = ()
    return ManagerPlanningBrief(
        company_revision=company_revision,
        company_purpose=" ".join(company_purpose.split())[:1_000],
        work_order_constraints=tuple(
            " ".join(str(item).split())[:360]
            for item in work_order_constraints
            if str(item).strip()
        )[:6],
        skills=tuple(skills),
        outcome_summary=outcomes,
        knowledge_pack_id=knowledge_pack_id,
        knowledge_pack_digest=knowledge_pack_digest,
        knowledge_delivery_digest=knowledge_delivery_digest,
        knowledge_citations=knowledge_citations,
    )


def _planning_skill(snapshot: VersionedContent) -> ManagerPlanningSkill | None:
    """Extract only an active Skill key/revision/purpose from its snapshot."""

    if len(snapshot.content_hash) != 64:
        return None
    try:
        payload = json.loads(snapshot.content)
        procedure = payload["procedure"]
        skill_key = str(procedure["skill_key"]).strip()
        purpose = " ".join(str(procedure["purpose"]).split())
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if not skill_key or not purpose:
        return None
    try:
        return ManagerPlanningSkill(
            skill_key=skill_key[:128],
            revision=str(snapshot.revision).strip()[:64],
            purpose=purpose[:360],
            content_hash=snapshot.content_hash,
        )
    except ValueError:
        return None
