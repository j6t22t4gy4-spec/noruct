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

from .artifact_origin import ArtifactOriginKind, network_imported_origin


class EvolutionArtifactRegistryMixin:
    """Own staged artifact registry validation, review, and import lifecycle."""
    def stage_verified_artifact_registry_bundle(
        self,
        bundle: Mapping[str, Any],
        *,
        source_label: str,
        signature: bytes,
        allowed_signers_path: Path,
        principal: str,
        command: Path,
    ) -> Mapping[str, Any]:
        if not source_label or len(source_label) > 160:
            raise ValueError("Artifact staging requires a non-empty source label")
        from .artifact_bundle import artifact_registry_bundle_signing_payload
        from .signing import verify_openssh_signature_bytes

        receipt = verify_openssh_signature_bytes(
            artifact_registry_bundle_signing_payload(bundle),
            signature=signature,
            allowed_signers_path=allowed_signers_path,
            principal=principal,
            command=command,
        )
        # A receipt is evidence for this exact canonical payload only.  Keep
        # this check in the store boundary too, so a caller cannot manufacture
        # a plausible-looking receipt and bypass the signature verifier.
        expected_payload_digest = content_digest(artifact_registry_bundle_signing_payload(bundle).decode("utf-8"))
        if receipt.get("payload_digest") != expected_payload_digest:
            raise ValueError("Artifact registry signature receipt does not bind this bundle")
        snapshot_id = f"artifact-registry-snapshot-{uuid.uuid4()}"; verified_at = utc_now().isoformat()
        with self._transaction() as connection:
            trusted = connection.execute("SELECT 1 FROM registry_signer_trust_roots WHERE source_label = ? AND signer_principal = ? AND allowed_signers_digest = ? AND status = 'ACTIVE'", (source_label, receipt["principal"], receipt["allowed_signers_digest"])).fetchone()
            if trusted is None:
                raise ValueError("Artifact registry signer is not an active trusted root for this source")
            duplicate = connection.execute("SELECT snapshot_id FROM trusted_artifact_registry_snapshots WHERE source_label = ? AND bundle_digest = ?", (source_label, bundle["bundle_digest"])).fetchone()
            if duplicate is not None:
                return self.get_staged_artifact_registry_snapshot(str(duplicate["snapshot_id"]))
            connection.execute("INSERT INTO trusted_artifact_registry_snapshots VALUES(?, ?, ?, ?, ?, ?, ?, 'STAGED_TRUSTED_NOT_IMPORTABLE', ?)", (snapshot_id, source_label, bundle["registry_id"], bundle["bundle_digest"], receipt["principal"], receipt["signature_digest"], receipt["allowed_signers_digest"], verified_at))
            for item in bundle["artifacts"]:
                connection.execute("INSERT INTO staged_artifact_registry_entries VALUES(?, ?, ?, ?, ?, ?, ?, 'STAGED_TRUSTED_NOT_IMPORTABLE')", (snapshot_id, item["artifact_id"], item["version"], item["kind"], item["release_channel"], canonical_json(item["manifest"]), item["manifest_digest"]))
        return self.get_staged_artifact_registry_snapshot(snapshot_id)

    def get_staged_artifact_registry_snapshot(self, snapshot_id: str) -> Mapping[str, Any]:
        with self._lock:
            snapshot = self._conn.execute("SELECT * FROM trusted_artifact_registry_snapshots WHERE snapshot_id = ?", (snapshot_id,)).fetchone()
            entries = self._conn.execute("SELECT * FROM staged_artifact_registry_entries WHERE snapshot_id = ? ORDER BY artifact_id, version", (snapshot_id,)).fetchall()
            review = self._conn.execute("SELECT * FROM artifact_registry_snapshot_reviews WHERE snapshot_id = ?", (snapshot_id,)).fetchone()
        result = self._row(snapshot)
        if result is None: raise KeyError(f"Unknown staged Artifact registry snapshot: {snapshot_id}")
        result["artifacts"] = tuple({**{key: value for key, value in dict(row).items() if key != "manifest_json"}, "manifest": json.loads(str(row["manifest_json"]))} for row in entries)
        result["review"] = self._row(review); result["runtime_effect"] = "NONE"
        from .score_contract import evolution_content_digest
        from .service import validate_evolution_artifact

        bundle_entries: list[Mapping[str, Any]] = []
        for item in result["artifacts"]:
            manifest = validate_evolution_artifact(item["manifest"])
            manifest_digest = evolution_content_digest(manifest)
            if (
                manifest_digest != item["manifest_digest"]
                or item["artifact_id"] != manifest["artifact_id"]
                or item["version"] != manifest["version"]
                or item["kind"] != manifest["kind"]
                or item["release_channel"] != manifest["release_channel"]
            ):
                raise ValueError("Staged Artifact registry entry failed immutable digest validation")
            bundle_entries.append(
                {
                    "artifact_id": item["artifact_id"],
                    "version": item["version"],
                    "kind": item["kind"],
                    "release_channel": item["release_channel"],
                    "manifest": manifest,
                    "manifest_digest": manifest_digest,
                }
            )
        unsigned = {
            "schema": "noruct.public-evolution-artifact-registry-bundle.v1",
            "registry_id": result["registry_id"],
            "artifacts": tuple(bundle_entries),
        }
        if evolution_content_digest(unsigned) != result["bundle_digest"]:
            raise ValueError("Staged Artifact registry snapshot failed bundle closure validation")
        return result

    def preview_staged_artifact_registry_compatibility(self, snapshot_id: str) -> Mapping[str, Any]:
        snapshot = self.get_staged_artifact_registry_snapshot(snapshot_id)
        if snapshot["status"] != "STAGED_TRUSTED_NOT_IMPORTABLE":
            return {"snapshot": snapshot, "decision": "NOT_REVIEWABLE", "runtime_effect": "NONE"}
        with self._lock:
            active_root = self._conn.execute(
                """SELECT 1 FROM registry_signer_trust_roots
                    WHERE source_label = ? AND signer_principal = ?
                      AND allowed_signers_digest = ? AND status = 'ACTIVE'""",
                (
                    snapshot["source_label"],
                    snapshot["signer_principal"],
                    snapshot["allowed_signers_digest"],
                ),
            ).fetchone()
        if active_root is None:
            return {"snapshot": snapshot, "decision": "BLOCKED_SIGNER_TRUST_INACTIVE", "runtime_effect": "NONE"}
        identities = tuple((item["artifact_id"], item["version"], item["manifest_digest"]) for item in snapshot["artifacts"])
        return {"snapshot": snapshot, "decision": "REQUIRES_OPERATOR_REVIEW", "compatibility_digest": content_digest({"bundle_digest": snapshot["bundle_digest"], "artifacts": identities}), "runtime_effect": "NONE"}

    def review_staged_artifact_registry_snapshot(self, snapshot_id: str, *, operator_id: str, decision: str, reason: str) -> Mapping[str, Any]:
        if decision not in {"APPROVE", "REJECT"} or not operator_id or not reason: raise ValueError("Artifact registry review requires operator id, reason, and APPROVE or REJECT")
        preview = self.preview_staged_artifact_registry_compatibility(snapshot_id)
        if preview["decision"] != "REQUIRES_OPERATOR_REVIEW": raise ValueError("Only unreviewed trusted Artifact snapshots may be reviewed")
        status = "REVIEW_APPROVED_NOT_IMPORTED" if decision == "APPROVE" else "REVIEW_REJECTED"
        with self._transaction() as connection:
            updated = connection.execute(
                """UPDATE trusted_artifact_registry_snapshots SET status = ?
                    WHERE snapshot_id = ? AND status = 'STAGED_TRUSTED_NOT_IMPORTABLE'
                      AND EXISTS (
                        SELECT 1 FROM registry_signer_trust_roots root
                         WHERE root.source_label = trusted_artifact_registry_snapshots.source_label
                           AND root.signer_principal = trusted_artifact_registry_snapshots.signer_principal
                           AND root.allowed_signers_digest = trusted_artifact_registry_snapshots.allowed_signers_digest
                           AND root.status = 'ACTIVE'
                      )""",
                (status, snapshot_id),
            ).rowcount
            if updated != 1:
                raise ValueError("Artifact snapshot signer trust changed before review was committed")
            connection.execute("INSERT INTO artifact_registry_snapshot_reviews VALUES(?, ?, ?, ?, ?, ?, ?)", (f"artifact-registry-review-{uuid.uuid4()}", snapshot_id, operator_id, decision, reason, preview["compatibility_digest"], utc_now().isoformat()))
        return self.get_staged_artifact_registry_snapshot(snapshot_id)

    def import_reviewed_staged_artifact(self, snapshot_id: str, artifact_id: str, version: str) -> Mapping[str, Any]:
        snapshot = self.get_staged_artifact_registry_snapshot(snapshot_id)
        if snapshot["status"] != "REVIEW_APPROVED_NOT_IMPORTED": raise ValueError("Artifact snapshot requires approved operator review before catalog import")
        entry = next((item for item in snapshot["artifacts"] if item["artifact_id"] == artifact_id and item["version"] == version), None)
        if entry is None: raise KeyError("Artifact is not present in the staged snapshot")
        from .score_contract import evolution_content_digest
        from .service import validate_evolution_artifact

        manifest = validate_evolution_artifact(entry["manifest"])
        digest = evolution_content_digest(manifest)
        if digest != entry["manifest_digest"]:
            raise ValueError("Reviewed Artifact entry digest changed before import")
        available_at = utc_now().isoformat()
        origin_metadata = network_imported_origin(
            snapshot_id=snapshot_id,
            source_label=str(snapshot["source_label"]),
            registry_id=str(snapshot["registry_id"]),
            bundle_digest=str(snapshot["bundle_digest"]),
        )
        with self._transaction() as connection:
            authorized = connection.execute(
                """SELECT 1 FROM trusted_artifact_registry_snapshots snapshot
                    JOIN registry_signer_trust_roots root
                      ON root.source_label = snapshot.source_label
                     AND root.signer_principal = snapshot.signer_principal
                     AND root.allowed_signers_digest = snapshot.allowed_signers_digest
                   WHERE snapshot.snapshot_id = ?
                     AND snapshot.status = 'REVIEW_APPROVED_NOT_IMPORTED'
                     AND root.status = 'ACTIVE'""",
                (snapshot_id,),
            ).fetchone()
            if authorized is None:
                raise ValueError("Artifact snapshot is no longer authorized for import")
            existing = connection.execute(
                "SELECT manifest_digest, origin_kind, origin_metadata_json "
                "FROM evolution_artifact_versions WHERE artifact_id = ? AND version = ?",
                (artifact_id, version),
            ).fetchone()
            if existing is not None and str(existing["manifest_digest"]) != digest:
                raise ValueError("An Artifact id/version is immutable; register a new version for changed content")
            if existing is not None and (
                str(existing["origin_kind"]) != ArtifactOriginKind.NETWORK_IMPORTED.value
                or json.loads(str(existing["origin_metadata_json"])) != origin_metadata
            ):
                raise ValueError(
                    "An Artifact id/version origin is immutable and cannot be reclassified"
                )
            if existing is None:
                passport = manifest.get("passport")
                connection.execute(
                    """INSERT INTO evolution_artifact_versions(
                        artifact_id, version, kind, release_channel, manifest_json,
                        manifest_digest, passport_json, origin_kind,
                        origin_metadata_json, available_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        artifact_id,
                        version,
                        manifest["kind"],
                        manifest["release_channel"],
                        canonical_json(manifest),
                        digest,
                        None if passport is None else canonical_json(passport),
                        ArtifactOriginKind.NETWORK_IMPORTED.value,
                        canonical_json(origin_metadata),
                        available_at,
                    ),
                )
                event = {
                    "artifact_id": artifact_id,
                    "version": version,
                    "kind": manifest["kind"],
                    "manifest_digest": digest,
                    "origin_kind": ArtifactOriginKind.NETWORK_IMPORTED.value,
                    "origin_metadata": origin_metadata,
                }
                connection.execute(
                    """INSERT INTO evolution_evidence_events(
                        event_type, subject_id, payload_json, payload_digest, recorded_at
                    ) VALUES('EVOLUTION_ARTIFACT_AVAILABLE', ?, ?, ?, ?)""",
                    (
                        f"{artifact_id}@{version}",
                        canonical_json(event),
                        content_digest(event),
                        available_at,
                    ),
                )
            updated = connection.execute(
                """UPDATE staged_artifact_registry_entries
                      SET status = 'IMPORTED_LOCAL_CATALOG'
                    WHERE snapshot_id = ? AND artifact_id = ? AND version = ?
                      AND status != 'REVOKED_SIGNER_TRUST'""",
                (snapshot_id, artifact_id, version),
            ).rowcount
            if updated != 1:
                raise ValueError("Artifact entry is no longer importable")
        return self.get_artifact_version(artifact_id, version)

    def get_artifact_registry_import_provenance(
        self, artifact_id: str, version: str
    ) -> Mapping[str, Any]:
        """Return the reviewed remote snapshot that supplied one local version.

        Registry import provenance remains authoritative even after the
        manifest has entered the local catalog.  A later local tracker must
        not mistake that immutable imported version for a locally derived
        candidate and activate it automatically.
        """

        with self._lock:
            row = self._conn.execute(
                """
                SELECT entry.snapshot_id, snapshot.source_label,
                       snapshot.registry_id, snapshot.bundle_digest,
                       entry.artifact_id, entry.version, entry.manifest_digest
                  FROM staged_artifact_registry_entries entry
                  JOIN trusted_artifact_registry_snapshots snapshot
                    ON snapshot.snapshot_id = entry.snapshot_id
                 WHERE entry.artifact_id = ? AND entry.version = ?
                   AND entry.status = 'IMPORTED_LOCAL_CATALOG'
                 ORDER BY snapshot.verified_at, entry.snapshot_id
                 LIMIT 1
                """,
                (artifact_id, version),
            ).fetchone()
        result = self._row(row)
        if result is None:
            raise KeyError(
                f"Artifact has no reviewed registry import provenance: {artifact_id}@{version}"
            )
        return result

    def list_staged_artifact_registry_snapshots(self) -> tuple[Mapping[str, Any], ...]:
        with self._lock:
            rows = self._conn.execute("SELECT snapshot_id FROM trusted_artifact_registry_snapshots ORDER BY verified_at, snapshot_id").fetchall()
        return tuple(self.get_staged_artifact_registry_snapshot(str(row["snapshot_id"])) for row in rows)
