from __future__ import annotations

import json
import io
import tempfile
import unittest
from pathlib import Path

from dynamic_firm import __version__
from dynamic_firm.cli import EXIT_INPUT, main
from dynamic_firm.company.models import content_digest
from dynamic_firm.foundation.provider_evidence import SCHEMA, ProviderSlotEvidenceError, _publish_validated_capture, validate_provider_evidence_matrix, validate_provider_evidence_matrix_records, validate_provider_slot_evidence
from dynamic_firm.foundation.source import EMPLOYEE_FOUNDATION_COMMIT, verify_employee_runtime_capsule


def _record(**overrides):
    capsule = verify_employee_runtime_capsule()
    value = {
        "schema_version": SCHEMA,
        "recorded_at": "2026-07-18T00:00:00+00:00",
        "operator_authorized_at": "2026-07-17T23:59:59+00:00",
        "noruct_version": __version__, "source_commit": EMPLOYEE_FOUNDATION_COMMIT,
        "capsule_tree_sha256": capsule["tree_sha256"], "wheel_sha256": "a" * 64,
        "worker_python_sha256": "b" * 64,
        "adapter_revision": "noruct-codex-parent-tool-cancel-v1",
        "action_policy_sha256": "c" * 64,
        "fixture_sha256": "d" * 64,
        "event_sequence_sha256": "e" * 64,
        "usage_accounting": "subscription_quota_usd_unavailable",
        "provider_id": "openai-codex-cli", "model_id": "selected-model", "slot": "direct",
        "operator_slot_authorized": True, "quota_confirmed": True,
        "activation": "explicit_preview_only", "commercial_default_eligible": False,
        "shared_network_release_authorized": False,
        "limits": {"max_model_calls": 1, "max_tool_calls": 0, "max_wall_time_ms": 1000},
        "observed": {"external_model_calls": 1, "tool_intents": 0, "approval_events": 0, "terminal_status": "SUCCEEDED", "provider_request_id_present": True, "parent_owned_tool": False, "side_effect_committed": False, "cancellation_event_present": False},
    }
    value.update(overrides)
    digest = content_digest(value)
    return {**value, "content_hash": digest, "evidence_id": f"employee-provider-slot-{digest[:24]}"}


