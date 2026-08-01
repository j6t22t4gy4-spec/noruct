from __future__ import annotations

import unittest

from dynamic_firm.kernel.models import EmployeeRecord, JobStatus, TaskMutationType
from dynamic_firm.kernel.service import FirmKernel
from dynamic_firm.providers.fake import ScriptedModelProvider
from dynamic_firm.runtime.models import (
    CompletionEnvelope,
    ModelResponse,
    RunSignal,
    SignalCode,
)
from dynamic_firm.runtime.service import NativeEmployeeRuntimeService
from dynamic_firm.runtime.store import RunStore
from dynamic_firm.runtime.tools import ToolRegistry
from tests.kernel.helpers import company_request, task


class TaskMutationIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_employee_mismatch_reaches_kernel_reroute_and_downstream(self) -> None:
        store = RunStore()
        provider = ScriptedModelProvider(
            [
                ModelResponse(
                    completion=CompletionEnvelope(
                        summary="This assignment needs another eligible analyst.",
                        signals=(
                            RunSignal(
                                SignalCode.ASSIGNEE_MISMATCH,
                                "analysis",
                                ("typed:mismatch",),
                            ),
                        ),
                    )
                ),
                ModelResponse(
                    completion=CompletionEnvelope(
                        summary="The reassigned analysis succeeded.",
                        acceptance_evidence=("analysis:evidence",),
                    )
                ),
                ModelResponse(
                    completion=CompletionEnvelope(
                        summary="The final integration succeeded.",
                        acceptance_evidence=("final:evidence",),
                    )
                ),
            ]
        )
        service = NativeEmployeeRuntimeService(
            store=store,
            provider=provider,
            registry=ToolRegistry(),
        )
        request = company_request(
            (
                task("analysis"),
                task("final", depends_on=("analysis",), capabilities=("integration",)),
            ),
            final_task_id="final",
            roster=(
                EmployeeRecord("analyst-a", "Analyst A", ("analysis",)),
                EmployeeRecord("analyst-b", "Analyst B", ("analysis",)),
                EmployeeRecord("integrator", "Integrator", ("integration",)),
            ),
        )

        result = await FirmKernel(employee_execution=service).run(request)
        runs = store.list_job_runs(request.job_id)

        self.assertEqual(result.status, JobStatus.SUCCEEDED)
        self.assertEqual(provider.call_count, 3)
        self.assertEqual(
            [item.mutation_type for item in result.mutation_events],
            [TaskMutationType.REROUTE],
        )
        self.assertEqual(
            [item["employee_id"] for item in runs],
            ["analyst-a", "analyst-b", "integrator"],
        )
        self.assertEqual(
            [item["status"] for item in runs],
            ["FAILED", "SUCCEEDED", "SUCCEEDED"],
        )
        store.close()


if __name__ == "__main__":
    unittest.main()
