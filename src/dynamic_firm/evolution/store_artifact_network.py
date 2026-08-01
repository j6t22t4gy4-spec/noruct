from __future__ import annotations

from datetime import timedelta
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from dynamic_firm.company.models import canonical_json, content_digest
from dynamic_firm.runtime.models import utc_now

from .artifact_origin import (
    ArtifactOriginKind,
    user_imported_origin,
    validate_artifact_origin,
)


class EvolutionArtifactNetworkMixin:
    """Own artifact provenance, installation, activation, and Job pin lifecycle."""
    @classmethod
    def _artifact_row(cls, row: sqlite3.Row | None) -> dict[str, Any] | None:
        result = cls._row(row)
        if result is None:
            return None
        passport = result.get("passport_json")
        if passport is not None:
            result["passport"] = json.loads(str(passport))
        result.pop("passport_json", None)
        origin_metadata = result.get("origin_metadata_json")
        if origin_metadata is not None:
            result["origin_metadata"] = json.loads(str(origin_metadata))
        result.pop("origin_metadata_json", None)
        return result

    def register_artifact_version(
        self,
        manifest: Mapping[str, Any],
        *,
        origin_kind: ArtifactOriginKind | str = ArtifactOriginKind.USER_IMPORTED,
        origin_metadata: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        """Add an immutable, already-validated Artifact to the local catalog.

        Registering is deliberately catalog-only: it does not stage, install,
        activate, alter a Company, or start an update tracker.
        """
        artifact_id = str(manifest["artifact_id"])
        version = str(manifest["version"])
        digest = content_digest(manifest)
        passport = manifest.get("passport")
        origin, metadata = validate_artifact_origin(
            origin_kind,
            user_imported_origin() if origin_metadata is None else origin_metadata,
        )
        available_at = utc_now().isoformat()
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT manifest_digest, origin_kind, origin_metadata_json "
                "FROM evolution_artifact_versions WHERE artifact_id = ? AND version = ?",
                (artifact_id, version),
            ).fetchone()
            if existing is not None:
                if str(existing["manifest_digest"]) != digest:
                    raise ValueError("An Artifact id/version is immutable; register a new version for changed content")
                if (
                    str(existing["origin_kind"]) != origin
                    or json.loads(str(existing["origin_metadata_json"])) != metadata
                ):
                    raise ValueError(
                        "An Artifact id/version origin is immutable and cannot be reclassified"
                    )
                return self.get_artifact_version(artifact_id, version)
            connection.execute(
                """
                INSERT INTO evolution_artifact_versions(
                    artifact_id, version, kind, release_channel, manifest_json, manifest_digest,
                    passport_json, origin_kind, origin_metadata_json, available_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id, version, manifest["kind"], manifest["release_channel"],
                    canonical_json(manifest), digest,
                    None if passport is None else canonical_json(passport),
                    origin, canonical_json(metadata), available_at,
                ),
            )
            event = {
                "artifact_id": artifact_id,
                "version": version,
                "kind": manifest["kind"],
                "manifest_digest": digest,
                "origin_kind": origin,
                "origin_metadata": metadata,
            }
            connection.execute(
                """
                INSERT INTO evolution_evidence_events(event_type, subject_id, payload_json, payload_digest, recorded_at)
                VALUES('EVOLUTION_ARTIFACT_AVAILABLE', ?, ?, ?, ?)
                """,
                (f"{artifact_id}@{version}", canonical_json(event), content_digest(event), available_at),
            )
        return self.get_artifact_version(artifact_id, version)

    def get_artifact_version(self, artifact_id: str, version: str) -> Mapping[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM evolution_artifact_versions WHERE artifact_id = ? AND version = ?",
                (artifact_id, version),
            ).fetchone()
        result = self._artifact_row(row)
        if result is None:
            raise KeyError(f"Unknown Evolution Artifact: {artifact_id}@{version}")
        return result

    def list_artifact_versions(
        self, *, artifact_id: str | None = None, kind: str | None = None
    ) -> tuple[Mapping[str, Any], ...]:
        clauses: list[str] = []
        parameters: list[str] = []
        if artifact_id is not None:
            clauses.append("artifact_id = ?")
            parameters.append(artifact_id)
        if kind is not None:
            clauses.append("kind = ?")
            parameters.append(kind)
        where = "" if not clauses else " WHERE " + " AND ".join(clauses)
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM evolution_artifact_versions" + where + " ORDER BY artifact_id, version",
                tuple(parameters),
            ).fetchall()
        return tuple(self._artifact_row(row) for row in rows)

    # -- Noruct Network source and provenance ---------------------------------

    def upsert_network_source(
        self,
        *,
        source_id: str,
        publisher_class: str,
        origin: str,
        signer_principal: str,
        allowed_signers_path: str,
        ssh_keygen_path: str,
        credential_env: str | None,
        private_registry_id: str | None,
        auto_update_enabled: bool,
        allow_insecure_loopback: bool,
    ) -> Mapping[str, Any]:
        """Persist one local Network source without storing signing material.

        The allowed-signers file and verifier remain caller-owned local paths.
        The corresponding immutable signer digest is recorded by the existing
        trust-root contract, not duplicated here.
        """

        now = utc_now().isoformat()
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO noruct_network_sources(
                    source_id, publisher_class, origin, signer_principal,
                    allowed_signers_path, ssh_keygen_path, credential_env, private_registry_id, status,
                    auto_update_enabled, allow_insecure_loopback, created_at,
                    updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    publisher_class = excluded.publisher_class,
                    origin = excluded.origin,
                    signer_principal = excluded.signer_principal,
                    allowed_signers_path = excluded.allowed_signers_path,
                    ssh_keygen_path = excluded.ssh_keygen_path,
                    credential_env = excluded.credential_env,
                    private_registry_id = excluded.private_registry_id,
                    status = 'ACTIVE',
                    auto_update_enabled = excluded.auto_update_enabled,
                    allow_insecure_loopback = excluded.allow_insecure_loopback,
                    updated_at = excluded.updated_at
                """,
                (
                    source_id,
                    publisher_class,
                    origin,
                    signer_principal,
                    allowed_signers_path,
                    ssh_keygen_path,
                    credential_env,
                    private_registry_id,
                    int(auto_update_enabled),
                    int(allow_insecure_loopback),
                    now,
                    now,
                ),
            )
        return self.get_network_source(source_id)

    def get_network_source(self, source_id: str) -> Mapping[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM noruct_network_sources WHERE source_id = ?",
                (source_id,),
            ).fetchone()
        result = self._row(row)
        if result is None:
            raise KeyError(f"Unknown Noruct Network source: {source_id}")
        result["auto_update_enabled"] = bool(result["auto_update_enabled"])
        result["allow_insecure_loopback"] = bool(result["allow_insecure_loopback"])
        return result

    def list_network_sources(self) -> tuple[Mapping[str, Any], ...]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT source_id FROM noruct_network_sources ORDER BY source_id"
            ).fetchall()
        return tuple(self.get_network_source(str(row["source_id"])) for row in rows)

    def record_network_artifact_provenance(
        self,
        *,
        artifact_id: str,
        version: str,
        source_id: str,
        registry_id: str,
        snapshot_id: str,
    ) -> Mapping[str, Any]:
        """Bind one local immutable Artifact version to one reviewed source.

        A version cannot be silently re-attributed to another publisher.  This
        makes first-party auto-update decisions source-aware without changing
        the manifest's immutable content or the Firm's state authority.
        """

        source = self.get_network_source(source_id)
        snapshot = self.get_staged_artifact_registry_snapshot(snapshot_id)
        if snapshot["source_label"] != source_id:
            raise ValueError("Network Artifact provenance source does not match staged snapshot")
        if snapshot["registry_id"] != registry_id:
            raise ValueError("Network Artifact provenance registry does not match staged snapshot")
        artifact = self.get_artifact_version(artifact_id, version)
        if artifact["origin_kind"] != ArtifactOriginKind.NETWORK_IMPORTED.value:
            raise ValueError(
                "Network provenance cannot be attached to a non-Network Artifact origin"
            )
        now = utc_now().isoformat()
        payload = {
            "artifact_id": artifact_id,
            "version": version,
            "manifest_digest": artifact["manifest_digest"],
            "source_id": source_id,
            "publisher_class": source["publisher_class"],
            "registry_id": registry_id,
            "snapshot_id": snapshot_id,
            "bundle_digest": snapshot["bundle_digest"],
        }
        digest = content_digest(payload)
        with self._transaction() as connection:
            existing = connection.execute(
                """SELECT provenance_digest FROM noruct_network_artifact_provenance
                   WHERE artifact_id = ? AND version = ?""",
                (artifact_id, version),
            ).fetchone()
            if existing is not None:
                if str(existing["provenance_digest"]) != digest:
                    raise ValueError("An Artifact version already has different Network provenance")
                return self.get_network_artifact_provenance(artifact_id, version)
            connection.execute(
                """INSERT INTO noruct_network_artifact_provenance(
                    artifact_id, version, source_id, registry_id, snapshot_id,
                    publisher_class, provenance_digest, recorded_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    artifact_id,
                    version,
                    source_id,
                    registry_id,
                    snapshot_id,
                    source["publisher_class"],
                    digest,
                    now,
                ),
            )
            connection.execute(
                """INSERT INTO evolution_evidence_events(
                    event_type, subject_id, payload_json, payload_digest, recorded_at
                ) VALUES('NORUCT_NETWORK_ARTIFACT_PROVENANCE', ?, ?, ?, ?)""",
                (f"{artifact_id}@{version}", canonical_json(payload), digest, now),
            )
        return self.get_network_artifact_provenance(artifact_id, version)

    def get_network_artifact_provenance(
        self, artifact_id: str, version: str
    ) -> Mapping[str, Any]:
        with self._lock:
            row = self._conn.execute(
                """SELECT * FROM noruct_network_artifact_provenance
                   WHERE artifact_id = ? AND version = ?""",
                (artifact_id, version),
            ).fetchone()
        result = self._row(row)
        if result is None:
            raise KeyError(f"Artifact has no Noruct Network provenance: {artifact_id}@{version}")
        return result

    def list_network_artifacts(
        self, *, source_id: str | None = None
    ) -> tuple[Mapping[str, Any], ...]:
        clause = "" if source_id is None else " WHERE p.source_id = ?"
        parameters: tuple[object, ...] = () if source_id is None else (source_id,)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT a.*, p.source_id, p.registry_id, p.snapshot_id,
                       p.publisher_class, p.provenance_digest, p.recorded_at
                  FROM evolution_artifact_versions a
                  JOIN noruct_network_artifact_provenance p
                    ON p.artifact_id = a.artifact_id AND p.version = a.version
                """ + clause + " ORDER BY a.artifact_id, a.version",
                parameters,
            ).fetchall()
        return tuple(self._artifact_row(row) for row in rows)

    def set_network_update_preference(
        self,
        *,
        scope_key: str,
        artifact_id: str,
        source_id: str,
        mode: str,
    ) -> Mapping[str, Any]:
        self.get_network_source(source_id)
        now = utc_now().isoformat()
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO noruct_network_update_preferences(
                    scope_key, artifact_id, source_id, mode, updated_at
                ) VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(scope_key, artifact_id) DO UPDATE SET
                    source_id = excluded.source_id,
                    mode = excluded.mode,
                    updated_at = excluded.updated_at
                """,
                (scope_key, artifact_id, source_id, mode, now),
            )
        return self.get_network_update_preference(scope_key, artifact_id)

    def get_network_update_preference(
        self, scope_key: str, artifact_id: str
    ) -> Mapping[str, Any]:
        with self._lock:
            row = self._conn.execute(
                """SELECT * FROM noruct_network_update_preferences
                   WHERE scope_key = ? AND artifact_id = ?""",
                (scope_key, artifact_id),
            ).fetchone()
        result = self._row(row)
        if result is None:
            raise KeyError(
                f"No Noruct Network update preference for {scope_key}/{artifact_id}"
            )
        return result

    def list_network_update_preferences(
        self, scope_key: str
    ) -> tuple[Mapping[str, Any], ...]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT artifact_id FROM noruct_network_update_preferences
                   WHERE scope_key = ? ORDER BY artifact_id""",
                (scope_key,),
            ).fetchall()
        return tuple(
            self.get_network_update_preference(scope_key, str(row["artifact_id"]))
            for row in rows
        )

    def stage_artifact_version(self, artifact_id: str, version: str) -> Mapping[str, Any]:
        self.get_artifact_version(artifact_id, version)
        staged_at = utc_now().isoformat()
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT installation_id, status FROM evolution_artifact_installations WHERE artifact_id = ? AND version = ?",
                (artifact_id, version),
            ).fetchone()
            if existing is not None:
                return self.get_artifact_installation(str(existing["installation_id"]))
            installation_id = f"artifact-installation-{uuid.uuid4()}"
            connection.execute(
                """
                INSERT INTO evolution_artifact_installations(
                    installation_id, artifact_id, version, status, staged_at, installed_at
                ) VALUES(?, ?, ?, 'STAGED', ?, NULL)
                """,
                (installation_id, artifact_id, version, staged_at),
            )
        return self.get_artifact_installation(installation_id)

    def install_artifact_version(self, artifact_id: str, version: str) -> Mapping[str, Any]:
        installed_at = utc_now().isoformat()
        with self._transaction() as connection:
            installation = connection.execute(
                "SELECT * FROM evolution_artifact_installations WHERE artifact_id = ? AND version = ?",
                (artifact_id, version),
            ).fetchone()
            if installation is None:
                raise ValueError("Artifact must be staged before installation")
            if str(installation["status"]) == "INSTALLED":
                return self.get_artifact_installation(str(installation["installation_id"]))
            if str(installation["status"]) != "STAGED":
                raise ValueError("Only a staged Artifact may be installed")
            connection.execute(
                """
                UPDATE evolution_artifact_installations
                   SET status = 'INSTALLED', installed_at = ?
                 WHERE installation_id = ?
                """,
                (installed_at, installation["installation_id"]),
            )
            event = {"artifact_id": artifact_id, "version": version, "installation_id": installation["installation_id"]}
            connection.execute(
                """
                INSERT INTO evolution_evidence_events(event_type, subject_id, payload_json, payload_digest, recorded_at)
                VALUES('EVOLUTION_ARTIFACT_INSTALLED', ?, ?, ?, ?)
                """,
                (f"{artifact_id}@{version}", canonical_json(event), content_digest(event), installed_at),
            )
        return self.get_artifact_installation(str(installation["installation_id"]))

    def get_artifact_installation(self, installation_id: str) -> Mapping[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM evolution_artifact_installations WHERE installation_id = ?", (installation_id,)
            ).fetchone()
        result = self._row(row)
        if result is None:
            raise KeyError(f"Unknown Artifact installation: {installation_id}")
        result["artifact"] = self.get_artifact_version(str(result["artifact_id"]), str(result["version"]))
        return result

    def list_artifact_installations(self) -> tuple[Mapping[str, Any], ...]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT installation_id FROM evolution_artifact_installations ORDER BY staged_at, installation_id"
            ).fetchall()
        return tuple(self.get_artifact_installation(str(row["installation_id"])) for row in rows)

    def activate_artifact_version(
        self, *, scope_key: str, artifact_id: str, version: str, activation_reason: str
    ) -> Mapping[str, Any]:
        artifact = self.get_artifact_version(artifact_id, version)
        activated_at = utc_now().isoformat()
        with self._transaction() as connection:
            installation = connection.execute(
                "SELECT status FROM evolution_artifact_installations WHERE artifact_id = ? AND version = ?",
                (artifact_id, version),
            ).fetchone()
            if installation is None or str(installation["status"]) != "INSTALLED":
                raise ValueError("Only an installed Artifact may be activated")
            current = connection.execute(
                """
                SELECT * FROM evolution_artifact_activations
                 WHERE scope_key = ? AND artifact_id = ? AND status = 'ACTIVE'
                """,
                (scope_key, artifact_id),
            ).fetchone()
            if current is not None and str(current["artifact_id"]) == artifact_id and str(current["version"]) == version:
                return self.get_artifact_activation(str(current["activation_id"]))
            if current is not None:
                connection.execute(
                    "UPDATE evolution_artifact_activations SET status = 'SUPERSEDED' WHERE activation_id = ?",
                    (current["activation_id"],),
                )
            activation_id = f"artifact-activation-{uuid.uuid4()}"
            connection.execute(
                """
                INSERT INTO evolution_artifact_activations(
                    activation_id, scope_key, kind, artifact_id, version, status,
                    activation_reason, activated_at, replaced_activation_id
                ) VALUES(?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?)
                """,
                (
                    activation_id, scope_key, artifact["kind"], artifact_id, version,
                    activation_reason, activated_at,
                    None if current is None else current["activation_id"],
                ),
            )
            event = {"activation_id": activation_id, "scope_key": scope_key, "artifact_id": artifact_id, "version": version, "reason": activation_reason}
            connection.execute(
                """
                INSERT INTO evolution_evidence_events(event_type, subject_id, payload_json, payload_digest, recorded_at)
                VALUES('EVOLUTION_ARTIFACT_ACTIVATED', ?, ?, ?, ?)
                """,
                (activation_id, canonical_json(event), content_digest(event), activated_at),
            )
        return self.get_artifact_activation(activation_id)

    def get_artifact_activation(self, activation_id: str) -> Mapping[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM evolution_artifact_activations WHERE activation_id = ?", (activation_id,)
            ).fetchone()
        result = self._row(row)
        if result is None:
            raise KeyError(f"Unknown Artifact activation: {activation_id}")
        result["artifact"] = self.get_artifact_version(str(result["artifact_id"]), str(result["version"]))
        return result

    def list_active_artifact_activations(self, scope_key: str) -> tuple[Mapping[str, Any], ...]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT activation_id FROM evolution_artifact_activations
                 WHERE scope_key = ? AND status = 'ACTIVE'
                 ORDER BY kind, artifact_id, version, activation_id
                """,
                (scope_key,),
            ).fetchall()
        return tuple(self.get_artifact_activation(str(row["activation_id"])) for row in rows)

    def has_recent_tracker_artifact_activation(
        self, *, scope_key: str, artifact_id: str, cooldown: timedelta
    ) -> bool:
        """Keep automatic local-derived promotions sparse without touching Jobs."""

        if cooldown.total_seconds() <= 0:
            raise ValueError("Artifact promotion cooldown must be positive")
        cutoff = (utc_now() - cooldown).isoformat()
        with self._lock:
            row = self._conn.execute(
                """
                SELECT 1 FROM evolution_artifact_activations
                 WHERE scope_key = ? AND artifact_id = ?
                   AND activation_reason = 'TRACK_STABLE_SHADOW_PASS'
                   AND activated_at >= ?
                 LIMIT 1
                """,
                (scope_key, artifact_id, cutoff),
            ).fetchone()
        return row is not None

    def rollback_artifact_activation(
        self,
        *,
        scope_key: str,
        artifact_id: str | None = None,
        kind: str | None = None,
    ) -> Mapping[str, Any]:
        if artifact_id is None and kind is None:
            raise ValueError("Artifact rollback requires artifact_id or kind")
        with self._transaction() as connection:
            if artifact_id is not None:
                clauses = ["scope_key = ?", "artifact_id = ?", "status = 'ACTIVE'"]
                parameters = [scope_key, artifact_id]
                if kind is not None:
                    clauses.append("kind = ?")
                    parameters.append(kind)
                currents = connection.execute(
                    "SELECT * FROM evolution_artifact_activations WHERE "
                    + " AND ".join(clauses)
                    + " ORDER BY activated_at, activation_id",
                    tuple(parameters),
                ).fetchall()
            else:
                currents = connection.execute(
                    """
                    SELECT * FROM evolution_artifact_activations
                     WHERE scope_key = ? AND kind = ? AND status = 'ACTIVE'
                     ORDER BY artifact_id, activated_at, activation_id
                    """,
                    (scope_key, kind),
                ).fetchall()
                if len(currents) > 1:
                    raise ValueError(
                        "Multiple active Artifacts share this kind; provide artifact_id to roll back one"
                    )
            current = None if not currents else currents[0]
            if current is None or current["replaced_activation_id"] is None:
                raise ValueError("Artifact activation has no prior version to restore")
            previous = connection.execute(
                "SELECT * FROM evolution_artifact_activations WHERE activation_id = ?",
                (current["replaced_activation_id"],),
            ).fetchone()
            if previous is None or str(previous["status"]) not in {"SUPERSEDED", "ROLLED_BACK"}:
                raise ValueError("The prior Artifact activation cannot be restored")
            connection.execute(
                "UPDATE evolution_artifact_activations SET status = 'ROLLED_BACK' WHERE activation_id = ?",
                (current["activation_id"],),
            )
            connection.execute(
                "UPDATE evolution_artifact_activations SET status = 'ACTIVE' WHERE activation_id = ?",
                (previous["activation_id"],),
            )
        return self.get_artifact_activation(str(previous["activation_id"]))

    def set_artifact_update_subscription(
        self, *, scope_key: str, kind: str, artifact_id: str, mode: str
    ) -> Mapping[str, Any]:
        now = utc_now().isoformat()
        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT subscription_id FROM evolution_update_subscriptions
                 WHERE scope_key = ? AND kind = ? AND artifact_id = ?
                """,
                (scope_key, kind, artifact_id),
            ).fetchone()
            if existing is None:
                subscription_id = f"artifact-subscription-{uuid.uuid4()}"
                connection.execute(
                    """
                    INSERT INTO evolution_update_subscriptions(
                        subscription_id, scope_key, kind, artifact_id, mode, status, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, 'ACTIVE', ?, ?)
                    """,
                    (subscription_id, scope_key, kind, artifact_id, mode, now, now),
                )
            else:
                subscription_id = str(existing["subscription_id"])
                connection.execute(
                    """
                    UPDATE evolution_update_subscriptions
                       SET mode = ?, status = 'ACTIVE', updated_at = ?
                     WHERE subscription_id = ?
                    """,
                    (mode, now, subscription_id),
                )
        return self.get_artifact_update_subscription(subscription_id)

    def get_artifact_update_subscription(self, subscription_id: str) -> Mapping[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM evolution_update_subscriptions WHERE subscription_id = ?", (subscription_id,)
            ).fetchone()
        result = self._row(row)
        if result is None:
            raise KeyError(f"Unknown Artifact update subscription: {subscription_id}")
        return result

    def list_artifact_update_subscriptions(self, scope_key: str) -> tuple[Mapping[str, Any], ...]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT subscription_id FROM evolution_update_subscriptions
                 WHERE scope_key = ? AND status = 'ACTIVE' ORDER BY kind, artifact_id
                """,
                (scope_key,),
            ).fetchall()
        return tuple(self.get_artifact_update_subscription(str(row["subscription_id"])) for row in rows)

    def pin_active_artifacts_for_job(self, *, job_id: str, scope_key: str) -> tuple[Mapping[str, Any], ...]:
        pinned_at = utc_now().isoformat()
        with self._transaction() as connection:
            snapshot = connection.execute(
                "SELECT scope_key FROM evolution_job_artifact_snapshots WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if snapshot is not None:
                if str(snapshot["scope_key"]) != scope_key:
                    raise ValueError("A Job Artifact snapshot cannot be read from a different scope")
                return self.list_job_artifact_pins(job_id)
            existing_pins = connection.execute(
                "SELECT scope_key FROM evolution_job_artifact_pins WHERE job_id = ? LIMIT 1", (job_id,)
            ).fetchone()
            if existing_pins is not None:
                if str(existing_pins["scope_key"]) != scope_key:
                    raise ValueError("A Job Artifact snapshot cannot be read from a different scope")
                connection.execute(
                    "INSERT INTO evolution_job_artifact_snapshots(job_id, scope_key, pinned_at) VALUES(?, ?, ?)",
                    (job_id, scope_key, pinned_at),
                )
                return self.list_job_artifact_pins(job_id)
            connection.execute(
                "INSERT INTO evolution_job_artifact_snapshots(job_id, scope_key, pinned_at) VALUES(?, ?, ?)",
                (job_id, scope_key, pinned_at),
            )
            active = self.list_active_artifact_activations(scope_key)
            for activation in active:
                artifact = activation["artifact"]
                connection.execute(
                    """
                    INSERT INTO evolution_job_artifact_pins(
                        job_id, scope_key, kind, artifact_id, version, manifest_digest, pinned_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id, scope_key, activation["kind"], activation["artifact_id"],
                        activation["version"], artifact["manifest_digest"], pinned_at,
                    ),
                )
        return self.list_job_artifact_pins(job_id)

    def list_job_artifact_pins(self, job_id: str) -> tuple[Mapping[str, Any], ...]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM evolution_job_artifact_pins
                 WHERE job_id = ? ORDER BY kind, artifact_id, version
                """,
                (job_id,),
            ).fetchall()
        return tuple(self._row(row) for row in rows)

    def pin_active_artifacts_for_runtime_job(
        self, *, job_id: str, scope_keys: tuple[str, ...]
    ) -> tuple[Mapping[str, Any], ...]:
        """Freeze company and employee-scoped Artifacts for one Job.

        The original single-scope pin table remains the compatibility/audit
        projection.  Runtime projection needs multiple scopes because a
        company default may be refined by a particular persistent employee.
        Neither path performs network I/O or changes local activation.
        """

        scopes = tuple(dict.fromkeys(scope_keys))
        if not scopes or any(not scope.strip() for scope in scopes):
            raise ValueError("Runtime Artifact pinning requires one or more non-empty scopes")
        pinned_at = utc_now().isoformat()
        with self._transaction() as connection:
            snapshot = connection.execute(
                """
                SELECT scope_keys_json FROM evolution_job_runtime_artifact_snapshots
                 WHERE job_id = ?
                """,
                (job_id,),
            ).fetchone()
            if snapshot is not None:
                existing_scopes = tuple(json.loads(str(snapshot["scope_keys_json"])))
                if existing_scopes != scopes:
                    raise ValueError("A Job Artifact runtime snapshot cannot be read from different scopes")
                return self.list_runtime_job_artifact_pins(job_id)
            connection.execute(
                """
                INSERT INTO evolution_job_runtime_artifact_snapshots(
                    job_id, scope_keys_json, pinned_at
                ) VALUES(?, ?, ?)
                """,
                (job_id, canonical_json(scopes), pinned_at),
            )
            rows = connection.execute(
                """
                SELECT * FROM evolution_job_runtime_artifact_pins
                 WHERE job_id = ? ORDER BY scope_key, kind, artifact_id, version
                """,
                (job_id,),
            ).fetchall()
            if rows:
                existing_scopes = tuple(sorted({str(row["scope_key"]) for row in rows}))
                if existing_scopes != tuple(sorted(scopes)):
                    raise ValueError("A Job Artifact runtime snapshot cannot be read from different scopes")
                return tuple(self._row(row) for row in rows)
            for scope_key in scopes:
                for activation in self.list_active_artifact_activations(scope_key):
                    artifact = activation["artifact"]
                    connection.execute(
                        """
                        INSERT INTO evolution_job_runtime_artifact_pins(
                            job_id, scope_key, kind, artifact_id, version,
                            manifest_digest, pinned_at
                        ) VALUES(?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            job_id,
                            scope_key,
                            activation["kind"],
                            activation["artifact_id"],
                            activation["version"],
                            artifact["manifest_digest"],
                            pinned_at,
                        ),
                    )
        return self.list_runtime_job_artifact_pins(job_id)

    def list_runtime_job_artifact_pins(self, job_id: str) -> tuple[Mapping[str, Any], ...]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM evolution_job_runtime_artifact_pins
                 WHERE job_id = ? ORDER BY scope_key, kind, artifact_id, version
                """,
                (job_id,),
            ).fetchall()
        return tuple(self._row(row) for row in rows)
