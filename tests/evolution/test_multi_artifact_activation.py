from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dynamic_firm.company.models import canonical_json, content_digest
from dynamic_firm.evolution.service import EvolutionNetworkService
from dynamic_firm.evolution.store import EVOLUTION_STORE_SCHEMA_VERSION, EvolutionStore


def _skill_artifact(artifact_id: str, version: str) -> dict[str, object]:
    return {
        "schema": "noruct.evolution-artifact.v1",
        "artifact_id": artifact_id,
        "version": version,
        "kind": "SKILL_PACKAGE",
        "release_channel": "STABLE",
        "compatibility": {
            "runtime_contract": "noruct_v1",
            "required_capabilities": ["workspace_read"],
        },
        "content": {
            "skill_key": artifact_id,
            "applies_to": ["repository_analysis"],
            "steps": [f"Apply {artifact_id} at {version}"],
            "required_capabilities": [],
        },
    }


class MultiArtifactActivationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = EvolutionStore(self.root / "evolution.db")
        self.service = EvolutionNetworkService(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def _install(self, artifact_id: str, version: str) -> None:
        self.store.register_artifact_version(_skill_artifact(artifact_id, version))
        self.store.stage_artifact_version(artifact_id, version)
        self.store.install_artifact_version(artifact_id, version)

    def test_same_kind_artifacts_coexist_pin_and_rollback_independently(self) -> None:
        for artifact_id in ("zeta_skill", "alpha_skill"):
            for version in ("1.0.0", "1.1.0"):
                self._install(artifact_id, version)

        for artifact_id in ("zeta_skill", "alpha_skill"):
            self.store.activate_artifact_version(
                scope_key="company_default",
                artifact_id=artifact_id,
                version="1.0.0",
                activation_reason="TEST_INITIAL",
            )

        active = self.store.list_active_artifact_activations("company_default")
        self.assertEqual([item["artifact_id"] for item in active], ["alpha_skill", "zeta_skill"])

        job_pins = self.store.pin_active_artifacts_for_job(
            job_id="job_multi", scope_key="company_default"
        )
        runtime_pins = self.store.pin_active_artifacts_for_runtime_job(
            job_id="job_runtime_multi", scope_keys=("company_default",)
        )
        self.assertEqual([item["artifact_id"] for item in job_pins], ["alpha_skill", "zeta_skill"])
        self.assertEqual(
            [item["artifact_id"] for item in runtime_pins], ["alpha_skill", "zeta_skill"]
        )

        self.store.activate_artifact_version(
            scope_key="company_default",
            artifact_id="alpha_skill",
            version="1.1.0",
            activation_reason="TEST_UPDATE",
        )
        current = {
            str(item["artifact_id"]): str(item["version"])
            for item in self.store.list_active_artifact_activations("company_default")
        }
        self.assertEqual(current, {"alpha_skill": "1.1.0", "zeta_skill": "1.0.0"})

        with self.assertRaisesRegex(
            ValueError, "Multiple active Artifacts share this kind; provide artifact_id"
        ):
            self.service.rollback_artifact(
                scope_key="company_default", kind="SKILL_PACKAGE"
            )

        restored = self.service.rollback_artifact(
            scope_key="company_default", artifact_id="alpha_skill"
        )
        self.assertEqual(restored["artifact_id"], "alpha_skill")
        self.assertEqual(restored["version"], "1.0.0")
        current = {
            str(item["artifact_id"]): str(item["version"])
            for item in self.store.list_active_artifact_activations("company_default")
        }
        self.assertEqual(current, {"alpha_skill": "1.0.0", "zeta_skill": "1.0.0"})

        self.assertEqual(
            [item["version"] for item in self.store.list_job_artifact_pins("job_multi")],
            ["1.0.0", "1.0.0"],
        )


class EvolutionStoreV17MigrationTests(unittest.TestCase):
    def test_v15_artifact_history_and_pins_migrate_without_loss(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "evolution-v15.db"
            artifact = _skill_artifact("legacy_skill", "1.0.0")
            digest = content_digest(artifact)
            with sqlite3.connect(path) as connection:
                connection.executescript(
                    """
                    PRAGMA foreign_keys = ON;
                    CREATE TABLE evolution_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE evolution_artifact_versions (
                        artifact_id TEXT NOT NULL,
                        version TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        release_channel TEXT NOT NULL,
                        manifest_json TEXT NOT NULL,
                        manifest_digest TEXT NOT NULL,
                        passport_json TEXT,
                        available_at TEXT NOT NULL,
                        PRIMARY KEY (artifact_id, version)
                    );
                    CREATE TABLE evolution_artifact_activations (
                        activation_id TEXT PRIMARY KEY,
                        scope_key TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        artifact_id TEXT NOT NULL,
                        version TEXT NOT NULL,
                        status TEXT NOT NULL,
                        activation_reason TEXT NOT NULL,
                        activated_at TEXT NOT NULL,
                        replaced_activation_id TEXT,
                        FOREIGN KEY (artifact_id, version)
                            REFERENCES evolution_artifact_versions(artifact_id, version)
                    );
                    CREATE UNIQUE INDEX active_evolution_artifact_per_scope_kind
                        ON evolution_artifact_activations(scope_key, kind)
                        WHERE status = 'ACTIVE';
                    CREATE TABLE evolution_job_artifact_pins (
                        job_id TEXT NOT NULL,
                        scope_key TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        artifact_id TEXT NOT NULL,
                        version TEXT NOT NULL,
                        manifest_digest TEXT NOT NULL,
                        pinned_at TEXT NOT NULL,
                        PRIMARY KEY(job_id, kind),
                        FOREIGN KEY (artifact_id, version)
                            REFERENCES evolution_artifact_versions(artifact_id, version)
                    );
                    CREATE TABLE evolution_job_runtime_artifact_pins (
                        job_id TEXT NOT NULL,
                        scope_key TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        artifact_id TEXT NOT NULL,
                        version TEXT NOT NULL,
                        manifest_digest TEXT NOT NULL,
                        pinned_at TEXT NOT NULL,
                        PRIMARY KEY(job_id, scope_key, kind),
                        FOREIGN KEY (artifact_id, version)
                            REFERENCES evolution_artifact_versions(artifact_id, version)
                    );
                    """
                )
                connection.execute(
                    "INSERT INTO evolution_meta(key, value) VALUES('schema_version', '15')"
                )
                connection.execute(
                    """
                    INSERT INTO evolution_artifact_versions VALUES(
                        ?, '1.0.0', 'SKILL_PACKAGE', 'STABLE', ?, ?, NULL,
                        '2026-07-21T00:00:00+00:00'
                    )
                    """,
                    (artifact["artifact_id"], canonical_json(artifact), digest),
                )
                connection.execute(
                    """
                    INSERT INTO evolution_artifact_activations VALUES(
                        'activation-v15', 'company_default', 'SKILL_PACKAGE', ?, '1.0.0',
                        'ACTIVE', 'V15_FIXTURE', '2026-07-21T00:01:00+00:00', NULL
                    )
                    """,
                    (artifact["artifact_id"],),
                )
                pin = (
                    "company_default",
                    "SKILL_PACKAGE",
                    artifact["artifact_id"],
                    "1.0.0",
                    digest,
                    "2026-07-21T00:02:00+00:00",
                )
                connection.execute(
                    "INSERT INTO evolution_job_artifact_pins VALUES('job-v15', ?, ?, ?, ?, ?, ?)",
                    pin,
                )
                connection.execute(
                    """
                    INSERT INTO evolution_job_runtime_artifact_pins
                    VALUES('runtime-job-v15', ?, ?, ?, ?, ?, ?)
                    """,
                    pin,
                )

            with EvolutionStore(path) as store:
                self.assertEqual(store.status()["schema_version"], EVOLUTION_STORE_SCHEMA_VERSION)
                self.assertEqual(
                    store.list_job_artifact_pins("job-v15")[0]["artifact_id"], "legacy_skill"
                )
                self.assertEqual(
                    store.list_runtime_job_artifact_pins("runtime-job-v15")[0]["artifact_id"],
                    "legacy_skill",
                )
                legacy = store.get_artifact_version("legacy_skill", "1.0.0")
                self.assertEqual(legacy["origin_kind"], "UNKNOWN_LEGACY")
                self.assertEqual(
                    legacy["origin_metadata"]["migration"],
                    "LEGACY_ORIGIN_UNAVAILABLE",
                )

                store.register_artifact_version(_skill_artifact("second_skill", "1.0.0"))
                store.stage_artifact_version("second_skill", "1.0.0")
                store.install_artifact_version("second_skill", "1.0.0")
                store.activate_artifact_version(
                    scope_key="company_default",
                    artifact_id="second_skill",
                    version="1.0.0",
                    activation_reason="POST_MIGRATION",
                )
                self.assertEqual(
                    [
                        item["artifact_id"]
                        for item in store.list_active_artifact_activations("company_default")
                    ],
                    ["legacy_skill", "second_skill"],
                )
                self.assertEqual(
                    len(
                        store.pin_active_artifacts_for_job(
                            job_id="job-v16", scope_key="company_default"
                        )
                    ),
                    2,
                )

                job_pk = [
                    str(row["name"])
                    for row in store._conn.execute(  # noqa: SLF001 - migration contract assertion
                        "PRAGMA table_info(evolution_job_artifact_pins)"
                    ).fetchall()
                    if int(row["pk"]) > 0
                ]
                runtime_pk = [
                    str(row["name"])
                    for row in store._conn.execute(  # noqa: SLF001 - migration contract assertion
                        "PRAGMA table_info(evolution_job_runtime_artifact_pins)"
                    ).fetchall()
                    if int(row["pk"]) > 0
                ]
                self.assertEqual(job_pk, ["job_id", "artifact_id"])
                self.assertEqual(runtime_pk, ["job_id", "scope_key", "artifact_id"])

    def test_v16_artifact_without_origin_migrates_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "evolution-v16.db"
            artifact = _skill_artifact("v16_skill", "1.0.0")
            with sqlite3.connect(path) as connection:
                connection.executescript(
                    """
                    CREATE TABLE evolution_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    CREATE TABLE evolution_artifact_versions (
                        artifact_id TEXT NOT NULL,
                        version TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        release_channel TEXT NOT NULL,
                        manifest_json TEXT NOT NULL,
                        manifest_digest TEXT NOT NULL,
                        passport_json TEXT,
                        available_at TEXT NOT NULL,
                        PRIMARY KEY (artifact_id, version)
                    );
                    """
                )
                connection.execute(
                    "INSERT INTO evolution_meta(key, value) VALUES('schema_version', '16')"
                )
                connection.execute(
                    """
                    INSERT INTO evolution_artifact_versions VALUES(
                        ?, '1.0.0', 'SKILL_PACKAGE', 'STABLE', ?, ?, NULL,
                        '2026-07-30T00:00:00+00:00'
                    )
                    """,
                    (
                        artifact["artifact_id"],
                        canonical_json(artifact),
                        content_digest(artifact),
                    ),
                )

            with EvolutionStore(path) as store:
                migrated = store.get_artifact_version("v16_skill", "1.0.0")
                self.assertEqual(
                    store.status()["schema_version"], EVOLUTION_STORE_SCHEMA_VERSION
                )
                self.assertEqual(migrated["origin_kind"], "UNKNOWN_LEGACY")
                self.assertEqual(
                    migrated["origin_metadata"]["prior_schema_version"], 16
                )


if __name__ == "__main__":
    unittest.main()
