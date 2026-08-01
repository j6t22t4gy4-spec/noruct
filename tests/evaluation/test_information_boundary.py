from __future__ import annotations

import asyncio
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from dynamic_firm import __version__
from dynamic_firm.cli import EXIT_OK, main
from dynamic_firm.evaluation.information_boundary import (
    INFORMATION_BOUNDARY_PREFLIGHT_SCHEMA,
    InformationBoundaryCase,
    create_information_boundary_preflight,
    load_information_boundary_preflight,
    materialize_information_boundary_fixture,
    run_information_boundary_benchmark,
    score_information_boundary_artifact,
)


def _write_source_root(root: Path) -> Path:
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "runtime.py").write_text("VALUE = 3\n", encoding="utf-8")
    (root / "tests" / "test_runtime.py").write_text(
        "def test_value(): pass\n",
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text(
        "[project]\nname='noruct'\n",
        encoding="utf-8",
    )
    return root


def _write_wheel(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            f"noruct-{__version__}.dist-info/METADATA",
            "Metadata-Version: 2.4\n"
            "Name: noruct\n"
            f"Version: {__version__}\n",
        )
    return path


class InformationBoundaryBenchmarkTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_four_provider_free_trajectories_pass(self) -> None:
        report = await run_information_boundary_benchmark()

        self.assertTrue(report.passed)
        self.assertTrue(report.ready_for_live_control_pair)
        self.assertEqual(report.external_provider_calls, 0)
        self.assertFalse(report.quota_consumed)
        self.assertEqual(
            tuple(record.case for record in report.records),
            tuple(InformationBoundaryCase),
        )
        by_case = {record.case: record for record in report.records}
        obvious = by_case[InformationBoundaryCase.OBVIOUS_SOLO]
        recovery = by_case[InformationBoundaryCase.SAME_WORKER_RECOVERY]
        boundary = by_case[InformationBoundaryCase.TYPED_INFORMATION_BOUNDARY]
        refusal = by_case[InformationBoundaryCase.INVALID_DUPLICATE_REFUSAL]
        self.assertEqual(obvious.admission.compiler_model_calls, 0)
        self.assertEqual(obvious.admission.organization_admission_count, 0)
        self.assertEqual(recovery.trajectory.task_mutation_count, 1)
        self.assertEqual(recovery.admission.organization_admission_count, 0)
        self.assertEqual(boundary.admission.organization_admission_count, 1)
        self.assertEqual(boundary.admission.final_graph_version, 2)
        self.assertEqual(boundary.trajectory.final_task_id, "integrate_goal")
        self.assertEqual(boundary.safety.final_writer_count, 1)
        self.assertTrue(boundary.safety.employee_memory_isolated)
        self.assertEqual(boundary.artifact_quality_gain, 0.4)
        self.assertIsNotNone(boundary.counterfactual)
        self.assertEqual(
            boundary.identity.workload_hash,
            boundary.counterfactual.workload_hash,
        )
        self.assertNotEqual(boundary.identity.run_id, boundary.counterfactual.run_id)
        self.assertEqual(
            refusal.admission.decision_reasons,
            ("CAPABILITY_INVALID", "CAPABILITY_ALREADY_ASSIGNED"),
        )

    async def test_artifact_scorer_has_no_topology_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = materialize_information_boundary_fixture(
                Path(directory) / "workspace"
            )
            (workspace / "REPORT.md").write_text(
                "decision=manual-review\n"
                "public_evidence=rollback-ready\n"
                "sealed_evidence=risk-9-threshold-7\n",
                encoding="utf-8",
            )
            score = score_information_boundary_artifact(workspace)

        self.assertTrue(score.passed)
        self.assertEqual(score.quality_score, 1.0)
        self.assertEqual(
            tuple(score_information_boundary_artifact.__annotations__),
            ("workspace", "return"),
        )

    async def test_preflight_seals_source_wheel_identity_and_refuses_tamper(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "preflight-v3.json"
            preflight = await create_information_boundary_preflight(
                output,
                wheel=_write_wheel(root / f"noruct-{__version__}-py3-none-any.whl"),
                source_root=_write_source_root(root / "source"),
                reserved_model_profile="cheap-live-model",
            )
            loaded = load_information_boundary_preflight(output)
            payload = json.loads(output.read_text(encoding="utf-8"))
            payload["reserved_model_profile"] = "tampered-model"
            output.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "content hash changed"):
                load_information_boundary_preflight(output)

        self.assertTrue(preflight.ready)
        self.assertEqual(preflight.external_provider_calls, 0)
        self.assertFalse(preflight.quota_consumed)
        self.assertEqual(loaded["schema_version"], INFORMATION_BOUNDARY_PREFLIGHT_SCHEMA)
        self.assertTrue(str(loaded["source_revision"]).startswith("snapshot-sha256:"))
        self.assertEqual(len(str(loaded["distribution_sha256"])), 64)

    async def test_cli_runs_self_test_and_creates_preflight(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        exit_code = await asyncio.to_thread(
            main,
            ["eval", "information-boundary", "--json"],
            stdout=output,
            stderr=error,
        )
        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, EXIT_OK)
        self.assertTrue(payload["passed"])
        self.assertEqual(len(payload["records"]), 4)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            preflight_path = root / "preflight.json"
            output = io.StringIO()
            error = io.StringIO()
            exit_code = await asyncio.to_thread(
                main,
                [
                    "eval",
                    "information-boundary",
                    "--create-preflight",
                    str(preflight_path),
                    "--wheel",
                    str(
                        _write_wheel(
                            root / f"noruct-{__version__}-py3-none-any.whl"
                        )
                    ),
                    "--source-root",
                    str(_write_source_root(root / "source")),
                    "--model",
                    "cheap-live-model",
                    "--json",
                ],
                stdout=output,
                stderr=error,
            )
            preflight = json.loads(output.getvalue())

        self.assertEqual(exit_code, EXIT_OK)
        self.assertTrue(preflight["ready"])
        self.assertEqual(preflight["external_provider_calls"], 0)
        self.assertFalse(preflight["quota_consumed"])


if __name__ == "__main__":
    unittest.main()