class ProviderSlotEvidenceTests(unittest.TestCase):
    def _write(self, value):
        directory = tempfile.TemporaryDirectory()
        path = Path(directory.name) / "record.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        self.addCleanup(directory.cleanup)
        return path

    def test_accepts_hash_bound_explicit_preview_direct_slot(self):
        record = validate_provider_slot_evidence(self._write(_record()))
        self.assertEqual(record["slot"], "direct")

    def test_historical_captured_provider_slots_are_rejected_after_capsule_identity_changes(self):
        root = Path(__file__).parents[2] / "docs" / "50-mvp" / "evaluations"
        for filename, expected_slot in (
            ("h2-26-provider-direct-slot.json", "direct"),
            ("h2-26-provider-read-tool-slot.json", "read_tool"),
            ("h2-26-provider-cancel-recovery-slot.json", "cancel_recovery"),
            ("h2-26-provider-approval-slot.json", "approval"),
        ):
            with self.assertRaisesRegex(ProviderSlotEvidenceError, "version is not accepted"):
                validate_provider_slot_evidence(root / filename)

    def test_rejects_missing_quota_secret_field_and_release_claim(self):
        for record in (
            _record(quota_confirmed=False),
            _record(api_key="not-allowed"),
            _record(commercial_default_eligible=True),
        ):
            with self.assertRaises(ProviderSlotEvidenceError):
                validate_provider_slot_evidence(self._write(record))

    def test_rejects_missing_reproducibility_identity_or_wrong_side_effect_contract(self):
        for record in (
            _record(fixture_sha256="not-a-hash"),
            _record(operator_authorized_at="2026-07-19T00:00:00+00:00"),
            _record(observed={"external_model_calls": 1, "tool_intents": 0, "approval_events": 0, "terminal_status": "SUCCEEDED", "provider_request_id_present": True, "parent_owned_tool": True, "side_effect_committed": False, "cancellation_event_present": False}),
        ):
            with self.assertRaises(ProviderSlotEvidenceError):
                validate_provider_slot_evidence(self._write(record))

    def test_cli_accepts_only_the_same_fail_closed_evidence_contract(self):
        accepted = self._write(_record())
        output = io.StringIO()
        self.assertEqual(
            main(["foundation", "validate-provider-evidence", str(accepted), "--json"], stdout=output),
            0,
        )
        self.assertIn('"commercial_default_eligible": false', output.getvalue())

        rejected = self._write(_record(shared_network_release_authorized=True))
        self.assertEqual(
            main(["foundation", "validate-provider-evidence", str(rejected)], stdout=io.StringIO()),
            EXIT_INPUT,
        )

    def test_capture_cli_requires_explicit_completed_ledger_confirmation(self):
        self.assertEqual(
            main(
                [
                    "foundation", "capture-provider-evidence",
                    "--ledger", "missing.db", "--run-id", "run", "--slot", "direct",
                    "--wheel", "missing.whl", "--runtime-python", "missing-python",
                    "--fixture-root", "missing-fixture", "--provider-id", "openai-codex-cli",
                    "--model-id", "selected-model", "--operator-authorized-at", "2026-07-18T00:00:00+00:00",
                    "--max-wall-time-ms", "1000", "--output", "record.json",
                ],
                stdout=io.StringIO(),
            ),
            EXIT_INPUT,
        )

    def test_invalid_capture_is_never_published_to_its_canonical_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "record.json"
            invalid = _record(operator_authorized_at="2026-07-19T00:00:00+00:00")
            with self.assertRaises(ProviderSlotEvidenceError):
                _publish_validated_capture(path, invalid)
            self.assertFalse(path.exists())

            _publish_validated_capture(path, _record())
            self.assertTrue(path.is_file())
            with self.assertRaises(ProviderSlotEvidenceError):
                _publish_validated_capture(path, _record())

    def test_matrix_requires_all_slots_from_one_provider_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for slot, limits, observed in (
                ("direct", 1, (0, 0, "SUCCEEDED")),
                ("read_tool", 2, (1, 0, "SUCCEEDED")),
                ("approval", 2, (1, 1, "SUCCEEDED")),
                ("cancel_recovery", 1, (0, 0, "CANCELLED")),
            ):
                value = _record(slot=slot, limits={"max_model_calls": limits, "max_tool_calls": 1, "max_wall_time_ms": 1000}, observed={"external_model_calls": 1, "tool_intents": observed[0], "approval_events": observed[1], "terminal_status": observed[2], "provider_request_id_present": True, "parent_owned_tool": slot in {"read_tool", "approval"}, "side_effect_committed": slot == "approval", "cancellation_event_present": slot == "cancel_recovery"})
                digest = content_digest({key: value[key] for key in value if key not in {"content_hash", "evidence_id"}})
                value.update(content_hash=digest, evidence_id=f"employee-provider-slot-{digest[:24]}")
                (root / f"{slot}.json").write_text(json.dumps(value), encoding="utf-8")
            matrix = validate_provider_evidence_matrix(root)
            self.assertTrue(matrix["complete"])
            self.assertFalse(matrix["commercial_default_eligible"])
            direct_paths = {slot: root / f"{slot}.json" for slot in ("direct", "read_tool", "approval", "cancel_recovery")}
            self.assertTrue(validate_provider_evidence_matrix_records(direct_paths)["complete"])
            self.assertEqual(main(["foundation", "provider-evidence-records-status", "--direct", str(direct_paths["direct"]), "--read-tool", str(direct_paths["read_tool"]), "--approval", str(direct_paths["approval"]), "--cancel-recovery", str(direct_paths["cancel_recovery"]), "--json"], stdout=io.StringIO()), 0)
            admission = io.StringIO()
            self.assertEqual(main(["foundation", "release-admission-status", "--direct", str(direct_paths["direct"]), "--read-tool", str(direct_paths["read_tool"]), "--approval", str(direct_paths["approval"]), "--cancel-recovery", str(direct_paths["cancel_recovery"]), "--state", str(root / "absent-runtime.db"), "--json"], stdout=admission), 0)
            self.assertIn('"release_authorized": false', admission.getvalue())
            self.assertIn('"migration_preview"', admission.getvalue())
            self.assertFalse((root / "absent-runtime.db").exists())
            (root / "approval.json").unlink()
            with self.assertRaises(ProviderSlotEvidenceError):
                validate_provider_evidence_matrix(root)
