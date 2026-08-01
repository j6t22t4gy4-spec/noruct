from __future__ import annotations

import asyncio
import io
import os
import sys
import unittest
from pathlib import Path

from dynamic_firm.cli import main
from dynamic_firm.foundation.parity import (
    run_foundation_parallelism_parity,
    run_foundation_reroute_parity,
    run_deferred_tool_discovery_parity,
    run_preview_parity,
    run_product_mcp_preview_parity,
    run_product_preview_parity,
    run_runtime_reliability_qualification,
)


def _worker_python() -> str | None:
    for candidate in (os.environ.get("NORUCT_EMPLOYEE_RUNTIME_PYTHON", ""), sys.executable):
        if candidate and os.access(candidate, os.X_OK):
            import subprocess
            if subprocess.run([candidate, "-c", "import yaml"], capture_output=True).returncode == 0:
                return candidate
    return None


@unittest.skipUnless(_worker_python(), "H2 runtime dependency unavailable")
class PreviewParityTests(unittest.TestCase):
    def test_bounded_preview_matrix_preserves_parent_authority(self) -> None:
        result = asyncio.run(run_preview_parity(python_executable=str(_worker_python())))
        self.assertTrue(result["passed"])
        self.assertEqual(result["external_model_calls"], 0)
        self.assertEqual(result["scenarios"]["parent_tool"]["tool_intents"], 1)
        self.assertEqual(result["scenarios"]["approval"]["approval_events"], 1)
        self.assertTrue(result["scenarios"]["approval_denied"]["passed"])
        self.assertTrue(result["scenarios"]["approval_denied"]["workspace_unchanged"])
        self.assertEqual(result["scenarios"]["cancel"]["cancel_events"], 1)

    def test_reliability_qualification_composes_runtime_and_product_contracts(self) -> None:
        result = asyncio.run(
            run_runtime_reliability_qualification(
                python_executable=str(_worker_python())
            )
        )

        self.assertTrue(result["passed"])
        self.assertTrue(all(result["checks"].values()))
        self.assertEqual(result["external_model_calls"], 0)
        self.assertIn("approval compare-and-swap", result["scope"]["covered_by_durable_store_regression"])
        self.assertTrue(result["checks"]["deferred_tool_discovery_stays_parent_authorized"])

    def test_deferred_tool_discovery_uses_full_core_but_parent_tool_authority(self) -> None:
        result = asyncio.run(
            run_deferred_tool_discovery_parity(
                python_executable=str(_worker_python())
            )
        )

        self.assertTrue(result["passed"])
        self.assertEqual(result["resolved_calls"], ["target_capability"])
        self.assertEqual(result["parent_tool_actions"], ["target_capability"])
        self.assertNotIn("target_capability", result["first_surface"])
        self.assertTrue(
            {"tool_search", "tool_describe", "tool_call"} <= set(result["first_surface"])
        )

    def test_product_preview_parity_preserves_firm_kernel_and_one_answer_writer(self) -> None:
        result = asyncio.run(run_product_preview_parity(python_executable=str(_worker_python())))

        self.assertTrue(result["passed"])
        self.assertEqual(result["external_model_calls"], 0)
        self.assertEqual(result["scenarios"]["direct_conversation"], "SUCCEEDED")
        self.assertEqual(result["scenarios"]["resumed_direct_conversation"], "SUCCEEDED")
        self.assertEqual(result["scenarios"]["single_task_company_goal"], "SUCCEEDED")
        self.assertEqual(result["scenarios"]["typed_capability_admission"], "SUCCEEDED")
        self.assertEqual(result["scenarios"]["approved_workspace_write"], "SUCCEEDED")
        self.assertEqual(result["scenarios"]["approved_workspace_edit"], "SUCCEEDED")
        self.assertEqual(result["scenarios"]["approved_workspace_command"], "SUCCEEDED")
        self.assertEqual(
            result["scenarios"]["approved_workspace_read_then_write"], "SUCCEEDED"
        )
        self.assertEqual(
            result["scenarios"]["approved_workspace_command_edit_verify"],
            "SUCCEEDED",
        )
        self.assertEqual(
            result["scenarios"]["company_budget_pre_dispatch_hard_stop"],
            "BUDGET_EXHAUSTED",
        )
        self.assertTrue(result["single_answer_writer"])
        self.assertIn("JOB_FINISHED", result["observed_product_events"])

    def test_foundation_reroute_parity_keeps_staffing_authority_in_kernel(self) -> None:
        result = asyncio.run(
            run_foundation_reroute_parity(python_executable=str(_worker_python()))
        )

        self.assertTrue(result["passed"])
        self.assertEqual(result["status"], "SUCCEEDED")
        self.assertEqual(result["mutation_types"], ("REROUTE",))
        self.assertEqual(result["maximum_parallelism"], 1)

    def test_foundation_parallelism_parity_joins_only_after_two_ready_tasks(self) -> None:
        result = asyncio.run(
            run_foundation_parallelism_parity(
                python_executable=str(_worker_python())
            )
        )

        self.assertTrue(result["passed"])
        self.assertTrue(result["parallel_starts_before_release"])
        self.assertEqual(result["maximum_parallelism"], 2)
        self.assertEqual(
            result["final_dependency_ids"],
            ("task-result:analysis-a", "task-result:analysis-b"),
        )

    @unittest.skipUnless(
        os.environ.get("NORUCT_MCP_SDK_PYTHON"),
        "set NORUCT_MCP_SDK_PYTHON to an audited mcp==1.28.1 Python",
    )
    def test_product_mcp_preview_parity_preserves_parent_tool_and_answer_boundaries(self) -> None:
        result = asyncio.run(
            run_product_mcp_preview_parity(
                python_executable=str(_worker_python()),
                mcp_python=os.environ["NORUCT_MCP_SDK_PYTHON"],
                server_script=Path(__file__).parents[1]
                / "fixtures"
                / "mcp_read_only_server.py",
            )
        )

        self.assertTrue(result["passed"])
        self.assertEqual(result["external_model_calls"], 0)
        self.assertEqual(
            result["scenarios"]["user_managed_multi_external_read"], "SUCCEEDED"
        )
        self.assertTrue(result["single_answer_writer"])
        self.assertIn("CAPABILITY_READY", result["observed_product_events"])

    def test_cli_readiness_reports_the_default_runtime_without_release_authorization(self) -> None:
        import json

        output = io.StringIO()
        self.assertEqual(
            main(["foundation", "readiness", "--runtime-python", str(_worker_python()), "--json"], stdout=output),
            0,
        )
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["execution"], "runtime_default_readiness")
        self.assertEqual(payload["default_runtime"], "noruct")
        self.assertTrue(payload["technical_default_ready"])
        self.assertTrue(payload["parity"]["passed"])
        self.assertTrue(payload["product_parity"]["passed"])
        self.assertTrue(payload["reroute_parity"]["passed"])
        self.assertTrue(payload["parallelism_parity"]["passed"])
        self.assertIn("employee_runtime", payload["scope"]["assessed"])
        self.assertIn("product_integration", payload["scope"]["assessed"])
        self.assertIn(
            "direct/resumed conversation, single-task and one typed-capability Company path, approved workspace write/edit/command paths, one read-then-approved-write tool iteration, one command-edit-verify coding loop, a Company budget pre-dispatch hard stop, one sequential typed assignee-mismatch reroute to a frozen exact-capable employee, and one two-task dependency-ready parallel join through the Foundation Runtime",
            payload["scope"]["partially_assessed"]["firm_kernel"],
        )
        self.assertEqual(
            payload["scope"]["not_assessed"]["paperclip_derived_control_plane"],
            "not_assessed_by_employee_runtime_readiness",
        )
        self.assertFalse(
            payload["scope"]["shared_evolution_network"]
            ["assessed_by_employee_runtime_readiness"]
        )
        self.assertEqual(
            payload["scope"]["shared_evolution_network"]["current_fail_closed_gate"]
            ["hosted_transport"],
            "IMPLEMENTED_EXPLICIT_OPT_IN_NOT_AUTO_ACTIVATED",
        )

    def test_cli_reliability_reports_clear_offline_scope(self) -> None:
        import json

        output = io.StringIO()
        self.assertEqual(
            main(
                [
                    "foundation",
                    "reliability",
                    "--runtime-python",
                    str(_worker_python()),
                    "--json",
                ],
                stdout=output,
            ),
            0,
        )
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["passed"])
        self.assertIn("approval_denial_leaves_workspace_unchanged", payload["checks"])
        self.assertIn("live provider/authentication", payload["scope"]["not_assessed"])
