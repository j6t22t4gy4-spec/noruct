"""Read-only operator report for the persistent Executive Manager.

The report deliberately exposes decisions and evidence, never hidden reasoning,
raw tool content, or a new authority path.  CLI/TUI/GUI can project this same
object without reading Company SQLite tables directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .manager import PersistentExecutiveManager
from .manager_outcomes import ManagerOutcomeAssessment, assess_manager_outcomes
from .models import EmployeeSkillVersion, OrganizationEpisode


@dataclass(frozen=True, slots=True)
class ManagerSkillHead:
    """Content-free active Skill metadata for the Manager operator surface.

    A Skill procedure can contain organization-specific instructions.  The
    operator projection consequently identifies a usable head and its exact
    revision, but never copies purpose, steps, verification text, or memory
    into a terminal, GUI, or general Company report.
    """

    skill_key: str
    context_key: str
    revision: int
    source_patch_id: str


@dataclass(frozen=True, slots=True)
class ManagerOperatingReport:
    manager_employee_id: str
    roster_revision: int
    model_profile: str
    memory_namespace: str
    assessment: tuple[ManagerOutcomeAssessment, ...]
    manager_episode_count: int
    supervised_job_count: int
    specialist_job_count: int
    replanned_job_count: int
    pending_reason: str | None
    skill_heads: tuple[ManagerSkillHead, ...] = ()
    state_changed: bool = False


def manager_operating_report(
    manager: PersistentExecutiveManager | None,
    episodes: tuple[OrganizationEpisode, ...],
    *,
    skill_versions: Iterable[EmployeeSkillVersion] = (),
) -> ManagerOperatingReport | None:
    if manager is None:
        return None
    manager_episodes = tuple(
        item for item in episodes if item.manager_employee_id == manager.identity.employee_id
    )
    assessments = assess_manager_outcomes(
        manager_episodes,
        manager_employee_id=manager.identity.employee_id,
    )
    pending = None
    if not manager_episodes:
        pending = "no_manager_attributed_organization_episode"
    elif not assessments or all(
        item.production_episode_count < 2 for item in assessments
    ):
        pending = "insufficient_independent_production_outcomes"
    skill_heads = tuple(
        ManagerSkillHead(
            skill_key=item.skill_key,
            context_key=item.context_key,
            revision=item.revision,
            source_patch_id=item.source_patch_id,
        )
        for item in sorted(
            (
                item
                for item in skill_versions
                if item.active and item.employee_id == manager.identity.employee_id
            ),
            key=lambda item: (item.context_key, item.skill_key, item.revision),
        )
    )
    return ManagerOperatingReport(
        manager_employee_id=manager.identity.employee_id,
        roster_revision=manager.identity.roster_revision,
        model_profile=manager.identity.model_profile,
        memory_namespace=manager.identity.memory_namespace,
        assessment=assessments,
        manager_episode_count=len(manager_episodes),
        supervised_job_count=sum(item.manager_supervision_count > 0 for item in manager_episodes),
        specialist_job_count=sum(item.temporary_role_count > 0 for item in manager_episodes),
        replanned_job_count=sum(item.graph_patch_count > 0 for item in manager_episodes),
        pending_reason=pending,
        skill_heads=skill_heads,
    )
