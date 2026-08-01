from __future__ import annotations

import unittest
from dataclasses import replace

from dynamic_firm.company.approved_route_plan_admission import (
    ApprovedRoutePlanAdmission,
    ApprovedRoutePlanAdmissionDisposition,
    admit_approved_route_plan,
    require_fresh_approved_route_plan,
)
from dynamic_firm.company.execution_route_binding import ExecutionRouteBinding
from dynamic_firm.company.multi_route_job_plan import (
    DependencyArtifactHandoff,
    MultiRouteJobPlan,
    TaskRouteAssignment,
)
from dynamic_firm.company.multi_route_runtime_policy import MultiRouteRuntimePolicy
from dynamic_firm.company.user_routing_policy import (
    ApprovedRouteMetadata,
    ApprovedRouteRegistry,
    UserRoutingPolicy,
    UserRoutingPolicyMode,
)


def binding(route_id: str, config_digest: str, credential_reference: str) -> ExecutionRouteBinding:
    values: dict[str, object] = {
        "attempt_id": f"attempt-{route_id}", "route_id": route_id,
        "execution_profile_id": f"profile-{route_id}", "provider_config_digest": config_digest,
        "credential_reference": credential_reference, "requested_model_id": f"model-{route_id}",
        "identity_assurance": "VERSIONED_MODEL_ID",
    }
    values.update({name: "b" * 64 for name in (
        "required_capability_digest", "inference_contract_digest", "egress_policy_digest",
        "intelligence_snapshot_digest", "orchestration_policy_digest", "compatibility_evidence_digest",
        "fallback_policy_digest", "fanout_policy_digest", "continuation_policy_digest",
    )})
    return ExecutionRouteBinding(**values)


class ApprovedRoutePlanAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.explore = binding("explore", "a" * 64, "PRIMARY_PROVIDER_KEY")
        self.integrate = binding("integrate", "c" * 64, "SECONDARY_PROVIDER_KEY")
        plan = MultiRouteJobPlan("d" * 64, (
            TaskRouteAssignment("explore", "employee-a", self.explore.digest),
            TaskRouteAssignment("integrate", "employee-b", self.integrate.digest, final=True),
        ), (), "employee-b")
        self.runtime_policy = MultiRouteRuntimePolicy(plan, (self.integrate, self.explore))
        self.registry = ApprovedRouteRegistry((
            ApprovedRouteMetadata("explore", self.explore.digest, "a" * 64, "PRIMARY_PROVIDER_KEY"),
            ApprovedRouteMetadata("integrate", self.integrate.digest, "c" * 64, "SECONDARY_PROVIDER_KEY"),
        ))

    def test_fresh_admission_returns_the_exact_original_frozen_policy_without_provider_factories(self) -> None:
        admission = admit_approved_route_plan(
            UserRoutingPolicy(UserRoutingPolicyMode.BALANCED), self.registry, self.runtime_policy
        )
        self.assertTrue(admission.admitted)
        self.assertIs(
            require_fresh_approved_route_plan(
                UserRoutingPolicy(UserRoutingPolicyMode.BALANCED), self.registry, self.runtime_policy
            ),
            self.runtime_policy,
        )
        self.assertEqual(admission.approved_binding_count, 2)
        self.assertEqual(admission.disposition, ApprovedRoutePlanAdmissionDisposition.ADMITTED_APPROVED_REUSE)

    def test_first_run_empty_registry_is_a_distinct_fail_closed_denial(self) -> None:
        admission = admit_approved_route_plan(
            UserRoutingPolicy(UserRoutingPolicyMode.PRIVATE_LOCAL_FIRST), ApprovedRouteRegistry(()), self.runtime_policy
        )
        self.assertEqual(admission.disposition, ApprovedRoutePlanAdmissionDisposition.DENIED_FIRST_RUN_NO_APPROVED_ROUTES)
        self.assertFalse(admission.admitted)
        with self.assertRaises(ValueError):
            require_fresh_approved_route_plan(
                UserRoutingPolicy(UserRoutingPolicyMode.PRIVATE_LOCAL_FIRST), ApprovedRouteRegistry(()), self.runtime_policy
            )

    def test_missing_config_and_credential_mismatches_fail_closed_before_activation(self) -> None:
        policy = UserRoutingPolicy(UserRoutingPolicyMode.QUALITY_FIRST)
        missing = ApprovedRouteRegistry((ApprovedRouteMetadata("explore", self.explore.digest, "a" * 64, "PRIMARY_PROVIDER_KEY"),))
        self.assertEqual(admit_approved_route_plan(policy, missing, self.runtime_policy).disposition,
                         ApprovedRoutePlanAdmissionDisposition.DENIED_MISSING_ROUTE_APPROVAL)
        wrong_config = ApprovedRouteRegistry((
            ApprovedRouteMetadata("explore", self.explore.digest, "a" * 64, "PRIMARY_PROVIDER_KEY"),
            ApprovedRouteMetadata("integrate", self.integrate.digest, "e" * 64, "SECONDARY_PROVIDER_KEY"),
        ))
        self.assertEqual(admit_approved_route_plan(policy, wrong_config, self.runtime_policy).disposition,
                         ApprovedRoutePlanAdmissionDisposition.DENIED_PROVIDER_CONFIG_DIGEST_MISMATCH)
        wrong_credential = ApprovedRouteRegistry((
            ApprovedRouteMetadata("explore", self.explore.digest, "a" * 64, "PRIMARY_PROVIDER_KEY"),
            ApprovedRouteMetadata("integrate", self.integrate.digest, "c" * 64, "OTHER_PROVIDER_KEY"),
        ))
        self.assertEqual(admit_approved_route_plan(policy, wrong_credential, self.runtime_policy).disposition,
                         ApprovedRoutePlanAdmissionDisposition.DENIED_CREDENTIAL_REFERENCE_MISMATCH)

    def test_full_execution_binding_digest_drift_fails_closed_before_provider_activation(self) -> None:
        drifted = replace(self.integrate, egress_policy_digest="e" * 64)
        drifted_plan = MultiRouteJobPlan("d" * 64, (
            TaskRouteAssignment("explore", "employee-a", self.explore.digest),
            TaskRouteAssignment("integrate", "employee-b", drifted.digest, final=True),
        ), (), "employee-b")
        drifted_policy = MultiRouteRuntimePolicy(drifted_plan, (drifted, self.explore))
        admission = admit_approved_route_plan(
            UserRoutingPolicy(UserRoutingPolicyMode.BALANCED), self.registry, drifted_policy
        )
        self.assertEqual(
            admission.disposition,
            ApprovedRoutePlanAdmissionDisposition.DENIED_EXECUTION_ROUTE_BINDING_DIGEST_MISMATCH,
        )

    def test_duplicate_or_incomplete_coverage_and_untyped_inputs_are_rejected(self) -> None:
        duplicate_plan = MultiRouteJobPlan("d" * 64, (
            TaskRouteAssignment("explore", "employee-a", self.explore.digest),
            TaskRouteAssignment("integrate", "employee-b", self.explore.digest, final=True),
        ), (), "employee-b")
        # Simulate a malformed typed object received across an unsafe boundary;
        # the normal constructor itself correctly rejects this duplicate shape.
        duplicate_policy = object.__new__(MultiRouteRuntimePolicy)
        object.__setattr__(duplicate_policy, "plan", duplicate_plan)
        object.__setattr__(duplicate_policy, "bindings", (self.explore, self.explore))
        denied = admit_approved_route_plan(UserRoutingPolicy(UserRoutingPolicyMode.BALANCED), self.registry, duplicate_policy)
        self.assertEqual(denied.disposition, ApprovedRoutePlanAdmissionDisposition.DENIED_DUPLICATE_OR_INCOMPLETE_BINDING_COVERAGE)
        with self.assertRaises(TypeError):
            admit_approved_route_plan("BALANCED", self.registry, self.runtime_policy)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            admit_approved_route_plan(UserRoutingPolicy(UserRoutingPolicyMode.BALANCED), self.registry, object())  # type: ignore[arg-type]

    def test_direct_constructed_dto_conveys_no_runtime_policy_authority(self) -> None:
        forged = ApprovedRoutePlanAdmission(
            disposition=ApprovedRoutePlanAdmissionDisposition.ADMITTED_APPROVED_REUSE,
            policy_digest="a" * 64,
            registry_digest="b" * 64,
            runtime_policy_summary_digest="c" * 64,
            approved_binding_count=1,
        )
        self.assertTrue(forged.admitted)
        self.assertNotIn("approved_runtime_policy", forged.__dataclass_fields__)
        self.assertFalse(hasattr(forged, "require_runtime_policy"))
        with self.assertRaises(ValueError):
            require_fresh_approved_route_plan(
                UserRoutingPolicy(UserRoutingPolicyMode.BALANCED), ApprovedRouteRegistry(()), self.runtime_policy
            )

    def test_policy_switch_changes_only_admission_policy_identity_and_summary_is_content_free(self) -> None:
        first = admit_approved_route_plan(UserRoutingPolicy(UserRoutingPolicyMode.QUALITY_FIRST), self.registry, self.runtime_policy)
        switched = admit_approved_route_plan(UserRoutingPolicy(UserRoutingPolicyMode.EFFICIENT), self.registry, self.runtime_policy)
        self.assertNotEqual(first.policy_digest, switched.policy_digest)
        self.assertEqual(first.registry_digest, switched.registry_digest)
        self.assertEqual(first.runtime_policy_summary_digest, switched.runtime_policy_summary_digest)
        self.assertEqual(first.approved_binding_count, switched.approved_binding_count)
        self.assertEqual(set(first.canonical_payload()), {
            "disposition", "policy_digest", "registry_digest", "runtime_policy_summary_digest", "approved_binding_count",
        })
        self.assertNotIn("explore", first.canonical_summary())
        self.assertNotIn("PRIMARY_PROVIDER_KEY", first.canonical_summary())
        self.assertEqual(first.summary_digest, admit_approved_route_plan(
            UserRoutingPolicy(UserRoutingPolicyMode.QUALITY_FIRST), self.registry, self.runtime_policy
        ).summary_digest)
        denied = admit_approved_route_plan(
            UserRoutingPolicy(UserRoutingPolicyMode.QUALITY_FIRST), ApprovedRouteRegistry(()), self.runtime_policy
        )
        self.assertNotEqual(first.summary_digest, denied.summary_digest)

    def test_runtime_summary_binds_dependency_handoff_and_final_owner_identity(self) -> None:
        dependent_plan = MultiRouteJobPlan("d" * 64, (
            TaskRouteAssignment("explore", "employee-a", self.explore.digest),
            TaskRouteAssignment("integrate", "employee-b", self.integrate.digest, depends_on=("explore",), final=True),
        ), (DependencyArtifactHandoff("explore", "integrate", "e" * 64),), "employee-b")
        final_owner_drift = MultiRouteJobPlan("d" * 64, (
            TaskRouteAssignment("explore", "employee-a", self.explore.digest, final=True),
            TaskRouteAssignment("integrate", "employee-b", self.integrate.digest),
        ), (), "employee-a")
        self.assertNotEqual(
            self.runtime_policy.summary_digest,
            MultiRouteRuntimePolicy(dependent_plan, (self.explore, self.integrate)).summary_digest,
        )
        self.assertNotEqual(
            self.runtime_policy.summary_digest,
            MultiRouteRuntimePolicy(final_owner_drift, (self.explore, self.integrate)).summary_digest,
        )


if __name__ == "__main__":
    unittest.main()
