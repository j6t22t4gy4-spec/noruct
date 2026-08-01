from __future__ import annotations

import unittest
from dataclasses import replace

from dynamic_firm.runtime.employee_capability import (
    build_employee_capability_profile,
    material_profile_difference,
    materially_equivalent,
)
from dynamic_firm.runtime.models import (
    ActionPolicy,
    EmployeeSessionRetention,
    ToolEffect,
    ToolGrant,
    VersionedContent,
)


class EmployeeCapabilityProfileTests(unittest.TestCase):
    def _profile(self, employee_id: str, **overrides):
        values = {
            "employee_id": employee_id,
            "roster_revision": 3,
            "model_profile": "model-a",
            "capabilities": ("analysis",),
            "skills": (),
            "action_policy": ActionPolicy(
                tool_grants=(ToolGrant("read", (ToolEffect.READ,)),)
            ),
            "task_evidence": None,
            "memory_namespace": f"employee:{employee_id}",
            "selected_memory": (),
            "session_retention": EmployeeSessionRetention.PERSIST,
            "validator_ids": ("structured-completion-v1",),
        }
        values.update(overrides)
        return build_employee_capability_profile(**values)

    def test_identity_only_clones_share_one_material_digest(self) -> None:
        first = self._profile("analyst-a")
        second = self._profile("analyst-b")

        self.assertNotEqual(first.profile_digest, second.profile_digest)
        self.assertEqual(first.material_digest, second.material_digest)
        self.assertTrue(materially_equivalent(first, second))
        self.assertEqual(material_profile_difference(first, second), ())

    def test_real_runtime_surface_changes_are_material(self) -> None:
        baseline = self._profile("analyst-a")
        changed = self._profile(
            "analyst-b",
            model_profile="model-b",
            skills=(VersionedContent("skill", "2", "procedure"),),
            action_policy=ActionPolicy(
                tool_grants=(ToolGrant("write", (ToolEffect.WRITE,)),),
                filesystem_policy="WORKSPACE_WRITE",
            ),
            selected_memory=(VersionedContent("employee-memory:analyst-b:fact", "4", "fact"),),
            validator_ids=("independent-review-v1", "structured-completion-v1"),
        )

        difference = material_profile_difference(baseline, changed)

        self.assertFalse(materially_equivalent(baseline, changed))
        self.assertIn("model_profile", difference)
        self.assertIn("skill_revision_refs", difference)
        self.assertIn("tool_grant_digest", difference)
        self.assertIn("permission_digest", difference)
        self.assertIn("memory_revision_refs", difference)
        self.assertIn("validator_ids", difference)

    def test_private_state_namespace_is_material_only_when_memory_is_selected(self) -> None:
        shared = VersionedContent("company-memory:shared", "1", "shared fact")
        first = self._profile("analyst-a", selected_memory=(shared,))
        second = self._profile("analyst-b", selected_memory=(shared,))

        self.assertNotEqual(first.material_digest, second.material_digest)
        self.assertIn("memory_namespace", material_profile_difference(first, second))

    def test_tampered_profile_fails_closed(self) -> None:
        profile = self._profile("analyst-a")
        with self.assertRaisesRegex(ValueError, "exact digest mismatch"):
            replace(profile, model_profile="different-model").verify()


if __name__ == "__main__":
    unittest.main()
