from __future__ import annotations

import unittest
from dataclasses import replace

from dynamic_firm.kernel.request_codec import (
    company_run_request_from_envelope,
    request_envelope_payload,
)
from dynamic_firm.kernel.models import EmployeeRecord
from dynamic_firm.kernel.mutation import frozen_snapshot_digest
from dynamic_firm.kernel.service import FirmKernel
from tests.kernel.helpers import company_request, task


_ROSTER = (EmployeeRecord("employee", "Analyst", ("analysis",)),)


class RequestCodecRuntimeBindingTests(unittest.TestCase):
    def test_runtime_bindings_survive_the_local_continuation_envelope(self) -> None:
        request = replace(
            company_request((task("only"),), final_task_id="only", roster=_ROSTER),
            runtime_provider_binding_digest="a" * 64,
            runtime_tool_contract_digest="b" * 64,
            runtime_company_coordination_digest="c" * 64,
        )

        restored = company_run_request_from_envelope(request_envelope_payload(request))

        self.assertEqual(restored, request)

    def test_historical_envelope_decodes_with_empty_fail_closed_bindings(self) -> None:
        request = company_request(
            (task("only"),), final_task_id="only", roster=_ROSTER
        )
        envelope = request_envelope_payload(request)
        payload = envelope["request"]
        assert isinstance(payload, dict)
        payload.pop("runtime_provider_binding_digest", None)
        payload.pop("runtime_tool_contract_digest", None)
        payload.pop("runtime_company_coordination_digest", None)

        restored = company_run_request_from_envelope(envelope)

        self.assertEqual(restored.runtime_provider_binding_digest, "")
        self.assertEqual(restored.runtime_tool_contract_digest, "")
        self.assertEqual(restored.runtime_company_coordination_digest, "")

    def test_kernel_rejects_a_malformed_nonempty_runtime_binding(self) -> None:
        request = replace(
            company_request(
                (task("only"),), final_task_id="only", roster=_ROSTER
            ),
            runtime_provider_binding_digest="A" * 64,
        )

        with self.assertRaisesRegex(ValueError, "provider binding digest"):
            FirmKernel._validate_request(request)

    def test_kernel_rejects_a_malformed_coordination_binding(self) -> None:
        request = replace(
            company_request(
                (task("only"),), final_task_id="only", roster=_ROSTER
            ),
            runtime_company_coordination_digest="not-a-digest",
        )

        with self.assertRaisesRegex(ValueError, "Company coordination digest"):
            FirmKernel._validate_request(request)

    def test_new_runtime_bindings_are_part_of_the_frozen_snapshot(self) -> None:
        baseline = replace(
            company_request(
                (task("only"),), final_task_id="only", roster=_ROSTER
            ),
            runtime_provider_binding_digest="a" * 64,
            runtime_tool_contract_digest="b" * 64,
            runtime_company_coordination_digest="c" * 64,
        )

        self.assertNotEqual(
            frozen_snapshot_digest(baseline),
            frozen_snapshot_digest(
                replace(baseline, runtime_company_coordination_digest="d" * 64)
            ),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
