from __future__ import annotations

import json
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dynamic_firm import __version__
from dynamic_firm.company import CompanyLearningService, CompanyStateStore
from dynamic_firm.company.evidence import LIVE_EVIDENCE_SCHEMA, verify_live_evidence_pair
from dynamic_firm.company.models import content_digest
from dynamic_firm.cli import EXIT_INPUT, EXIT_OK, main


def _record(
    *,
    strategy: str,
    revision: str,
    recorded_at: str,
    quality: float,
    model_calls: int,
    run_id: str,
) -> dict:
    dynamic = strategy == "dynamic"
    plan = (
        [
            {
                "task_key": "spec",
                "required_capabilities": ["research"],
                "depends_on": [],
                "final": False,
            },
            {
                "task_key": "tests",
                "required_capabilities": ["testing"],
                "depends_on": [],
                "final": False,
            },
            {
                "task_key": "integrate",
                "required_capabilities": ["implementation"],
                "depends_on": ["spec", "tests"],
                "final": True,
            },
        ]
        if dynamic
        else [
            {
                "task_key": "solo",
                "required_capabilities": ["implementation"],
                "depends_on": [],
                "final": True,
            }
        ]
    )
    value = {
        "schema_version": LIVE_EVIDENCE_SCHEMA,
        "recorded_at": recorded_at,
        "noruct_version": __version__,
        "source_revision": revision,
        "evaluation_run_id": run_id,
        "provider_kind": "openai-codex-user-managed",
        "model_id": "contract-model",
        "planner_source": (
            "live-dynamic-workflow-compiler"
            if dynamic
            else "bounded-counterfactual-plan"
        ),
        "validation_observation_scope": "noruct-post-worker-final-only",
        "subscription_cost_usd": None,
        "quota_confirmed": True,
        "elapsed_ms": 100,
        "external_model_calls": model_calls,
        "result": {
            "fixture": "parallel-evidence",
            "strategy": strategy,
            "status": "SUCCEEDED",
            "planning_mode": "TEAM" if dynamic else "SOLO",
            "ledger_matches_kernel": True,
            "workspace_unchanged_before_approval": True,
            "trajectory": {
                "employee_count": 2 if dynamic else 1,
                "maximum_parallelism": 2 if dynamic else 1,
                "writer_employee_ids": ["employee-writer"],
                "approvals_requested": 1,
                "approvals_granted": 1,
                "preapproval_workspace_mutations": 0,
                "validation_attempts": [True],
            },
            "score": {
                "task_success": True,
                "overall_passed": dynamic,
                "quality_score": quality,
                "validation_passed": True,
                "authority_ok": True,
            },
            "plan_template": plan,
        },
    }
    digest = content_digest(value)
    value["evidence_id"] = f"live-evidence-{digest[:24]}"
    value["content_hash"] = digest
    return value


def _write_pair(root: Path, revision: str, day: int) -> tuple[Path, Path]:
    baseline = _record(
        strategy="solo",
        revision=revision,
        recorded_at=f"2026-07-{day:02d}T00:00:00+00:00",
        quality=0.8,
        model_calls=4,
        run_id=f"live-run-{day:02d}-solo",
    )
    dynamic = _record(
        strategy="dynamic",
        revision=revision,
        recorded_at=f"2026-07-{day:02d}T00:01:00+00:00",
        quality=1.0,
        model_calls=4,
        run_id=f"live-run-{day:02d}-dynamic",
    )
    baseline_path = root / f"{revision}-{day:02d}-solo.json"
    dynamic_path = root / f"{revision}-{day:02d}-dynamic.json"
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
    dynamic_path.write_text(json.dumps(dynamic), encoding="utf-8")
    return baseline_path, dynamic_path


