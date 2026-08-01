from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dynamic_firm import __version__
from dynamic_firm.cli import EXIT_OK, main
from dynamic_firm.evaluation.closed_loop import run_closed_loop_evaluation
from dynamic_firm.evaluation.firm_value import (
    create_firm_value_manifest,
    firm_value_manifest_to_json,
    load_firm_value_manifest,
    run_firm_value_self_test,
    wheel_distribution_sha256,
)


class FirmValueContractTests(unittest.TestCase):
    def test_offline_self_test_covers_value_and_refusal_gates(self) -> None:
        record = run_firm_value_self_test()

        self.assertTrue(record.passed)
        self.assertEqual(record.provider_calls, 0)
        self.assertFalse(record.quota_consumed)
        self.assertEqual(len(record.report.pairs), 3)
        self.assertEqual(
            record.report.overall_classification,
            "VALUE_SIGNAL_WITH_HIGHER_COST",
        )
        self.assertEqual(
            {check.name for check in record.checks},
            {
                "exact_three_by_two_record_set_aggregates",
                "solo_edit_keeps_one_employee",
                "parallel_case_exposes_quality_cost_tradeoff",
                "tampered_and_missing_records_are_refused",
                "mixed_model_and_duplicate_run_are_refused",
                "unconfirmed_quota_record_is_refused",
                "directional_gate_does_not_call_high_cost_signal_an_absolute_win",
                "self_test_uses_no_provider_network_or_quota",
            },
        )

    def test_manifest_hash_fixture_revision_and_freshness_fail_closed(self) -> None:
        now = datetime(2026, 7, 15, tzinfo=timezone.utc)
        manifest = create_firm_value_manifest(
            distribution_sha256="a" * 64,
            source_revision="fixture-source-revision",
            model_id="fixture-model",
            now=now,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(firm_value_manifest_to_json(manifest), encoding="utf-8")
            loaded = load_firm_value_manifest(path, now=now + timedelta(hours=1))
            tampered = json.loads(path.read_text(encoding="utf-8"))
            tampered["model_id"] = "different-model"
            tampered_path = Path(directory) / "tampered.json"
            tampered_path.write_text(json.dumps(tampered), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "content hash"):
                load_firm_value_manifest(tampered_path, now=now + timedelta(hours=1))
            with self.assertRaisesRegex(ValueError, "expired"):
                load_firm_value_manifest(path, now=now + timedelta(days=8))

        self.assertEqual(loaded.benchmark_id, manifest.benchmark_id)
        self.assertEqual(len(loaded.fixtures), 3)
        self.assertEqual(len(loaded.expected_runs), 6)

    def test_wheel_and_cli_manifest_creation_are_provider_free(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wheel = root / f"noruct-{__version__}-py3-none-any.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr(
                    f"noruct-{__version__}.dist-info/METADATA",
                    "Metadata-Version: 2.4\n"
                    "Name: noruct\n"
                    f"Version: {__version__}\n",
                )
            output_path = root / "manifest.json"
            output = io.StringIO()
            error = io.StringIO()
            exit_code = main(
                [
                    "eval",
                    "firm-value",
                    "--create-manifest",
                    str(output_path),
                    "--wheel",
                    str(wheel),
                    "--source-revision",
                    "fixture-source-revision",
                    "--model",
                    "fixture-model",
                    "--json",
                ],
                stdout=output,
                stderr=error,
            )
            persisted = json.loads(output_path.read_text(encoding="utf-8"))
            expected_sha = wheel_distribution_sha256(wheel)
            mode = output_path.stat().st_mode & 0o777

        self.assertEqual(exit_code, EXIT_OK, error.getvalue())
        self.assertEqual(json.loads(output.getvalue()), persisted)
        self.assertEqual(persisted["schema_version"], "noruct.firm-value-benchmark.v1")
        self.assertEqual(persisted["distribution_sha256"], expected_sha)
        self.assertEqual(mode, 0o600)

    def test_wheel_allows_only_the_exact_audited_optional_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            wheel = Path(directory) / f"noruct-{__version__}-py3-none-any.whl"
            metadata = (
                "Metadata-Version: 2.4\n"
                "Name: noruct\n"
                f"Version: {__version__}\n"
                "Requires-Dist: PyYAML==6.0.3\n"
                "Provides-Extra: modern-tui\n"
                'Requires-Dist: textual==8.2.8; extra == "modern-tui"\n'
                'Requires-Dist: markdown-it-py==4.2.0; extra == "modern-tui"\n'
                'Requires-Dist: mdit-py-plugins==0.6.1; extra == "modern-tui"\n'
                'Requires-Dist: mdurl==0.1.2; extra == "modern-tui"\n'
                'Requires-Dist: platformdirs==4.10.1; extra == "modern-tui"\n'
                'Requires-Dist: pygments==2.20.0; extra == "modern-tui"\n'
                'Requires-Dist: rich==15.0.0; extra == "modern-tui"\n'
                'Requires-Dist: typing-extensions==4.16.0; extra == "modern-tui"\n'
                'Requires-Dist: linkify-it-py==2.1.0; extra == "modern-tui"\n'
                'Requires-Dist: uc-micro-py==2.0.0; extra == "modern-tui"\n'
            )
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr(f"noruct-{__version__}.dist-info/METADATA", metadata)
            self.assertEqual(len(wheel_distribution_sha256(wheel)), 64)
            unexpected = Path(directory) / f"unexpected-noruct-{__version__}-py3-none-any.whl"
            with zipfile.ZipFile(unexpected, "w") as archive:
                archive.writestr(
                    f"noruct-{__version__}.dist-info/METADATA",
                    metadata + 'Requires-Dist: surprise==1.0\n',
                )
            with self.assertRaisesRegex(ValueError, "exact audited base/optional"):
                wheel_distribution_sha256(unexpected)

    def test_cli_default_is_offline_self_test(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        exit_code = main(
            ["eval", "firm-value", "--json"],
            stdout=output,
            stderr=error,
        )
        payload = json.loads(output.getvalue())

        self.assertEqual(exit_code, EXIT_OK, error.getvalue())
        self.assertEqual(payload["schema_version"], "noruct.firm-value-self-test.v1")
        self.assertEqual(payload["provider_calls"], 0)
        self.assertFalse(payload["quota_consumed"])


class FirmValueLiveRecordIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_closed_loop_record_contains_manifest_and_active_job_facts(self) -> None:
        record = await run_closed_loop_evaluation("parallel-evidence", "dynamic")

        self.assertTrue(record.fixture_revision.startswith("fixture-"))
        self.assertEqual(record.permission_mode, "shadow-workspace-approved")
        self.assertEqual(record.approval_mode, "allow-once")
        self.assertEqual(record.active_job_audit_status, "TERMINAL")
        self.assertEqual(len(record.task_attempts), 3)
        self.assertEqual(record.task_mutations, ())


if __name__ == "__main__":
    unittest.main()
