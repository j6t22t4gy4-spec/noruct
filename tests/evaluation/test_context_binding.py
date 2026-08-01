from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from dynamic_firm.company.models import content_digest
from dynamic_firm.evaluation.context_binding import (
    EXACT_CONTEXT_BINDING_SCHEMA,
    ExactContextBindingError,
    ExactContextBindingFailureCode,
    create_exact_context_bound_preparation,
    create_exact_context_evidence_binding,
    exact_context_binding_to_json,
    load_exact_context_bound_preparation,
    load_exact_context_evidence_binding,
)
from dynamic_firm.evaluation.workflow_patch_live import (
    WORKFLOW_PATCH_CONTEXT,
    workflow_patch_pattern_id,
)
from dynamic_firm.runtime.models import to_primitive


def _reseal_preflight(value: dict[str, object]) -> dict[str, object]:
    payload = dict(value)
    payload.pop("preflight_id", None)
    payload.pop("content_hash", None)
    digest = content_digest(payload)
    value["content_hash"] = digest
    value["preflight_id"] = f"workflow-patch-natural-preflight-{digest[:24]}"
    return value


def _preflight(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": "noruct.workflow-patch-natural-workload-preflight.v2",
        "preflight_id": "pending",
        "content_hash": "pending",
        "recorded_at": "2026-07-17T00:00:00+00:00",
        "noruct_version": "0.0.55",
        "source_revision": "snapshot-sha256:" + "1" * 64,
        "parent_extension_id": "workflow-patch-extension-parent",
        "parent_semantic_anchor": "2" * 64,
        "applied_pattern_id": "workflow-parent-pattern",
        "applied_context_fingerprint": WORKFLOW_PATCH_CONTEXT,
        "goal_digest": "3" * 64,
        "route": "COMPANY_GOAL",
        "workspace_manifest_status": "BLOCKED",
        "workspace_manifest_error": "ToolValidationError: entry limit",
        "workspace_manifest_count": 0,
        "workspace_manifest_limit": 500,
        "workspace_identity_status": "READY",
        "workspace_identity_failure_code": None,
        "workspace_projection_revision": "noruct.workspace-structure.v2",
        "workspace_projection_truncated": False,
        "workspace_context_fingerprint": "wctx2-" + "4" * 24,
        "selected_prior_ids": [],
        "ready_for_live_observation": False,
        "outcome": "NATURAL_WORKLOAD_PREFLIGHT_BLOCKED_BY_PRIOR_CONTEXT",
        "recommended_direction": "collect-production-exact-context-evidence",
        "checks": [],
        "external_model_calls": 0,
        "quota_consumed": False,
    }
    value.update(overrides)
    return _reseal_preflight(value)