class LiveEvidenceIntakeTests(unittest.TestCase):
    def test_schema_v2_migrates_without_mutating_existing_company_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "company.db"
            with CompanyStateStore(path) as store:
                before = store.summary()
            connection = sqlite3.connect(path)
            connection.execute("DROP TABLE verified_live_evidence_pairs")
            connection.execute(
                "UPDATE company_state_meta SET value = '2' WHERE key = 'schema_version'"
            )
            connection.commit()
            connection.close()

            with CompanyStateStore(path) as migrated:
                self.assertEqual(migrated.schema_version(), 9)
                self.assertEqual(migrated.company().revision, before.company_revision)
                self.assertEqual(migrated.playbook().revision, before.playbook_revision)
                self.assertEqual(migrated.list_live_evidence_pairs(), ())

    def test_cli_preview_is_read_only_and_import_requires_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "company.db"
            baseline, dynamic = _write_pair(root, "revision-cli", 7)
            output = io.StringIO()
            self.assertEqual(
                main(
                    [
                        "company",
                        "evidence-preview",
                        str(baseline),
                        str(dynamic),
                        "--state",
                        str(state),
                        "--json",
                    ],
                    stdout=output,
                    stderr=io.StringIO(),
                ),
                EXIT_OK,
            )
            preview = json.loads(output.getvalue())
            self.assertTrue(preview["importable"])
            with CompanyStateStore(state) as store:
                self.assertEqual(len(store.list_episodes()), 0)

            error = io.StringIO()
            self.assertEqual(
                main(
                    [
                        "company",
                        "evidence-import",
                        str(baseline),
                        str(dynamic),
                        "--state",
                        str(state),
                    ],
                    stdout=io.StringIO(),
                    stderr=error,
                ),
                EXIT_INPUT,
            )
            self.assertIn("requires --confirm", error.getvalue())
            self.assertEqual(
                main(
                    [
                        "company",
                        "evidence-import",
                        str(baseline),
                        str(dynamic),
                        "--state",
                        str(state),
                        "--confirm",
                    ],
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                ),
                EXIT_OK,
            )
            with CompanyStateStore(state) as store:
                self.assertEqual(len(store.list_episodes()), 1)

    def test_two_independent_runs_on_one_revision_create_only_a_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_paths = _write_pair(root, "revision-a1", 1)
            second_paths = _write_pair(root, "revision-a1", 2)
            with CompanyStateStore(root / "company.db") as store:
                first = verify_live_evidence_pair(*first_paths)
                second = verify_live_evidence_pair(*second_paths)
                store.import_live_evidence_pair(first)
                store.import_live_evidence_pair(second)
                result = CompanyLearningService(store).curate()

                self.assertEqual(first.campaign_id, second.campaign_id)
                self.assertNotEqual(first.baseline_run_id, second.baseline_run_id)
                self.assertEqual(store.schema_version(), 9)
                self.assertEqual(store.summary().verified_live_pair_count, 2)
                self.assertEqual(result.decision, "CANDIDATE_AVAILABLE")
                self.assertEqual(len(result.candidates), 1)
                candidate = result.candidates[0]
                self.assertTrue(candidate.eligible_for_apply)
                self.assertEqual(candidate.status.value, "PROPOSED")
                self.assertEqual(store.playbook().revision, 1)

    def test_different_revisions_do_not_merge_into_one_campaign(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with CompanyStateStore(root / "company.db") as store:
                first = verify_live_evidence_pair(*_write_pair(root, "revision-a1", 1))
                second = verify_live_evidence_pair(*_write_pair(root, "revision-b2", 2))
                self.assertNotEqual(first.campaign_id, second.campaign_id)
                store.import_live_evidence_pair(first)
                store.import_live_evidence_pair(second)

                result = CompanyLearningService(store).curate()

                self.assertEqual(result.decision, "NO_PATCH")
                self.assertEqual(result.qualified_episode_count, 2)
                self.assertEqual(len(result.candidates), 0)

    def test_pair_requires_distinct_evaluation_run_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline_path, dynamic_path = _write_pair(root, "revision-runs", 3)
            baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
            dynamic = json.loads(dynamic_path.read_text(encoding="utf-8"))
            dynamic["evaluation_run_id"] = baseline["evaluation_run_id"]
            dynamic.pop("evidence_id")
            dynamic.pop("content_hash")
            digest = content_digest(dynamic)
            dynamic["evidence_id"] = f"live-evidence-{digest[:24]}"
            dynamic["content_hash"] = digest
            dynamic_path.write_text(json.dumps(dynamic), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "distinct evaluation run ids"):
                verify_live_evidence_pair(baseline_path, dynamic_path)

    def test_run_id_cannot_be_reused_across_pair_roles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = verify_live_evidence_pair(*_write_pair(root, "revision-cross", 5))
            second_paths = _write_pair(root, "revision-cross", 6)
            second_baseline = json.loads(second_paths[0].read_text(encoding="utf-8"))
            second_baseline["evaluation_run_id"] = first.dynamic_run_id
            second_baseline.pop("evidence_id")
            second_baseline.pop("content_hash")
            digest = content_digest(second_baseline)
            second_baseline["evidence_id"] = f"live-evidence-{digest[:24]}"
            second_baseline["content_hash"] = digest
            second_paths[0].write_text(json.dumps(second_baseline), encoding="utf-8")
            second = verify_live_evidence_pair(*second_paths)

            with CompanyStateStore(root / "company.db") as store:
                store.import_live_evidence_pair(first)
                with self.assertRaisesRegex(ValueError, "evaluation_run_id"):
                    store.import_live_evidence_pair(second)
                self.assertEqual(len(store.list_episodes()), 1)

    def test_schema_v3_pair_migrates_with_legacy_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "company.db"
            pair = verify_live_evidence_pair(*_write_pair(root, "revision-legacy", 4))
            with CompanyStateStore(state) as store:
                store.import_live_evidence_pair(pair)
                stored = store.list_live_evidence_pairs()[0]

            legacy_fields = (
                "baseline_evidence_id",
                "dynamic_evidence_id",
                "baseline_content_hash",
                "dynamic_content_hash",
                "source_revision",
                "fixture",
                "provider_kind",
                "model_id",
                "baseline_quality_score",
                "dynamic_quality_score",
                "baseline_model_calls",
                "dynamic_model_calls",
            )
            legacy_identity = {key: stored[key] for key in legacy_fields}
            legacy_hash = content_digest(legacy_identity)
            legacy_payload = {
                key: value
                for key, value in stored.items()
                if key not in {"campaign_id", "baseline_run_id", "dynamic_run_id"}
            }
            legacy_payload["content_hash"] = legacy_hash

            with sqlite3.connect(state) as connection:
                connection.executescript(
                    """
                    DROP INDEX verified_live_evidence_campaign_idx;
                    DROP TABLE verified_live_evidence_pairs;
                    CREATE TABLE verified_live_evidence_pairs (
                        pair_id TEXT PRIMARY KEY,
                        baseline_evidence_id TEXT NOT NULL UNIQUE,
                        dynamic_evidence_id TEXT NOT NULL UNIQUE,
                        baseline_content_hash TEXT NOT NULL UNIQUE,
                        dynamic_content_hash TEXT NOT NULL UNIQUE,
                        source_revision TEXT NOT NULL UNIQUE,
                        fixture TEXT NOT NULL,
                        provider_kind TEXT NOT NULL,
                        model_id TEXT NOT NULL,
                        episode_id TEXT NOT NULL UNIQUE
                            REFERENCES organization_episodes(episode_id),
                        payload_json TEXT NOT NULL,
                        content_hash TEXT NOT NULL UNIQUE,
                        imported_at TEXT NOT NULL
                    );
                    UPDATE company_state_meta SET value = '3' WHERE key = 'schema_version';
                    """
                )
                connection.execute(
                    """
                    INSERT INTO verified_live_evidence_pairs VALUES(
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        pair.pair_id,
                        pair.baseline_evidence_id,
                        pair.dynamic_evidence_id,
                        pair.baseline_content_hash,
                        pair.dynamic_content_hash,
                        pair.source_revision,
                        pair.fixture,
                        pair.provider_kind,
                        pair.model_id,
                        pair.episode.episode_id,
                        json.dumps(legacy_payload, sort_keys=True, separators=(",", ":")),
                        legacy_hash,
                        "2026-07-14T00:00:00+00:00",
                    ),
                )

            with CompanyStateStore(state) as migrated:
                values = migrated.list_live_evidence_pairs()
                self.assertEqual(migrated.schema_version(), 9)
                self.assertEqual(len(values), 1)
                self.assertTrue(values[0]["campaign_id"].startswith("legacy-campaign-"))
                self.assertTrue(values[0]["baseline_run_id"].startswith("legacy-live-evidence-"))
                self.assertEqual(len(migrated.list_episodes()), 1)

    def test_tampered_preflight_unconfirmed_and_unsafe_records_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline_path, dynamic_path = _write_pair(root, "revision-c3", 3)
            tampered = json.loads(dynamic_path.read_text(encoding="utf-8"))
            tampered["result"]["score"]["quality_score"] = 0.1
            dynamic_path.write_text(json.dumps(tampered), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "content hash"):
                verify_live_evidence_pair(baseline_path, dynamic_path)

            baseline_path, dynamic_path = _write_pair(root, "revision-d4", 4)
            invalid = json.loads(dynamic_path.read_text(encoding="utf-8"))
            invalid["schema_version"] = "noruct.live-coding-preflight.v1"
            invalid.pop("evidence_id")
            invalid.pop("content_hash")
            digest = content_digest(invalid)
            invalid["evidence_id"] = f"live-evidence-{digest[:24]}"
            invalid["content_hash"] = digest
            dynamic_path.write_text(json.dumps(invalid), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "v3"):
                verify_live_evidence_pair(baseline_path, dynamic_path)

            baseline_path, dynamic_path = _write_pair(root, "revision-e5", 5)
            invalid = json.loads(dynamic_path.read_text(encoding="utf-8"))
            invalid["quota_confirmed"] = False
            invalid.pop("evidence_id")
            invalid.pop("content_hash")
            digest = content_digest(invalid)
            invalid["evidence_id"] = f"live-evidence-{digest[:24]}"
            invalid["content_hash"] = digest
            dynamic_path.write_text(json.dumps(invalid), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "quota"):
                verify_live_evidence_pair(baseline_path, dynamic_path)

    def test_duplicate_hash_and_run_ids_do_not_append_episode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = _write_pair(root, "revision-f6", 6)
            pair = verify_live_evidence_pair(*paths)
            with CompanyStateStore(root / "company.db") as store:
                store.import_live_evidence_pair(pair)
                with self.assertRaisesRegex(ValueError, "duplicate"):
                    store.import_live_evidence_pair(pair)
                self.assertEqual(len(store.list_episodes()), 1)
                self.assertTrue(store.live_evidence_conflicts(pair))


if __name__ == "__main__":
    unittest.main()
