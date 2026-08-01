from __future__ import annotations

from dataclasses import replace
import unittest

from dynamic_firm.company.execution_route_binding import ExecutionRouteBinding
from dynamic_firm.company.multi_route_job_plan import DependencyArtifactHandoff, MultiRouteJobPlan, TaskRouteAssignment
from dynamic_firm.company.multi_route_runtime_policy import MultiRouteRuntimePolicy
from dynamic_firm.runtime.models import EmployeeRunRequest
from tests.runtime.helpers import make_request


def binding(route_id: str, config_digest: str) -> ExecutionRouteBinding:
    values: dict[str, object] = {
        "attempt_id": f"attempt-{route_id}",
        "route_id": route_id,
        "execution_profile_id": f"profile-{route_id}",
        "provider_config_digest": config_digest,
        "credential_reference": "NORUCT_PROVIDER_KEY",
        "requested_model_id": f"model-{route_id}",
        "identity_assurance": "VERSIONED_MODEL_ID",
    }
    values.update(
        {
            name: "b" * 64
            for name in (
                "required_capability_digest",
                "inference_contract_digest",
                "egress_policy_digest",
                "intelligence_snapshot_digest",
                "orchestration_policy_digest",
                "compatibility_evidence_digest",
                "fallback_policy_digest",
                "fanout_policy_digest",
                "continuation_policy_digest",
            )
        }
    )
    return ExecutionRouteBinding(**values)


class EmployeeRunRequestWithModelProfile(EmployeeRunRequest):
    __slots__ = ("model_profile",)


class MultiRouteRuntimePolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.explore = binding("explore", "a" * 64)
        self.integrate = binding("integrate", "c" * 64)
        self.plan = MultiRouteJobPlan(
            "d" * 64,
            (
                TaskRouteAssignment("explore", "employee-a", self.explore.digest),
                TaskRouteAssignment(
                    "integrate", "employee-b", self.integrate.digest, final=True
                ),
            ),
            (),
            "employee-b",
        )
        self.policy = MultiRouteRuntimePolicy(self.plan, (self.integrate, self.explore))

    @staticmethod
    def request(task_id: str, employee_id: str) -> EmployeeRunRequest:
        request = make_request()
        return replace(
            request,
            task=replace(request.task, task_id=task_id),
            employee=replace(request.employee, employee_id=employee_id),
        )

    def test_heterogeneous_tasks_return_only_their_exact_frozen_binding(self) -> None:
        self.assertIs(self.policy(self.request("explore", "employee-a")), self.explore)
        self.assertIs(self.policy(self.request("integrate", "employee-b")), self.integrate)

    def test_task_or_employee_drift_is_rejected_before_provider_construction(self) -> None:
        with self.assertRaises(ValueError):
            self.policy(self.request("missing", "employee-a"))
        with self.assertRaises(ValueError):
            self.policy(self.request("explore", "employee-b"))

    def test_missing_extra_or_digest_mismatched_bindings_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            MultiRouteRuntimePolicy(self.plan, (self.explore,))
        with self.assertRaises(ValueError):
            MultiRouteRuntimePolicy(
                self.plan,
                (self.explore, self.integrate, binding("extra", "e" * 64)),
            )
        with self.assertRaises(ValueError):
            MultiRouteRuntimePolicy(
                self.plan,
                (binding("explore-drift", "f" * 64), self.integrate),
            )

    def test_canonical_summary_is_deterministic_and_content_free(self) -> None:
        reversed_bindings = MultiRouteRuntimePolicy(self.plan, (self.explore, self.integrate))
        self.assertEqual(self.policy.canonical_summary(), reversed_bindings.canonical_summary())
        self.assertEqual(self.policy.summary_digest, reversed_bindings.summary_digest)
        self.assertNotIn("model-explore", self.policy.canonical_summary())
        self.assertNotIn("NORUCT_PROVIDER_KEY", self.policy.canonical_summary())

    def test_summary_digest_binds_dependency_handoff_and_final_owner(self) -> None:
        dependent = MultiRouteJobPlan(
            "d" * 64,
            (
                TaskRouteAssignment("explore", "employee-a", self.explore.digest),
                TaskRouteAssignment("integrate", "employee-b", self.integrate.digest, depends_on=("explore",), final=True),
            ),
            (DependencyArtifactHandoff("explore", "integrate", "e" * 64),),
            "employee-b",
        )
        final_owner_drift = MultiRouteJobPlan(
            "d" * 64,
            (
                TaskRouteAssignment("explore", "employee-a", self.explore.digest, final=True),
                TaskRouteAssignment("integrate", "employee-b", self.integrate.digest),
            ),
            (),
            "employee-a",
        )
        self.assertNotEqual(
            self.policy.summary_digest,
            MultiRouteRuntimePolicy(dependent, (self.explore, self.integrate)).summary_digest,
        )
        self.assertNotEqual(
            self.policy.summary_digest,
            MultiRouteRuntimePolicy(final_owner_drift, (self.explore, self.integrate)).summary_digest,
        )

    def test_summary_digest_binds_expected_selection_receipt_digest(self) -> None:
        receipt_bound = MultiRouteJobPlan(
            "d" * 64,
            (
                TaskRouteAssignment(
                    "explore",
                    "employee-a",
                    self.explore.digest,
                    expected_selection_receipt_digest="e" * 64,
                ),
                TaskRouteAssignment("integrate", "employee-b", self.integrate.digest, final=True),
            ),
            (),
            "employee-b",
        )
        self.assertNotEqual(
            self.policy.summary_digest,
            MultiRouteRuntimePolicy(receipt_bound, (self.explore, self.integrate)).summary_digest,
        )

    def test_request_model_profile_cannot_change_the_returned_binding(self) -> None:
        request = self.request("explore", "employee-a")
        profiled = EmployeeRunRequestWithModelProfile(
            **{name: getattr(request, name) for name in request.__dataclass_fields__}
        )
        object.__setattr__(profiled, "model_profile", "attacker-controlled-route")
        self.assertIs(self.policy(profiled), self.explore)


if __name__ == "__main__":
    unittest.main()
