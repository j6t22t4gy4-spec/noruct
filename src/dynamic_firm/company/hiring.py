from __future__ import annotations

import re
from collections import defaultdict

from dynamic_firm.kernel.models import EmployeeRecord

from .models import (
    HiringRecommendationResult,
    RosterPatchCandidate,
    RosterPatchOperation,
    RosterPatchStatus,
    StaffingDemandEvidence,
    content_digest,
)
from .roster import decode_active_roster
from .roster_patch import RosterPatchService
from .store import CompanyStateStore


MINIMUM_STAFFING_DEMAND_COUNT = 2
HIRING_RECOMMENDER_ACTOR = "system:staffing-demand-curator"


def _employee_identity(capability: str, context_fingerprint: str) -> tuple[str, str]:
    words = tuple(item for item in re.split(r"[^a-z0-9]+", capability.casefold()) if item)
    slug = "-".join(words) or "capability"
    suffix = content_digest(
        {"capability": capability, "context_fingerprint": context_fingerprint}
    )[:8]
    employee_id = f"employee-{slug[:40].rstrip('-')}-{suffix}"
    role_stem = " ".join(item.capitalize() for item in words) or "Capability"
    return employee_id, f"{role_stem} Specialist"


class HiringRecommendationService:
    """Turn repeated safe demand into proposals, never approval or ROSTER mutation."""

    def __init__(self, store: CompanyStateStore) -> None:
        self.store = store
        self.roster_patches = RosterPatchService(store)

    @staticmethod
    def _group_key(evidence: StaffingDemandEvidence) -> tuple[str, str]:
        return evidence.context_fingerprint, evidence.capability

    def curate(self) -> HiringRecommendationResult:
        evidence = self.store.list_staffing_demands()
        qualified = tuple(item for item in evidence if item.safety_passed)
        groups: dict[tuple[str, str], list[StaffingDemandEvidence]] = defaultdict(list)
        for item in evidence:
            groups[self._group_key(item)].append(item)

        roster = decode_active_roster(self.store.roster())
        active_capabilities = {
            item.strip().casefold() for item in roster.available_capabilities
        }
        open_patches = tuple(
            patch
            for patch in self.store.list_roster_patches()
            if patch.status in {RosterPatchStatus.PROPOSED, RosterPatchStatus.APPROVED}
            and patch.operation == RosterPatchOperation.ADD_EMPLOYEE
        )
        candidates: list[RosterPatchCandidate] = []
        reasons: list[str] = []

        for (context_fingerprint, capability), items in sorted(groups.items()):
            ordered = tuple(
                sorted(items, key=lambda item: (item.recorded_at, item.evidence_id))
            )
            if any(not item.safety_passed for item in ordered):
                reasons.append(f"staffing_demand_failed_safety_gate:{capability}")
                continue
            independent_jobs = {item.job_id for item in ordered}
            if len(independent_jobs) < MINIMUM_STAFFING_DEMAND_COUNT:
                reasons.append(f"insufficient_repeated_demand:{capability}")
                continue
            if capability in active_capabilities:
                reasons.append(f"capability_already_covered:{capability}")
                continue
            duplicate = next(
                (
                    patch
                    for patch in open_patches
                    if capability
                    in {
                        str(item).strip().casefold()
                        for item in patch.after_employee.get("capabilities", ())
                    }
                ),
                None,
            )
            if duplicate is not None:
                candidates.append(duplicate)
                reasons.append(f"matching_open_roster_patch:{capability}")
                continue

            production = tuple(item for item in ordered if item.production_eligible)
            selected = (
                production
                if len({item.job_id for item in production})
                >= MINIMUM_STAFFING_DEMAND_COUNT
                else ordered
            )
            employee_id, role = _employee_identity(capability, context_fingerprint)
            candidate = self.roster_patches.propose_add_employee(
                EmployeeRecord(
                    employee_id=employee_id,
                    role=role,
                    capabilities=(capability,),
                    model_profile="company-default",
                ),
                rationale=(
                    f"Repeated validated temporary staffing demand for {capability} "
                    f"across {len({item.job_id for item in selected})} independent jobs."
                ),
                actor=HIRING_RECOMMENDER_ACTOR,
                evidence_ids=tuple(sorted(item.evidence_id for item in selected)),
            )
            if candidate.status in {
                RosterPatchStatus.PROPOSED,
                RosterPatchStatus.APPROVED,
            }:
                candidates.append(candidate)
            else:
                reasons.append(f"matching_recommendation_was_closed:{capability}")

        if not evidence:
            reasons.append("no_staffing_demand_evidence")
        elif not qualified:
            reasons.append("no_staffing_demand_passed_safety_gates")

        unique_candidates = {
            candidate.patch_id: candidate for candidate in candidates
        }
        return HiringRecommendationResult(
            decision="CANDIDATE_AVAILABLE" if unique_candidates else "NO_PATCH",
            candidates=tuple(unique_candidates[key] for key in sorted(unique_candidates)),
            considered_evidence_count=len(evidence),
            qualified_evidence_count=len(qualified),
            reasons=tuple(dict.fromkeys(reasons)),
        )
