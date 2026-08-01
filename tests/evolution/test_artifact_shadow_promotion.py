from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from dynamic_firm.company.models import canonical_json, content_digest
from dynamic_firm.evolution import EvolutionNetworkService, EvolutionStore
from dynamic_firm.evolution.store import EVOLUTION_STORE_SCHEMA_VERSION


def _artifact(
    artifact_id: str,
    version: str,
    *,
    required_capabilities: tuple[str, ...] = ("workspace_read",),
) -> dict[str, object]:
    return {
        "schema": "noruct.evolution-artifact.v1",
        "artifact_id": artifact_id,
        "version": version,
        "kind": "SKILL_PACKAGE",
        "release_channel": "STABLE",
        "compatibility": {
            "runtime_contract": "noruct_v1",
            "required_capabilities": list(required_capabilities),
        },
        "content": {
            "skill_key": artifact_id,
            "applies_to": ["repository_analysis"],
            "steps": [f"Use {artifact_id} version {version}"],
            "required_capabilities": [],
        },
        "passport": {
            "schema": "noruct.workforce-passport.v1",
            "benchmark": {
                "suite_id": "repository_suite",
                "version": "1.0.0",
                "digest": "b" * 64,
            },
            "metrics": {
                "quality_score": 0.8,
                "safety_score": 1.0,
                "cost_bucket": "LOW",
                "latency_bucket": "LOW",
            },
            "limitations": [],
        },
    }


class ArtifactShadowPromotionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "evolution.db"
        self.store = EvolutionStore(self.path)
        self.service = EvolutionNetworkService(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def _setup_pair(
        self,
        *,
        artifact_id: str = "shadow_skill",
        scope_key: str = "employee_researcher",
        candidate_capabilities: tuple[str, ...] = ("workspace_read",),
    ) -> None:
        base = _artifact(artifact_id, "1.0.0")
        self.service.register_artifact_manifest(base)
        self.service.stage_artifact(artifact_id, "1.0.0")
        self.service.install_artifact(artifact_id, "1.0.0")
        self.service.activate_artifact(
            scope_key=scope_key,
            artifact_id=artifact_id,
            version="1.0.0",
            allowed_capabilities=("workspace_read",),
        )
        candidate = _artifact(
            artifact_id,
            "1.1.0",
            required_capabilities=candidate_capabilities,
        )
        self.service.register_local_derived_artifact_manifest(
            candidate,
            base_artifact_id=artifact_id,
            base_version="1.0.0",
            producer="shadow_evaluator",
            evidence_digest=content_digest(
                {"artifact_id": artifact_id, "candidate": "1.1.0"}
            ),
        )
        self.service.set_artifact_update_subscription(
            scope_key=scope_key,
            kind="SKILL_PACKAGE",
            artifact_id=artifact_id,
            mode="TRACK_STABLE",
        )

    def _record(
        self,
        *,
        artifact_id: str = "shadow_skill",
        scope_key: str = "employee_researcher",
        candidate_version: str = "1.1.0",
        **overrides: object,
    ) -> dict[str, object]:
        values: dict[str, object] = {
            "fixture_kind": "SYNTHETIC",
            "fixture_id": "same_contract_fixture",
            "fixture_version": "1.0.0",
            "fixture_digest": content_digest(
                {"fixture": "same_contract_fixture", "version": "1.0.0"}
            ),
            "baseline_quality": 0.8,
            "candidate_quality": 0.85,
            "baseline_safety": 1.0,
            "candidate_safety": 1.0,
            "baseline_cost": 1.0,
            "candidate_cost": 1.0,
            "cost_ceiling": 1.1,
            "terminal_state": "COMPLETE",
            "complete": True,
            "attempt_count": 1,
            "failure_count": 0,
            "failure_history_digest": content_digest([]),
        }
        values.update(overrides)
        return dict(
            self.service.record_artifact_shadow_evaluation(
                scope_key=scope_key,
                artifact_id=artifact_id,
                candidate_version=candidate_version,
                **values,
            )
        )

    def _apply(
        self,
        *,
        artifact_id: str = "shadow_skill",
        scope_key: str = "employee_researcher",
        allowed_capabilities: tuple[str, ...] = ("workspace_read",),
    ) -> dict[str, object]:
        del artifact_id
        return dict(
            self.service.apply_artifact_update_subscriptions(
                scope_key=scope_key,
                allowed_capabilities=allowed_capabilities,
            )[0]
        )

    def test_missing_receipt_stages_candidate_without_activation(self) -> None:
        self._setup_pair()

        outcome = self._apply()

        self.assertEqual(outcome["decision"], "STAGED_PENDING_SHADOW_EVALUATION")
        self.assertEqual(outcome["shadow_state"], "MISSING")
        self.assertEqual(
            self.service.list_active_artifacts("employee_researcher")[0]["version"],
            "1.0.0",
        )
        self.assertEqual(self.store.list_artifact_installations()[-1]["status"], "STAGED")

    def test_exact_pass_activates_next_job_and_preserves_pin_and_rollback(self) -> None:
        self._setup_pair()
        base_digest = self.store.get_artifact_version("shadow_skill", "1.0.0")[
            "manifest_digest"
        ]
        candidate_digest = self.store.get_artifact_version("shadow_skill", "1.1.0")[
            "manifest_digest"
        ]
        pinned = self.service.pin_active_artifacts_for_job(
            job_id="job_before_shadow", scope_key="employee_researcher"
        )
        receipt = self._record(
            attempt_count=3,
            failure_count=2,
            failure_history_digest=content_digest(
                ["attempt_1_failed", "attempt_2_failed"]
            ),
        )

        outcome = self._apply()

        self.assertEqual(receipt["result"], "PASS")
        self.assertEqual(outcome["decision"], "ACTIVATED_NEXT_JOB")
        self.assertEqual(outcome["shadow_receipt_id"], receipt["receipt_id"])
        self.assertEqual(pinned[0]["version"], "1.0.0")
        self.assertEqual(
            self.store.list_job_artifact_pins("job_before_shadow")[0]["version"],
            "1.0.0",
        )
        self.assertEqual(
            self.service.list_active_artifacts("employee_researcher")[0]["version"],
            "1.1.0",
        )
        self.assertEqual(
            self.store.get_artifact_version("shadow_skill", "1.0.0")[
                "manifest_digest"
            ],
            base_digest,
        )
        self.assertEqual(
            self.store.get_artifact_version("shadow_skill", "1.1.0")[
                "manifest_digest"
            ],
            candidate_digest,
        )
        restored = self.service.rollback_artifact(
            scope_key="employee_researcher", artifact_id="shadow_skill"
        )
        self.assertEqual(restored["version"], "1.0.0")

    def test_latest_failed_receipt_preserves_attempt_history_and_blocks_prior_pass(self) -> None:
        self._setup_pair()
        passed = self._record()
        failed = self._record(
            terminal_state="FAILED",
            complete=False,
            attempt_count=2,
            failure_count=2,
            failure_history_digest=content_digest(["failed_1", "failed_2"]),
        )

        outcome = self._apply()
        projection = self.service.artifact_shadow_evaluation_projection(
            scope_key="employee_researcher", artifact_id="shadow_skill"
        )

        self.assertEqual(passed["result"], "PASS")
        self.assertEqual(failed["result"], "FAILED")
        self.assertEqual(outcome["decision"], "STAGED_PENDING_SHADOW_EVALUATION")
        self.assertEqual(outcome["shadow_state"], "FAILED")
        self.assertEqual(len(projection["receipts"]), 2)
        self.assertFalse(projection["receipts"][0]["latest_for_slot"])
        self.assertTrue(projection["receipts"][1]["latest_for_slot"])
        self.assertEqual(projection["receipts"][1]["failure_count"], 2)
        self.assertFalse(projection["network_request_performed"])
        self.assertNotIn("manifest", projection["receipts"][1])

    def test_regression_incomplete_and_cost_ceiling_each_fail_closed(self) -> None:
        cases = (
            ("regression_skill", {"candidate_quality": 0.7}, "REGRESSION"),
            (
                "incomplete_skill",
                {"terminal_state": "INCOMPLETE", "complete": False},
                "INCOMPLETE",
            ),
            (
                "cost_skill",
                {"candidate_cost": 1.2, "cost_ceiling": 1.1},
                "COST_CEILING_EXCEEDED",
            ),
        )
        for artifact_id, overrides, expected in cases:
            with self.subTest(expected=expected):
                scope = artifact_id.removesuffix("_skill") + "_scope"
                self._setup_pair(artifact_id=artifact_id, scope_key=scope)
                receipt = self._record(
                    artifact_id=artifact_id, scope_key=scope, **overrides
                )
                outcome = self._apply(artifact_id=artifact_id, scope_key=scope)
                self.assertEqual(receipt["result"], expected)
                self.assertEqual(
                    outcome["decision"], "STAGED_PENDING_SHADOW_EVALUATION"
                )
                self.assertEqual(outcome["shadow_state"], expected)

    def test_permission_expansion_cannot_pass_even_when_local_authority_has_it(self) -> None:
        self._setup_pair(
            artifact_id="expanded_skill",
            scope_key="expanded_scope",
            candidate_capabilities=("workspace_read", "workspace_write"),
        )

        outcome = self._apply(
            artifact_id="expanded_skill",
            scope_key="expanded_scope",
            allowed_capabilities=("workspace_read", "workspace_write"),
        )

        self.assertEqual(outcome["decision"], "STAGED_PENDING_SHADOW_EVALUATION")
        self.assertEqual(outcome["shadow_state"], "PERMISSION_EXPANSION")

    def test_receipt_for_a_different_active_base_is_stale(self) -> None:
        self._setup_pair()
        self.service.stage_artifact("shadow_skill", "1.1.0")
        self.service.install_artifact("shadow_skill", "1.1.0")
        self.service.activate_artifact(
            scope_key="employee_researcher",
            artifact_id="shadow_skill",
            version="1.1.0",
            allowed_capabilities=("workspace_read",),
        )
        self.service.register_local_derived_artifact_manifest(
            _artifact("shadow_skill", "1.2.0"),
            base_artifact_id="shadow_skill",
            base_version="1.1.0",
            producer="shadow_evaluator",
            evidence_digest=content_digest({"candidate": "1.2.0"}),
        )
        self._record(candidate_version="1.2.0")
        self.service.rollback_artifact(
            scope_key="employee_researcher", artifact_id="shadow_skill"
        )

        outcome = self._apply()

        self.assertEqual(outcome["decision"], "STAGED_PENDING_SHADOW_EVALUATION")
        self.assertEqual(outcome["shadow_state"], "STALE")
        self.assertEqual(
            self.service.list_active_artifacts("employee_researcher")[0]["version"],
            "1.0.0",
        )

    def test_receipt_table_is_append_only_and_digest_tamper_fails_closed(self) -> None:
        self._setup_pair()
        receipt = self._record()
        with self.assertRaisesRegex(sqlite3.DatabaseError, "immutable"):
            with self.store._transaction() as connection:  # noqa: SLF001
                connection.execute(
                    "UPDATE evolution_artifact_shadow_receipts "
                    "SET candidate_quality = '0.1' WHERE receipt_id = ?",
                    (receipt["receipt_id"],),
                )
        with self.store._transaction() as connection:  # noqa: SLF001 - corruption fixture
            connection.execute("DROP TRIGGER immutable_artifact_shadow_receipt_update")
            connection.execute(
                "UPDATE evolution_artifact_shadow_receipts "
                "SET candidate_quality = '0.1' WHERE receipt_id = ?",
                (receipt["receipt_id"],),
            )

        outcome = self._apply()
        projection = self.service.artifact_shadow_evaluation_projection(
            scope_key="employee_researcher", artifact_id="shadow_skill"
        )

        self.assertEqual(outcome["decision"], "STAGED_PENDING_SHADOW_EVALUATION")
        self.assertEqual(outcome["shadow_state"], "TAMPERED")
        self.assertEqual(projection["integrity_state"], "TAMPERED")
        self.assertEqual(projection["next_action"], "EXPLICIT_REVIEW")

    def test_candidate_manifest_digest_drift_blocks_an_exact_pass(self) -> None:
        self._setup_pair()
        self._record()
        candidate = self.store.get_artifact_version("shadow_skill", "1.1.0")
        changed = dict(candidate["manifest"])
        changed_content = dict(changed["content"])
        changed_content["steps"] = ("Tampered after evaluation",)
        changed["content"] = changed_content
        with self.store._transaction() as connection:  # noqa: SLF001 - corruption fixture
            connection.execute(
                "UPDATE evolution_artifact_versions SET manifest_json = ? "
                "WHERE artifact_id = 'shadow_skill' AND version = '1.1.0'",
                (canonical_json(changed),),
            )

        outcome = self._apply()
        projection = self.service.artifact_shadow_evaluation_projection(
            scope_key="employee_researcher", artifact_id="shadow_skill"
        )

        self.assertEqual(outcome["decision"], "STAGED_PENDING_SHADOW_EVALUATION")
        self.assertEqual(outcome["shadow_state"], "TAMPERED")
        self.assertEqual(projection["receipts"][0]["evidence_state"], "TAMPERED")

    def test_candidate_registration_is_additive_and_never_overwrites_base(self) -> None:
        self._setup_pair()
        base_before = self.store.get_artifact_version("shadow_skill", "1.0.0")
        changed = _artifact("shadow_skill", "1.1.0")
        changed["content"]["steps"] = ["Changed candidate bytes"]

        with self.assertRaisesRegex(ValueError, "immutable"):
            self.service.register_local_derived_artifact_manifest(
                changed,
                base_artifact_id="shadow_skill",
                base_version="1.0.0",
                producer="shadow_evaluator",
                evidence_digest=content_digest({"candidate": "changed"}),
            )

        base_after = self.store.get_artifact_version("shadow_skill", "1.0.0")
        self.assertEqual(base_after["manifest_digest"], base_before["manifest_digest"])
        self.assertEqual(
            tuple(item["version"] for item in self.service.list_artifacts()),
            ("1.0.0", "1.1.0"),
        )

    def test_regression_signal_proposes_exact_rollback_without_deactivating(self) -> None:
        self._setup_pair()
        self._record()
        self.assertEqual(self._apply()["decision"], "ACTIVATED_NEXT_JOB")
        current = self.service.list_active_artifacts("employee_researcher")[0]
        signal = self.service.report_artifact_regression(
            scope_key="employee_researcher",
            artifact_id="shadow_skill",
            signal_kind="SAFETY_REGRESSION",
            evidence_digest=content_digest({"observation": "safe-content-free"}),
        )
        self.assertEqual(signal["activation_id"], current["activation_id"])
        proposal = self.service.artifact_regression_projection(
            scope_key="employee_researcher", artifact_id="shadow_skill"
        )
        self.assertEqual(proposal["integrity_state"], "VERIFIED")
        self.assertEqual(proposal["next_action"], "CONFIRM_EXACT_ROLLBACK")
        self.assertEqual(proposal["signals"][0]["proposal_state"], "ROLLBACK_PROPOSED")
        self.assertEqual(proposal["signals"][0]["rollback_target"]["version"], "1.0.0")
        self.assertEqual(
            proposal["signals"][0]["rollback_command"],
            "noruct evolution artifact rollback employee_researcher --artifact-id shadow_skill --confirm",
        )
        self.assertEqual(
            self.service.list_active_artifacts("employee_researcher")[0]["version"],
            "1.1.0",
        )
        self.service.rollback_artifact(
            scope_key="employee_researcher", artifact_id="shadow_skill"
        )
        historical = self.service.artifact_regression_projection(
            scope_key="employee_researcher", artifact_id="shadow_skill"
        )
        self.assertEqual(historical["next_action"], "NONE")
        self.assertEqual(historical["signals"][0]["proposal_state"], "HISTORICAL_SIGNAL")

    def test_regression_signal_receipt_tampering_fails_closed(self) -> None:
        self._setup_pair()
        self._record()
        self._apply()
        self.service.report_artifact_regression(
            scope_key="employee_researcher",
            artifact_id="shadow_skill",
            signal_kind="QUALITY_REGRESSION",
            evidence_digest=content_digest({"observation": "bounded"}),
        )
        with self.store._transaction() as connection:
            connection.execute("DROP TRIGGER immutable_artifact_regression_signal_update")
            connection.execute(
                "UPDATE evolution_artifact_regression_signals SET signal_kind = 'EFFECT_FAILURE'"
            )
        projection = self.service.artifact_regression_projection(
            scope_key="employee_researcher", artifact_id="shadow_skill"
        )
        self.assertEqual(projection["integrity_state"], "TAMPERED")
        self.assertEqual(projection["next_action"], "EXPLICIT_REVIEW")

    def test_recursive_improvement_policy_bounds_attempts_candidates_and_frequency(self) -> None:
        self._setup_pair()
        with self.assertRaisesRegex(ValueError, "bounded recursive-improvement"):
            self._record(attempt_count=9)
        for version in ("1.2.0", "1.3.0", "1.4.0"):
            self.service.register_local_derived_artifact_manifest(
                _artifact("shadow_skill", version),
                base_artifact_id="shadow_skill",
                base_version="1.0.0",
                producer="shadow_evaluator",
                evidence_digest=content_digest({"candidate": version}),
            )
        bounded = self._apply()
        self.assertEqual(bounded["decision"], "CANDIDATE_REVIEW_BOUND_REACHED")
        self.assertEqual(bounded["candidate_limit"], 3)

        self._setup_pair(artifact_id="cooldown_skill", scope_key="cooldown_scope")
        self._record(artifact_id="cooldown_skill", scope_key="cooldown_scope")
        self.assertEqual(
            self._apply(artifact_id="cooldown_skill", scope_key="cooldown_scope")["decision"],
            "ACTIVATED_NEXT_JOB",
        )
        self.service.register_local_derived_artifact_manifest(
            _artifact("cooldown_skill", "1.2.0"),
            base_artifact_id="cooldown_skill",
            base_version="1.1.0",
            producer="shadow_evaluator",
            evidence_digest=content_digest({"candidate": "1.2.0"}),
        )
        cooldown = dict(
            self.service.apply_artifact_update_subscriptions(
                scope_key="cooldown_scope", allowed_capabilities=("workspace_read",)
            )[-1]
        )
        self.assertEqual(cooldown["decision"], "PROMOTION_COOLDOWN_ACTIVE")


class EvolutionStoreV18ShadowMigrationTests(unittest.TestCase):
    def test_v17_catalog_migrates_and_receipt_replays_after_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "evolution-v17.db"
            with EvolutionStore(path) as store:
                service = EvolutionNetworkService(store)
                service.register_artifact_manifest(_artifact("migrated_skill", "1.0.0"))
                service.stage_artifact("migrated_skill", "1.0.0")
                service.install_artifact("migrated_skill", "1.0.0")
                service.activate_artifact(
                    scope_key="migrated_scope",
                    artifact_id="migrated_skill",
                    version="1.0.0",
                    allowed_capabilities=("workspace_read",),
                )
                service.register_local_derived_artifact_manifest(
                    _artifact("migrated_skill", "1.1.0"),
                    base_artifact_id="migrated_skill",
                    base_version="1.0.0",
                    producer="shadow_evaluator",
                    evidence_digest=content_digest({"candidate": "1.1.0"}),
                )
            with sqlite3.connect(path) as connection:
                connection.execute("DROP TRIGGER immutable_artifact_shadow_receipt_update")
                connection.execute("DROP TRIGGER immutable_artifact_shadow_receipt_delete")
                connection.execute("DROP TABLE evolution_artifact_shadow_receipts")
                connection.execute(
                    "UPDATE evolution_meta SET value = '17' WHERE key = 'schema_version'"
                )

            with EvolutionStore(path) as migrated:
                service = EvolutionNetworkService(migrated)
                self.assertEqual(
                    migrated.status()["schema_version"], EVOLUTION_STORE_SCHEMA_VERSION
                )
                self.assertEqual(migrated.status()["artifact_shadow_receipts"], 0)
                receipt = service.record_artifact_shadow_evaluation(
                    scope_key="migrated_scope",
                    artifact_id="migrated_skill",
                    candidate_version="1.1.0",
                    fixture_kind="PUBLIC",
                    fixture_id="migration_fixture",
                    fixture_version="1.0.0",
                    fixture_digest=content_digest({"fixture": "migration_fixture"}),
                    baseline_quality=0.8,
                    candidate_quality=0.8,
                    baseline_safety=1.0,
                    candidate_safety=1.0,
                    baseline_cost=1.0,
                    candidate_cost=1.0,
                    cost_ceiling=1.0,
                    terminal_state="COMPLETE",
                    complete=True,
                    attempt_count=1,
                    failure_count=0,
                    failure_history_digest=content_digest([]),
                )
                receipt_id = str(receipt["receipt_id"])
                receipt_digest = str(receipt["receipt_digest"])

            with EvolutionStore(path) as replayed:
                loaded = replayed.get_artifact_shadow_receipt(receipt_id)
                self.assertEqual(loaded["receipt_digest"], receipt_digest)
                self.assertEqual(loaded["result"], "PASS")
                self.assertEqual(
                    replayed.get_artifact_version("migrated_skill", "1.0.0")[
                        "origin_kind"
                    ],
                    "USER_IMPORTED",
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
