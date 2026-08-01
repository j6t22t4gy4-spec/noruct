from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from dynamic_firm.cli import EXIT_RUNTIME, main
from dynamic_firm.foundation.source import (
    EMPLOYEE_ACTIVE_FORK_TREE_SHA256,
    EMPLOYEE_FOUNDATION_COMMIT,
    EMPLOYEE_FOUNDATION_TREE_SHA256,
    EMPLOYEE_FOUNDATION_VERSION,
    foundation_cutover_status,
    foundation_preview_preflight,
    foundation_status,
    run_foundation_smoke,
    verify_employee_runtime_capsule,
    verify_foundation_source,
)
from dynamic_firm.foundation.inventory import foundation_capability_inventory


PROJECT_ROOT = Path(__file__).parents[2]
VENDOR_ROOT = PROJECT_ROOT / "src" / "dynamic_firm" / "_vendor" / "hermes_agent"
CAPSULE_ROOT = (
    PROJECT_ROOT / "src" / "dynamic_firm" / "_vendor" / "employee_runtime_capsule"
)


class EmployeeFoundationTests(unittest.TestCase):
    def test_exact_pinned_source_tree_is_complete_and_hash_sealed(self) -> None:
        source = verify_foundation_source()

        self.assertEqual(source["source_commit"], EMPLOYEE_FOUNDATION_COMMIT)
        self.assertEqual(source["upstream_version"], EMPLOYEE_FOUNDATION_VERSION)
        self.assertEqual(source["file_count"], 878)
        self.assertEqual(source["source_bytes"], 25_920_089)
        self.assertEqual(source["tree_sha256"], EMPLOYEE_ACTIVE_FORK_TREE_SHA256)
        self.assertEqual(source["license"], "MIT")

    def test_full_source_inventory_classifies_every_vendored_file(self) -> None:
        inventory = foundation_capability_inventory()

        self.assertTrue(inventory["complete_source_intake"])
        self.assertEqual(inventory["source_file_count"], 878)
        self.assertEqual(inventory["verified_source_file_count"], 878)
        self.assertEqual(
            sum(item["source_file_count"] for item in inventory["families"]),
            878,
        )
        families = {item["family"]: item for item in inventory["families"]}
        self.assertIn("employee-core", families)
        self.assertIn("gateway-and-channels", families)
        self.assertIn("editor-protocol", families)

    def test_cli_inventory_does_not_start_a_worker_or_load_product_config(self) -> None:
        output = io.StringIO()

        self.assertEqual(main(["foundation", "inventory", "--json"], stdout=output), 0)
        inventory = json.loads(output.getvalue())
        self.assertTrue(inventory["complete_source_intake"])
        self.assertEqual(inventory["authority"]["company_state"], "noruct")

    def test_product_capsule_is_hash_sealed_and_bound_to_the_h1_baseline(self) -> None:
        capsule = verify_employee_runtime_capsule()

        self.assertEqual(capsule["source_commit"], EMPLOYEE_FOUNDATION_COMMIT)
        self.assertEqual(capsule["upstream_version"], EMPLOYEE_FOUNDATION_VERSION)
        self.assertEqual(capsule["file_count"], 107)
        self.assertEqual(capsule["source_bytes"], 2_493_854)
        self.assertEqual(capsule["secondary_provenance_marker_count"], 2)
        self.assertEqual(
            capsule["tree_sha256"],
            "d97c040bcf8182d4c29688f4c618ae38e75bcebd60f6dd89d9ed602a2b34c2bc",
        )
        self.assertEqual(
            capsule["development_baseline_tree_sha256"],
            EMPLOYEE_FOUNDATION_TREE_SHA256,
        )
        self.assertEqual(capsule["license"], "MIT")
        self.assertTrue((CAPSULE_ROOT / "LICENSE").is_file())

    def test_manifest_maps_every_copy_and_excludes_unreviewed_assets(self) -> None:
        manifest = json.loads(
            (VENDOR_ROOT / "UPSTREAM_MANIFEST.json").read_text(encoding="utf-8")
        )
        policy = json.loads(
            (VENDOR_ROOT / "VENDOR_POLICY.json").read_text(encoding="utf-8")
        )
        paths = {item["upstream_path"] for item in manifest["files"]}

        self.assertTrue(
            all(item["treatment"] in {"exact_copy", "adapted"} for item in manifest["files"])
        )
        self.assertEqual(
            [item["upstream_path"] for item in manifest["files"] if item["treatment"] == "adapted"],
            [
                "hermes_cli/cli_agent_setup_mixin.py",
                "noruct_firm/__init__.py",
                "noruct_firm/agent.py",
                "noruct_firm/context.py",
                "noruct_firm/entrypoint.py",
                "noruct_firm/fork_cli.py",
                "noruct_firm/session.py",
                "run_agent.py",
            ],
        )
        self.assertFalse(any(path.startswith("plugins/hermes-achievements/") for path in paths))
        self.assertFalse(any(path.startswith("plugins/security-guidance/") for path in paths))
        self.assertFalse(
            any(Path(path).suffix.lower() in {".gif", ".ico", ".jpeg", ".jpg", ".mp3", ".png", ".svg", ".ttf", ".wav", ".webp", ".woff", ".woff2"} for path in paths)
        )
        self.assertFalse(any("__pycache__" in Path(path).parts for path in paths))
        self.assertFalse(any(Path(path).suffix == ".pyc" for path in paths))
        self.assertIn("upstream_commands_and_product_identity", policy["excluded_surfaces"])

    def test_status_preserves_noruct_identity_and_default_runtime(self) -> None:
        status = foundation_status()

        self.assertTrue(status["source_ready"])
        self.assertEqual(status["product_identity"], "noruct")
        self.assertEqual(status["state_authority"], "noruct")
        self.assertEqual(status["activation"], "noruct_runtime_default")
        self.assertEqual(status["default_runtime"], "noruct")
        self.assertEqual(status["employee_execution_port"], "noruct.employee.v2")
        self.assertEqual(status["dependencies"]["selected_extras"], [])
        self.assertEqual(status["dependencies"]["direct_requirement_count"], 1)
        self.assertEqual(status["dependencies"]["exact_package_count"], 1)
        self.assertFalse(status["dependencies"]["commercial_release_approved"])
        self.assertEqual(
            status["source_qualification_dependencies"]["selected_extras"],
            ["cli", "mcp"],
        )

    def test_cutover_keeps_legal_review_advisory_without_an_alternate_runtime(self) -> None:
        cutover = foundation_cutover_status()

        self.assertEqual(cutover["default_runtime"], "noruct")
        self.assertIsNone(cutover["rollback_runtime"])
        self.assertFalse(cutover["runtime_rollback_available"])
        self.assertEqual(
            cutover["historical_state_compatibility"]["label"],
            "historical_employee_state",
        )
        self.assertEqual(
            cutover["default_runtime_eligible"],
            bool(foundation_status()["dependencies"]["ready"]),
        )
        self.assertFalse(cutover["commercial_release_approved"])
        self.assertEqual(cutover["gate"]["dependency"]["state"], "advisory_closed")
        self.assertEqual(len(cutover["gate"]["dependency"]["license_review_blockers"]), 0)
        self.assertEqual(cutover["gate"]["secondary_provenance"]["state"], "advisory_open")
        self.assertFalse(cutover["legal_review"]["affects_runtime_selection"])
        self.assertEqual(
            cutover["gate"]["secondary_provenance"]["distributed_source_finding_count"],
            0,
        )
        self.assertEqual(
            cutover["gate"]["secondary_provenance"]["development_baseline_finding_count"],
            60,
        )
        self.assertEqual(
            cutover["gate"]["secondary_provenance"]["active_import_surface_finding_count"],
            4,
        )

    def test_cli_status_and_source_verification_do_not_load_product_config(self) -> None:
        status_output = io.StringIO()
        verify_output = io.StringIO()

        self.assertEqual(main(["foundation", "status", "--json"], stdout=status_output), 0)
        self.assertEqual(main(["foundation", "verify-source"], stdout=verify_output), 0)

        status = json.loads(status_output.getvalue())
        self.assertEqual(
            status["source"]["development_baseline_tree_sha256"],
            EMPLOYEE_FOUNDATION_TREE_SHA256,
        )
        self.assertEqual(status["source"]["file_count"], 878)
        self.assertIn("878 files", verify_output.getvalue())

    def test_cli_cutover_status_exposes_advisories_without_loading_product_config(self) -> None:
        output = io.StringIO()

        self.assertEqual(main(["foundation", "cutover-status", "--json"], stdout=output), 0)

        status = json.loads(output.getvalue())
        self.assertEqual(status["default_runtime"], "noruct")
        self.assertEqual(
            status["default_runtime_eligible"],
            bool(foundation_status()["dependencies"]["ready"]),
        )
        self.assertEqual(status["gate"]["dependency"]["state"], "advisory_closed")

    def test_cli_validates_explicit_provenance_record_without_activation(self) -> None:
        output = io.StringIO()
        packet = PROJECT_ROOT / "docs" / "60-governance" / "shipped-capsule-secondary-provenance-review-packet.json"
        decisions = PROJECT_ROOT / "docs" / "60-governance" / "release-approvals" / "shipped-capsule-provenance-review.project-owner-2026-07-19.json"

        self.assertEqual(
            main(
                [
                    "foundation", "validate-provenance-review",
                    "--packet", str(packet),
                    "--decisions", str(decisions), "--json",
                ],
                stdout=output,
            ),
            0,
        )
        result = json.loads(output.getvalue())
        self.assertTrue(result["review_complete"])
        self.assertFalse(result["commercial_release_authorized"])
        self.assertFalse(result["commercial_default_activation"])

    def test_release_admission_can_project_explicit_completed_provenance_without_activation(self) -> None:
        output = io.StringIO()
        root = PROJECT_ROOT / "docs"
        direct = root / "50-mvp/evaluations/h2-96-provider-direct-slot.json"
        read_tool = root / "50-mvp/evaluations/h2-96-provider-read-tool-slot.json"
        approval = root / "50-mvp/evaluations/h2-96-provider-approval-slot.json"
        cancel = root / "50-mvp/evaluations/h2-96-provider-cancel-recovery-slot.json"
        packet = root / "60-governance/shipped-capsule-secondary-provenance-review-packet.json"
        decisions = root / "60-governance/release-approvals/shipped-capsule-provenance-review.project-owner-2026-07-19.json"

        self.assertEqual(
            main(
                [
                    "foundation", "release-admission-status",
                    "--direct", str(direct), "--read-tool", str(read_tool),
                    "--approval", str(approval), "--cancel-recovery", str(cancel),
                    "--provenance-packet", str(packet),
                    "--provenance-decisions", str(decisions), "--json",
                ],
                stdout=output,
            ),
            0,
        )
        result = json.loads(output.getvalue())
        self.assertEqual(result["provenance_review"]["state"], "REVIEW_RECORD_VALIDATED")
        self.assertTrue(result["provenance_review"]["review_complete"])
        self.assertFalse(result["release_authorized"])
        self.assertFalse(result["commercial_default_eligible"])

    def test_isolated_runtime_smoke_when_runtime_dependency_exists(self) -> None:
        status = foundation_status()
        if not status["dependency_ready"]:
            self.skipTest("H2 employee runtime dependency is not installed in this interpreter")

        result = run_foundation_smoke(timeout_seconds=90)

        self.assertTrue(result["ok"])
        self.assertEqual(result["upstream_version"], EMPLOYEE_FOUNDATION_VERSION)
        self.assertEqual(result["agent_class"], "AIAgent")
        self.assertEqual(result["fork_file_count"], 878)
        self.assertEqual(result["worker_protocol"], "noruct.employee.v2")
        self.assertTrue(result["parent_authority"])
        self.assertEqual(result["model_request_count"], 1)
        self.assertGreaterEqual(result["text_delta_count"], 1)
        self.assertEqual(result["final_response"], "foundation smoke passed")
        self.assertEqual(result["tool_count"], 0)
        self.assertEqual(result["network_calls"], 0)
        self.assertTrue(result["home_isolated"])
        self.assertFalse(result["session_database"])

    def test_cli_preflight_runs_the_selected_employee_worker_python(self) -> None:
        status = foundation_status()
        if not status["dependency_ready"]:
            self.skipTest("H2 employee runtime dependency is not installed in this interpreter")

        output = io.StringIO()
        self.assertEqual(
            main(
                [
                    "foundation",
                    "preflight",
                    "--runtime-python",
                    sys.executable,
                    "--json",
                ],
                stdout=output,
            ),
            0,
        )
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["schema_version"], "noruct.employee-runtime-preflight.v1")
        self.assertEqual(payload["execution"], "runtime_default_readiness")
        self.assertFalse(payload["commercial_default_eligible"])
        self.assertEqual(payload["worker"]["worker_python"], sys.executable)
        self.assertEqual(payload["worker"]["worker_protocol"], "noruct.employee.v2")

    def test_preflight_reports_default_runtime_readiness_separately_from_release(self) -> None:
        status = foundation_status()
        if not status["dependency_ready"]:
            self.skipTest("H2 employee runtime dependency is not installed in this interpreter")

        preflight = foundation_preview_preflight(python_executable=sys.executable)

        self.assertTrue(preflight["ok"])
        self.assertEqual(preflight["external_model_calls"], 0)
        self.assertEqual(preflight["network_access"], "denied_in_worker")
        self.assertEqual(preflight["cutover"]["default_runtime"], "noruct")
        self.assertTrue(preflight["technical_default_ready"])
        self.assertFalse(preflight["commercial_default_eligible"])

    def test_cli_preflight_fails_closed_when_selected_worker_lacks_runtime_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            environment = Path(directory) / "empty-worker"
            created = subprocess.run(
                [sys.executable, "-m", "venv", str(environment)],
                check=False,
                capture_output=True,
                timeout=30,
            )
            if created.returncode != 0:
                self.skipTest("current Python cannot create an isolated venv")
            output = io.StringIO()
            error = io.StringIO()

            exit_code = main(
                [
                    "foundation",
                    "preflight",
                    "--runtime-python",
                    str(environment / "bin" / "python"),
                    "--timeout",
                    "30",
                ],
                stdout=output,
                stderr=error,
            )

        self.assertEqual(exit_code, EXIT_RUNTIME)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("repair the Noruct installation", error.getvalue())


if __name__ == "__main__":
    unittest.main()
