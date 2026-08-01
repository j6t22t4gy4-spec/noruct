from __future__ import annotations

import unittest
from types import SimpleNamespace

from dynamic_firm.runtime.job_inspector_delivery import (
    safe_final_task_capabilities,
    validation_receipts,
)
from dynamic_firm.runtime.models import EventType


class JobInspectorDeliveryTests(unittest.TestCase):
    def test_final_capability_projection_rejects_ambiguous_or_oversized_values(self) -> None:
        errors: list[str] = []
        self.assertEqual(
            safe_final_task_capabilities(
                {
                    "tasks": (
                        {
                            "task_id": "final",
                            "required_capabilities": ("implementation", "review"),
                        },
                    )
                },
                final_task_id="final",
                errors=errors,
            ),
            ("implementation", "review"),
        )
        self.assertEqual(errors, [])
        self.assertEqual(
            safe_final_task_capabilities(
                {"tasks": ({"task_id": "final", "required_capabilities": ("x" * 65,)},)},
                final_task_id="final",
                errors=errors,
            ),
            (),
        )
        self.assertIn("final task capabilities invalid", errors)

    def test_validation_projection_keeps_name_and_pass_state_only(self) -> None:
        class Store:
            def list_job_runs(self, job_id):
                self.assertEqual(job_id, "job-1")
                return ({"run_id": "run-1", "task_id": "final", "employee_id": "coder"},)

            def list_events(self, run_id, after_seq):
                self.assertEqual((run_id, after_seq), ("run-1", 0))
                return (
                    SimpleNamespace(
                        type=EventType.VALIDATION_RECORDED,
                        payload={"name": "pytest", "passed": True, "detail": "must not project"},
                    ),
                    SimpleNamespace(
                        type=EventType.MODEL_CALL_COMPLETED,
                        payload={"name": "not-a-validation", "passed": False},
                    ),
                )

            def assertEqual(self, left, right):
                unittest.TestCase().assertEqual(left, right)

        projected = validation_receipts(Store(), "job-1")
        self.assertEqual(
            projected,
            ({"task_id": "final", "employee_id": "coder", "name": "pytest", "status": "PASSED"},),
        )
        self.assertNotIn("detail", repr(projected))


if __name__ == "__main__":
    unittest.main()
