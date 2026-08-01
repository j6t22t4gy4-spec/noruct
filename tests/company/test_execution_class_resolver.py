from __future__ import annotations

import unittest

from dynamic_firm.company.execution_class_resolver import ExecutionClass, OrganizationExecutionStage, derive_execution_class
from dynamic_firm.company.operating import CompanyWorkMode
from dynamic_firm.company.organization_plan import FrozenOrganizationPlan, OrganizationPlanBindingError, OrganizationPlanRoute, SourceAuthorityBinding


def plan() -> FrozenOrganizationPlan:
    return FrozenOrganizationPlan.from_routes({route: SourceAuthorityBinding(f"authority-{index}", f"digest-{index}") for index, route in enumerate(OrganizationPlanRoute)})


class ExecutionClassResolverTests(unittest.TestCase):
    def test_team_stages_project_existing_plan_routes(self) -> None:
        value = plan(); observed = {binding.authority_id: binding.authority_digest for binding in value.source_bindings}
        projection = derive_execution_class(value, observed, OrganizationExecutionStage.VERIFY, CompanyWorkMode.TEAM_JOB)
        self.assertEqual(projection.execution_class, ExecutionClass.INDEPENDENT_VERIFICATION)
        self.assertEqual(projection.source_route, OrganizationPlanRoute.VERIFICATION)

    def test_direct_and_solo_default_to_strong_solo(self) -> None:
        value = plan(); observed = {binding.authority_id: binding.authority_digest for binding in value.source_bindings}
        for mode in (CompanyWorkMode.DIRECT, CompanyWorkMode.SOLO_JOB):
            self.assertEqual(derive_execution_class(value, observed, "EXPLORE", mode).execution_class, ExecutionClass.STRONG_SOLO)

    def test_missing_or_stale_plan_fails_closed(self) -> None:
        value = plan(); observed = {binding.authority_id: binding.authority_digest for binding in value.source_bindings}
        with self.assertRaises(ValueError): derive_execution_class(None, observed, "FRAME", "DIRECT")
        observed["authority-0"] = "stale"
        with self.assertRaises(OrganizationPlanBindingError): derive_execution_class(value, observed, "FRAME", "TEAM_JOB")
