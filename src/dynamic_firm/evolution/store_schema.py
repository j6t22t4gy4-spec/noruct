from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from dynamic_firm.company.models import canonical_json, content_digest
from dynamic_firm.runtime.models import utc_now

from .store_primitives import (
    EVOLUTION_STORE_SCHEMA_VERSION,
    UnsupportedEvolutionStoreSchemaError,
)
from .artifact_origin import unknown_legacy_origin


class EvolutionStoreSchemaMixin:
    """Own local Evolution schema bootstrap and compatible migrations."""
    def _initialize_schema(self) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS evolution_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            version_row = connection.execute(
                "SELECT value FROM evolution_meta WHERE key = 'schema_version'"
            ).fetchone()
            previous_schema_version = (
                None if version_row is None else int(str(version_row["value"]))
            )
            if previous_schema_version not in {
                None,
                15,
                16,
                17,
                18,
                EVOLUTION_STORE_SCHEMA_VERSION,
            }:
                raise UnsupportedEvolutionStoreSchemaError(
                    "Unsupported Evolution store schema version: "
                    f"{previous_schema_version}; expected 15, 16, 17, 18, or "
                    f"{EVOLUTION_STORE_SCHEMA_VERSION}"
                )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS evolution_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evolution_consents (
                    consent_id TEXT PRIMARY KEY,
                    purpose TEXT NOT NULL,
                    allowed_reuse TEXT NOT NULL,
                    retention_days INTEGER NOT NULL,
                    authority TEXT NOT NULL,
                    status TEXT NOT NULL,
                    granted_at TEXT NOT NULL,
                    withdrawn_at TEXT
                );
                CREATE TABLE IF NOT EXISTS learning_capsules (
                    capsule_id TEXT PRIMARY KEY,
                    consent_id TEXT NOT NULL REFERENCES evolution_consents(consent_id),
                    payload_json TEXT,
                    payload_digest TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    withdrawn_at TEXT,
                    transport_state TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS employee_blueprints (
                    blueprint_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    role TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,
                    manifest_digest TEXT NOT NULL,
                    imported_at TEXT NOT NULL,
                    PRIMARY KEY (blueprint_id, version)
                );
                CREATE TABLE IF NOT EXISTS blueprint_selections (
                    selection_id TEXT PRIMARY KEY,
                    role TEXT NOT NULL,
                    blueprint_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    selected_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    replaced_selection_id TEXT,
                    FOREIGN KEY (blueprint_id, version)
                        REFERENCES employee_blueprints(blueprint_id, version)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS active_blueprint_selection_per_role
                    ON blueprint_selections(role) WHERE status = 'ACTIVE';
                CREATE TABLE IF NOT EXISTS evolution_evidence_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_digest TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS blueprint_release_candidates (
                    candidate_id TEXT PRIMARY KEY,
                    blueprint_id TEXT NOT NULL,
                    base_version TEXT NOT NULL,
                    candidate_version TEXT NOT NULL,
                    delta_json TEXT NOT NULL,
                    delta_digest TEXT NOT NULL,
                    holdout_json TEXT NOT NULL,
                    holdout_digest TEXT NOT NULL,
                    status TEXT NOT NULL,
                    revocation_reason TEXT,
                    created_at TEXT NOT NULL,
                    revoked_at TEXT
                );
                CREATE TABLE IF NOT EXISTS release_candidate_capsules (
                    candidate_id TEXT NOT NULL REFERENCES blueprint_release_candidates(candidate_id),
                    capsule_id TEXT NOT NULL REFERENCES learning_capsules(capsule_id),
                    capsule_digest TEXT NOT NULL,
                    PRIMARY KEY(candidate_id, capsule_id)
                );
                CREATE TABLE IF NOT EXISTS release_candidate_reviews (
                    review_id TEXT PRIMARY KEY,
                    candidate_id TEXT NOT NULL REFERENCES blueprint_release_candidates(candidate_id),
                    operator_id TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    candidate_digest TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS release_candidate_signatures (
                    signature_id TEXT PRIMARY KEY,
                    candidate_id TEXT NOT NULL REFERENCES blueprint_release_candidates(candidate_id),
                    algorithm TEXT NOT NULL,
                    principal TEXT NOT NULL,
                    payload_digest TEXT NOT NULL,
                    signature_digest TEXT NOT NULL,
                    allowed_signers_digest TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS local_blueprint_registry_releases (
                    release_id TEXT PRIMARY KEY,
                    candidate_id TEXT NOT NULL UNIQUE REFERENCES blueprint_release_candidates(candidate_id),
                    blueprint_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,
                    manifest_digest TEXT NOT NULL,
                    status TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    revoked_at TEXT,
                    UNIQUE(blueprint_id, version)
                );
                CREATE TABLE IF NOT EXISTS tenant_registry_adoptions (
                    adoption_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    release_id TEXT NOT NULL REFERENCES local_blueprint_registry_releases(release_id),
                    status TEXT NOT NULL,
                    replaced_adoption_id TEXT,
                    adopted_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS active_tenant_registry_role
                    ON tenant_registry_adoptions(tenant_id, role) WHERE status = 'ACTIVE';
                CREATE TABLE IF NOT EXISTS trusted_registry_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    source_label TEXT NOT NULL,
                    registry_id TEXT NOT NULL,
                    bundle_digest TEXT NOT NULL,
                    signer_principal TEXT NOT NULL,
                    signature_digest TEXT NOT NULL,
                    allowed_signers_digest TEXT NOT NULL,
                    status TEXT NOT NULL,
                    verified_at TEXT NOT NULL,
                    UNIQUE(source_label, bundle_digest)
                );
                CREATE TABLE IF NOT EXISTS staged_registry_releases (
                    snapshot_id TEXT NOT NULL REFERENCES trusted_registry_snapshots(snapshot_id),
                    remote_release_id TEXT NOT NULL,
                    blueprint_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,
                    manifest_digest TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    PRIMARY KEY(snapshot_id, remote_release_id)
                );
                CREATE TABLE IF NOT EXISTS registry_signer_trust_roots (
                    trust_root_id TEXT PRIMARY KEY,
                    source_label TEXT NOT NULL,
                    signer_principal TEXT NOT NULL,
                    allowed_signers_digest TEXT NOT NULL,
                    operator_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    registered_at TEXT NOT NULL,
                    retired_at TEXT,
                    revoked_at TEXT,
                    revocation_reason TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS active_registry_signer_source_principal
                    ON registry_signer_trust_roots(source_label, signer_principal)
                    WHERE status = 'ACTIVE';
                CREATE TABLE IF NOT EXISTS staged_registry_snapshot_reviews (
                    review_id TEXT PRIMARY KEY,
                    snapshot_id TEXT NOT NULL UNIQUE REFERENCES trusted_registry_snapshots(snapshot_id),
                    operator_id TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    compatibility_digest TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS remote_tenant_adoption_candidates (
                    candidate_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL REFERENCES trusted_registry_snapshots(snapshot_id),
                    remote_release_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    status TEXT NOT NULL,
                    operator_id TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT
                );
                CREATE TABLE IF NOT EXISTS hosted_capsule_receipts (
                    capsule_id TEXT PRIMARY KEY REFERENCES learning_capsules(capsule_id),
                    endpoint_origin TEXT NOT NULL,
                    contribution_id TEXT NOT NULL,
                    submission_receipt_digest TEXT NOT NULL,
                    submitted_at TEXT NOT NULL,
                    withdrawal_receipt_digest TEXT,
                    withdrawn_at TEXT
                );
                CREATE TABLE IF NOT EXISTS evolution_artifact_versions (
                    artifact_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    release_channel TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,
                    manifest_digest TEXT NOT NULL,
                    passport_json TEXT,
                    origin_kind TEXT NOT NULL,
                    origin_metadata_json TEXT NOT NULL,
                    available_at TEXT NOT NULL,
                    PRIMARY KEY (artifact_id, version)
                );
                CREATE TABLE IF NOT EXISTS evolution_artifact_installations (
                    installation_id TEXT PRIMARY KEY,
                    artifact_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    staged_at TEXT NOT NULL,
                    installed_at TEXT,
                    UNIQUE(artifact_id, version),
                    FOREIGN KEY (artifact_id, version)
                        REFERENCES evolution_artifact_versions(artifact_id, version)
                );
                CREATE TABLE IF NOT EXISTS evolution_artifact_activations (
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
                CREATE UNIQUE INDEX IF NOT EXISTS active_evolution_artifact_per_scope_artifact
                    ON evolution_artifact_activations(scope_key, artifact_id)
                    WHERE status = 'ACTIVE';
                CREATE TABLE IF NOT EXISTS evolution_update_subscriptions (
                    subscription_id TEXT PRIMARY KEY,
                    scope_key TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    artifact_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(scope_key, kind, artifact_id)
                );
                CREATE TABLE IF NOT EXISTS evolution_artifact_shadow_receipts (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    schema TEXT NOT NULL,
                    receipt_id TEXT NOT NULL UNIQUE,
                    slot_digest TEXT NOT NULL,
                    scope_key TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    artifact_id TEXT NOT NULL,
                    base_version TEXT NOT NULL,
                    base_manifest_digest TEXT NOT NULL,
                    base_contract_digest TEXT NOT NULL,
                    base_required_capabilities_digest TEXT NOT NULL,
                    candidate_version TEXT NOT NULL,
                    candidate_manifest_digest TEXT NOT NULL,
                    candidate_contract_digest TEXT NOT NULL,
                    candidate_required_capabilities_digest TEXT NOT NULL,
                    fixture_kind TEXT NOT NULL,
                    fixture_id TEXT NOT NULL,
                    fixture_version TEXT NOT NULL,
                    fixture_digest TEXT NOT NULL,
                    baseline_quality TEXT NOT NULL,
                    candidate_quality TEXT NOT NULL,
                    baseline_safety TEXT NOT NULL,
                    candidate_safety TEXT NOT NULL,
                    baseline_cost TEXT NOT NULL,
                    candidate_cost TEXT NOT NULL,
                    cost_ceiling TEXT NOT NULL,
                    terminal_state TEXT NOT NULL,
                    complete INTEGER NOT NULL,
                    attempt_count INTEGER NOT NULL,
                    failure_count INTEGER NOT NULL,
                    failure_history_digest TEXT NOT NULL,
                    result TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    receipt_digest TEXT NOT NULL,
                    FOREIGN KEY (artifact_id, base_version)
                        REFERENCES evolution_artifact_versions(artifact_id, version),
                    FOREIGN KEY (artifact_id, candidate_version)
                        REFERENCES evolution_artifact_versions(artifact_id, version)
                );
                CREATE INDEX IF NOT EXISTS artifact_shadow_receipts_by_candidate
                    ON evolution_artifact_shadow_receipts(
                        scope_key, artifact_id, candidate_version, sequence
                    );
                CREATE TRIGGER IF NOT EXISTS immutable_artifact_shadow_receipt_update
                BEFORE UPDATE ON evolution_artifact_shadow_receipts
                BEGIN
                    SELECT RAISE(ABORT, 'Artifact shadow receipts are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS immutable_artifact_shadow_receipt_delete
                BEFORE DELETE ON evolution_artifact_shadow_receipts
                BEGIN
                    SELECT RAISE(ABORT, 'Artifact shadow receipts are append-only');
                END;
                CREATE TABLE IF NOT EXISTS evolution_artifact_regression_signals (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    schema TEXT NOT NULL,
                    signal_id TEXT NOT NULL UNIQUE,
                    scope_key TEXT NOT NULL,
                    artifact_id TEXT NOT NULL,
                    activation_id TEXT NOT NULL REFERENCES evolution_artifact_activations(activation_id),
                    signal_kind TEXT NOT NULL,
                    evidence_digest TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    receipt_digest TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS artifact_regression_signals_by_subject
                    ON evolution_artifact_regression_signals(scope_key, artifact_id, sequence);
                CREATE TRIGGER IF NOT EXISTS immutable_artifact_regression_signal_update
                BEFORE UPDATE ON evolution_artifact_regression_signals
                BEGIN
                    SELECT RAISE(ABORT, 'Artifact regression signals are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS immutable_artifact_regression_signal_delete
                BEFORE DELETE ON evolution_artifact_regression_signals
                BEGIN
                    SELECT RAISE(ABORT, 'Artifact regression signals are append-only');
                END;
                -- The Network layer deliberately reuses this local catalog
                -- rather than introducing a second state authority.  Source
                -- identity and update posture are stored separately from an
                -- Artifact manifest: an Artifact stays immutable even when a
                -- trusted publisher rotates or a local operator changes how
                -- it wants future releases delivered.
                CREATE TABLE IF NOT EXISTS noruct_network_sources (
                    source_id TEXT PRIMARY KEY,
                    publisher_class TEXT NOT NULL,
                    origin TEXT NOT NULL,
                    signer_principal TEXT NOT NULL,
                    allowed_signers_path TEXT NOT NULL,
                    ssh_keygen_path TEXT NOT NULL,
                    credential_env TEXT,
                    private_registry_id TEXT,
                    status TEXT NOT NULL,
                    auto_update_enabled INTEGER NOT NULL,
                    allow_insecure_loopback INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS noruct_network_artifact_provenance (
                    artifact_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    source_id TEXT NOT NULL REFERENCES noruct_network_sources(source_id),
                    registry_id TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL REFERENCES trusted_artifact_registry_snapshots(snapshot_id),
                    publisher_class TEXT NOT NULL,
                    provenance_digest TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    PRIMARY KEY (artifact_id, version)
                );
                CREATE TABLE IF NOT EXISTS noruct_network_update_preferences (
                    scope_key TEXT NOT NULL,
                    artifact_id TEXT NOT NULL,
                    source_id TEXT NOT NULL REFERENCES noruct_network_sources(source_id),
                    mode TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (scope_key, artifact_id)
                );
                CREATE TABLE IF NOT EXISTS evolution_job_artifact_pins (
                    job_id TEXT NOT NULL,
                    scope_key TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    artifact_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    manifest_digest TEXT NOT NULL,
                    pinned_at TEXT NOT NULL,
                    PRIMARY KEY(job_id, artifact_id),
                    FOREIGN KEY (artifact_id, version)
                        REFERENCES evolution_artifact_versions(artifact_id, version)
                );
                CREATE TABLE IF NOT EXISTS evolution_job_artifact_snapshots (
                    job_id TEXT PRIMARY KEY,
                    scope_key TEXT NOT NULL,
                    pinned_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evolution_job_runtime_artifact_pins (
                    job_id TEXT NOT NULL,
                    scope_key TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    artifact_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    manifest_digest TEXT NOT NULL,
                    pinned_at TEXT NOT NULL,
                    PRIMARY KEY(job_id, scope_key, artifact_id),
                    FOREIGN KEY (artifact_id, version)
                        REFERENCES evolution_artifact_versions(artifact_id, version)
                );
                CREATE TABLE IF NOT EXISTS evolution_job_runtime_artifact_snapshots (
                    job_id TEXT PRIMARY KEY,
                    scope_keys_json TEXT NOT NULL,
                    pinned_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS trusted_artifact_registry_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    source_label TEXT NOT NULL,
                    registry_id TEXT NOT NULL,
                    bundle_digest TEXT NOT NULL,
                    signer_principal TEXT NOT NULL,
                    signature_digest TEXT NOT NULL,
                    allowed_signers_digest TEXT NOT NULL,
                    status TEXT NOT NULL,
                    verified_at TEXT NOT NULL,
                    UNIQUE(source_label, bundle_digest)
                );
                CREATE TABLE IF NOT EXISTS staged_artifact_registry_entries (
                    snapshot_id TEXT NOT NULL REFERENCES trusted_artifact_registry_snapshots(snapshot_id),
                    artifact_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    release_channel TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,
                    manifest_digest TEXT NOT NULL,
                    status TEXT NOT NULL,
                    PRIMARY KEY(snapshot_id, artifact_id, version)
                );
                CREATE TABLE IF NOT EXISTS artifact_registry_snapshot_reviews (
                    review_id TEXT PRIMARY KEY,
                    snapshot_id TEXT NOT NULL UNIQUE REFERENCES trusted_artifact_registry_snapshots(snapshot_id),
                    operator_id TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    compatibility_digest TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                );
                """
            )
            if previous_schema_version == 15:
                self._migrate_v15_to_v16(connection)
            if previous_schema_version in {15, 16}:
                self._migrate_v16_to_v17(
                    connection, prior_schema_version=previous_schema_version
                )
            # v16 introduced Network before authenticated private-team
            # sources.  Keep the local store backward compatible by adding
            # only this non-secret environment-variable reference.  The raw
            # token remains outside SQLite and outside Company authority.
            try:
                connection.execute(
                    "ALTER TABLE noruct_network_sources ADD COLUMN credential_env TEXT"
                )
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
            try:
                connection.execute(
                    "ALTER TABLE noruct_network_sources ADD COLUMN private_registry_id TEXT"
                )
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
            connection.execute(
                "INSERT INTO evolution_meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(EVOLUTION_STORE_SCHEMA_VERSION),),
            )

    @staticmethod
    def _migrate_v15_to_v16(connection: sqlite3.Connection) -> None:
        """Widen active Artifact identity without losing v15 history or pins."""

        connection.execute("DROP INDEX IF EXISTS active_evolution_artifact_per_scope_kind")
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS active_evolution_artifact_per_scope_artifact
                ON evolution_artifact_activations(scope_key, artifact_id)
                WHERE status = 'ACTIVE'
            """
        )

        table_migrations = (
            (
                "evolution_job_artifact_pins",
                """
                CREATE TABLE evolution_job_artifact_pins_v16 (
                    job_id TEXT NOT NULL,
                    scope_key TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    artifact_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    manifest_digest TEXT NOT NULL,
                    pinned_at TEXT NOT NULL,
                    PRIMARY KEY(job_id, artifact_id),
                    FOREIGN KEY (artifact_id, version)
                        REFERENCES evolution_artifact_versions(artifact_id, version)
                )
                """,
            ),
            (
                "evolution_job_runtime_artifact_pins",
                """
                CREATE TABLE evolution_job_runtime_artifact_pins_v16 (
                    job_id TEXT NOT NULL,
                    scope_key TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    artifact_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    manifest_digest TEXT NOT NULL,
                    pinned_at TEXT NOT NULL,
                    PRIMARY KEY(job_id, scope_key, artifact_id),
                    FOREIGN KEY (artifact_id, version)
                        REFERENCES evolution_artifact_versions(artifact_id, version)
                )
                """,
            ),
        )
        columns = (
            "job_id, scope_key, kind, artifact_id, version, manifest_digest, pinned_at"
        )
        for table_name, create_sql in table_migrations:
            replacement_name = f"{table_name}_v16"
            before_count = int(
                connection.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
            )
            connection.execute(f"DROP TABLE IF EXISTS {replacement_name}")
            connection.execute(create_sql)
            connection.execute(
                f"INSERT INTO {replacement_name}({columns}) "
                f"SELECT {columns} FROM {table_name}"
            )
            after_count = int(
                connection.execute(f"SELECT COUNT(*) FROM {replacement_name}").fetchone()[0]
            )
            if after_count != before_count:
                raise RuntimeError(f"Evolution v16 migration did not preserve {table_name}")
            connection.execute(f"DROP TABLE {table_name}")
            connection.execute(f"ALTER TABLE {replacement_name} RENAME TO {table_name}")

    @staticmethod
    def _migrate_v16_to_v17(
        connection: sqlite3.Connection, *, prior_schema_version: int
    ) -> None:
        """Classify pre-origin rows conservatively without inventing provenance."""

        columns = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(evolution_artifact_versions)"
            ).fetchall()
        }
        if "origin_kind" not in columns:
            connection.execute(
                "ALTER TABLE evolution_artifact_versions "
                "ADD COLUMN origin_kind TEXT NOT NULL DEFAULT 'UNKNOWN_LEGACY'"
            )
        if "origin_metadata_json" not in columns:
            metadata = canonical_json(
                unknown_legacy_origin(prior_schema_version=prior_schema_version)
            )
            connection.execute(
                "ALTER TABLE evolution_artifact_versions "
                "ADD COLUMN origin_metadata_json TEXT NOT NULL DEFAULT "
                + "'" + metadata.replace("'", "''") + "'"
            )