class ExactContextBindingTests(unittest.TestCase):
    def _write_preflight(self, root: Path, value: dict[str, object]) -> Path:
        path = root / "natural-preflight.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_binding_is_deterministic_private_and_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value = _preflight(
                workspace_root="/Users/example/private-repository",
                raw_secret="sk-should-never-cross-the-binding-boundary",
            )
            path = self._write_preflight(root, value)
            first = create_exact_context_evidence_binding(
                path,
                execution_profile="READ_ONLY",
            )
            second = create_exact_context_evidence_binding(
                path,
                execution_profile="READ_ONLY",
            )
            binding_path = root / "binding.json"
            binding_path.write_text(
                exact_context_binding_to_json(first),
                encoding="utf-8",
            )
            loaded = load_exact_context_evidence_binding(binding_path)

        self.assertEqual(first, second)
        self.assertEqual(loaded, first)
        self.assertEqual(first.schema_version, EXACT_CONTEXT_BINDING_SCHEMA)
        serialized = exact_context_binding_to_json(first)
        self.assertNotIn("/Users/example", serialized)
        self.assertNotIn("private-repository", serialized)
        self.assertNotIn("sk-should-never", serialized)
        self.assertNotIn("workspace_root", serialized)
        self.assertNotIn("raw_secret", serialized)

    def test_preflight_tamper_and_semantic_failures_have_typed_codes(self) -> None:
        cases = (
            (
                {"schema_version": "noruct.workflow-patch-natural-workload-preflight.v3"},
                ExactContextBindingFailureCode.UNSUPPORTED_SCHEMA,
                True,
            ),
            (
                {"content_hash": "0" * 64},
                ExactContextBindingFailureCode.CONTENT_HASH_MISMATCH,
                False,
            ),
            (
                {"workspace_identity_status": "FAILED"},
                ExactContextBindingFailureCode.PREFLIGHT_IDENTITY_UNAVAILABLE,
                True,
            ),
            (
                {"route": "CONVERSATION"},
                ExactContextBindingFailureCode.PREFLIGHT_ROUTE_MISMATCH,
                True,
            ),
            (
                {"external_model_calls": 1},
                ExactContextBindingFailureCode.PREFLIGHT_PROVIDER_EVIDENCE_INVALID,
                True,
            ),
            (
                {"selected_prior_ids": ["workflow-parent-pattern"]},
                ExactContextBindingFailureCode.PREFLIGHT_LINEAGE_INVALID,
                True,
            ),
        )
        for updates, expected, reseal in cases:
            with self.subTest(expected=expected):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    value = _preflight()
                    value.update(updates)
                    if reseal:
                        _reseal_preflight(value)
                    path = self._write_preflight(root, value)
                    with self.assertRaises(ExactContextBindingError) as raised:
                        create_exact_context_evidence_binding(
                            path,
                            execution_profile="READ_ONLY",
                        )
                self.assertEqual(raised.exception.code, expected)

    def test_binding_tamper_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binding = create_exact_context_evidence_binding(
                self._write_preflight(root, _preflight()),
                execution_profile="READ_ONLY",
            )
            value = to_primitive(binding)
            value["goal_digest"] = "9" * 64
            path = root / "binding.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(ExactContextBindingError) as raised:
                load_exact_context_evidence_binding(path)

        self.assertEqual(
            raised.exception.code,
            ExactContextBindingFailureCode.CONTENT_HASH_MISMATCH,
        )

    def test_artifact_size_and_symlink_boundaries_are_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            oversized = root / "oversized.json"
            oversized.write_text("x" * (256 * 1024 + 1), encoding="utf-8")
            with self.assertRaises(ExactContextBindingError) as size_error:
                create_exact_context_evidence_binding(
                    oversized,
                    execution_profile="READ_ONLY",
                )
            source = self._write_preflight(root, _preflight())
            symlink = root / "preflight-link.json"
            symlink.symlink_to(source)
            with self.assertRaises(ExactContextBindingError) as link_error:
                create_exact_context_evidence_binding(
                    symlink,
                    execution_profile="READ_ONLY",
                )

        self.assertEqual(
            size_error.exception.code,
            ExactContextBindingFailureCode.ARTIFACT_TOO_LARGE,
        )
        self.assertEqual(
            link_error.exception.code,
            ExactContextBindingFailureCode.ARTIFACT_UNAVAILABLE,
        )

    def test_bound_preparation_has_separate_inert_lineage_and_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binding = create_exact_context_evidence_binding(
                self._write_preflight(root, _preflight()),
                execution_profile="READ_ONLY",
            )
            bound_pattern = workflow_patch_pattern_id(
                context_fingerprint=binding.production_context_fingerprint
            )
            preparation = create_exact_context_bound_preparation(
                binding,
                noruct_version="0.0.55",
                source_revision=binding.source_revision,
                goal_digest=binding.goal_digest,
                execution_profile=binding.execution_profile,
                parent_extension_id=binding.parent_extension_id,
                parent_pattern_id=binding.parent_pattern_id,
                parent_semantic_anchor=binding.parent_semantic_anchor,
                bound_pattern_id=bound_pattern,
            )
            path = root / "preparation.json"
            path.write_text(
                exact_context_binding_to_json(preparation),
                encoding="utf-8",
            )
            loaded = load_exact_context_bound_preparation(path)

        self.assertEqual(loaded, preparation)
        self.assertNotEqual(preparation.bound_pattern_id, preparation.parent_pattern_id)
        self.assertEqual(len(preparation.expected_runs), 2)
        self.assertEqual(len({item.run_id for item in preparation.expected_runs}), 2)
        self.assertFalse(preparation.eligible_for_apply)
        self.assertFalse(preparation.automatic_approval)
        self.assertEqual(preparation.external_model_calls, 0)
        self.assertFalse(preparation.quota_consumed)

    def test_prepare_refuses_every_exact_dimension_before_lineage_creation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binding = create_exact_context_evidence_binding(
                self._write_preflight(root, _preflight()),
                execution_profile="READ_ONLY",
            )
        valid = {
            "noruct_version": "0.0.55",
            "source_revision": binding.source_revision,
            "goal_digest": binding.goal_digest,
            "execution_profile": binding.execution_profile,
            "parent_extension_id": binding.parent_extension_id,
            "parent_pattern_id": binding.parent_pattern_id,
            "parent_semantic_anchor": binding.parent_semantic_anchor,
            "bound_pattern_id": workflow_patch_pattern_id(
                context_fingerprint=binding.production_context_fingerprint
            ),
        }
        cases = (
            (
                {"source_revision": "snapshot-sha256:" + "8" * 64},
                ExactContextBindingFailureCode.SOURCE_MISMATCH,
            ),
            (
                {"goal_digest": "8" * 64},
                ExactContextBindingFailureCode.GOAL_MISMATCH,
            ),
            (
                {"execution_profile": "WRITE"},
                ExactContextBindingFailureCode.PROFILE_MISMATCH,
            ),
            (
                {"parent_semantic_anchor": "8" * 64},
                ExactContextBindingFailureCode.PARENT_MISMATCH,
            ),
            (
                {"bound_pattern_id": binding.parent_pattern_id},
                ExactContextBindingFailureCode.BOUND_PATTERN_COLLISION,
            ),
        )
        for updates, expected in cases:
            with self.subTest(expected=expected):
                arguments = {**valid, **updates}
                with self.assertRaises(ExactContextBindingError) as raised:
                    create_exact_context_bound_preparation(binding, **arguments)
                self.assertEqual(raised.exception.code, expected)

    def test_legacy_and_bound_pattern_ids_are_distinct_and_stable(self) -> None:
        legacy = workflow_patch_pattern_id()
        bound = workflow_patch_pattern_id(
            context_fingerprint="wctx2-" + "4" * 24
        )

        self.assertEqual(legacy, workflow_patch_pattern_id())
        self.assertNotEqual(legacy, bound)
        with self.assertRaises(ValueError):
            workflow_patch_pattern_id(context_fingerprint="../../unsafe")


if __name__ == "__main__":
    unittest.main()
