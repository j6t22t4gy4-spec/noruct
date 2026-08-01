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


class EvolutionReleaseRegistryMixin:
    """Own release-candidate, registry, trust-root, and tenant-adoption lifecycle."""
    def create_release_candidate(
        self,
        *,
        blueprint: Mapping[str, Any],
        delta: Mapping[str, Any],
        holdout: Mapping[str, Any],
        capsule_ids: tuple[str, ...],
    ) -> Mapping[str, Any]:
        if not capsule_ids:
            raise ValueError("At least one active Learning Capsule receipt is required")
        if holdout.get("decision") != "ELIGIBLE_FOR_MANUAL_REVIEW":
            raise ValueError("Only a manual-review eligible holdout may create a release candidate")
        candidate_id = f"release-candidate-{uuid.uuid4()}"
        created_at = utc_now().isoformat()
        with self._transaction() as connection:
            capsules = []
            for capsule_id in capsule_ids:
                row = connection.execute(
                    "SELECT * FROM learning_capsules WHERE capsule_id = ?", (capsule_id,)
                ).fetchone()
                if row is None or row["status"] != "QUEUED_LOCAL_ONLY":
                    raise ValueError(f"Release candidate requires an active queued Capsule: {capsule_id}")
                capsules.append(row)
            connection.execute(
                """
                INSERT INTO blueprint_release_candidates(
                    candidate_id, blueprint_id, base_version, candidate_version,
                    delta_json, delta_digest, holdout_json, holdout_digest, status,
                    revocation_reason, created_at, revoked_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'PENDING_REVIEW', NULL, ?, NULL)
                """,
                (
                    candidate_id, blueprint["blueprint_id"], blueprint["version"], delta["candidate_version"],
                    canonical_json(delta), content_digest(delta), canonical_json(holdout), content_digest(holdout), created_at,
                ),
            )
            for capsule in capsules:
                connection.execute(
                    "INSERT INTO release_candidate_capsules(candidate_id, capsule_id, capsule_digest) VALUES(?, ?, ?)",
                    (candidate_id, capsule["capsule_id"], capsule["payload_digest"]),
                )
            payload = {"candidate_id": candidate_id, "capsule_count": len(capsules), "holdout_digest": content_digest(holdout)}
            connection.execute(
                """
                INSERT INTO evolution_evidence_events(event_type, subject_id, payload_json, payload_digest, recorded_at)
                VALUES('RELEASE_CANDIDATE_CREATED', ?, ?, ?, ?)
                """,
                (candidate_id, canonical_json(payload), content_digest(payload), created_at),
            )
        return self.get_release_candidate(candidate_id)

    def get_release_candidate(self, candidate_id: str) -> Mapping[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM blueprint_release_candidates WHERE candidate_id = ?", (candidate_id,)
            ).fetchone()
            capsules = self._conn.execute(
                "SELECT capsule_id, capsule_digest FROM release_candidate_capsules WHERE candidate_id = ? ORDER BY capsule_id",
                (candidate_id,),
            ).fetchall()
            reviews = self._conn.execute(
                "SELECT * FROM release_candidate_reviews WHERE candidate_id = ? ORDER BY recorded_at, review_id",
                (candidate_id,),
            ).fetchall()
        result = self._row(row)
        if result is None:
            raise KeyError(f"Unknown Blueprint release candidate: {candidate_id}")
        result["delta"] = json.loads(result.pop("delta_json"))
        result["holdout"] = json.loads(result.pop("holdout_json"))
        result["capsules"] = tuple(dict(item) for item in capsules)
        result["reviews"] = tuple(dict(item) for item in reviews)
        return result

    def record_verified_signature(self, candidate_id: str, receipt: Mapping[str, str]) -> Mapping[str, Any]:
        recorded_at = utc_now().isoformat()
        with self._transaction() as connection:
            row = connection.execute("SELECT status FROM blueprint_release_candidates WHERE candidate_id = ?", (candidate_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown Blueprint release candidate: {candidate_id}")
            if row["status"] != "APPROVED_PENDING_SIGNATURE":
                raise ValueError("Only APPROVED_PENDING_SIGNATURE candidate may record a signature")
            signature_id = f"release-signature-{uuid.uuid4()}"
            connection.execute(
                "INSERT INTO release_candidate_signatures VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (signature_id, candidate_id, receipt["algorithm"], receipt["principal"], receipt["payload_digest"], receipt["signature_digest"], receipt["allowed_signers_digest"], recorded_at),
            )
            connection.execute("UPDATE blueprint_release_candidates SET status = 'SIGNATURE_VERIFIED' WHERE candidate_id = ?", (candidate_id,))
            event = {"candidate_id": candidate_id, "signature_id": signature_id, "principal": receipt["principal"], "signature_digest": receipt["signature_digest"]}
            connection.execute("INSERT INTO evolution_evidence_events(event_type, subject_id, payload_json, payload_digest, recorded_at) VALUES('RELEASE_CANDIDATE_SIGNATURE_VERIFIED', ?, ?, ?, ?)", (candidate_id, canonical_json(event), content_digest(event), recorded_at))
        return self.get_release_candidate(candidate_id)

    def publish_release_candidate(self, candidate_id: str) -> Mapping[str, Any]:
        published_at = utc_now().isoformat()
        with self._transaction() as connection:
            candidate = connection.execute("SELECT * FROM blueprint_release_candidates WHERE candidate_id = ?", (candidate_id,)).fetchone()
            if candidate is None:
                raise KeyError(f"Unknown Blueprint release candidate: {candidate_id}")
            if candidate["status"] != "SIGNATURE_VERIFIED":
                raise ValueError("Only SIGNATURE_VERIFIED candidates may publish a local registry release")
            base = connection.execute("SELECT manifest_json FROM employee_blueprints WHERE blueprint_id = ? AND version = ?", (candidate["blueprint_id"], candidate["base_version"])).fetchone()
            if base is None:
                raise ValueError("Release candidate base Blueprint is unavailable in the local catalog")
            manifest = json.loads(base["manifest_json"])
            delta = json.loads(candidate["delta_json"])
            manifest["version"] = candidate["candidate_version"]
            manifest["capability_aliases"] = ({"alias": delta["alias"], "target_capability": delta["target_capability"]},)
            digest = content_digest(manifest)
            release_id = f"registry-release-{uuid.uuid4()}"
            connection.execute(
                "INSERT INTO local_blueprint_registry_releases VALUES(?, ?, ?, ?, ?, ?, 'PUBLISHED_LOCAL', ?, NULL)",
                (release_id, candidate_id, candidate["blueprint_id"], candidate["candidate_version"], canonical_json(manifest), digest, published_at),
            )
            connection.execute("UPDATE blueprint_release_candidates SET status = 'PUBLISHED_LOCAL' WHERE candidate_id = ?", (candidate_id,))
            event = {"release_id": release_id, "candidate_id": candidate_id, "manifest_digest": digest}
            connection.execute("INSERT INTO evolution_evidence_events(event_type, subject_id, payload_json, payload_digest, recorded_at) VALUES('LOCAL_REGISTRY_RELEASE_PUBLISHED', ?, ?, ?, ?)", (release_id, canonical_json(event), content_digest(event), published_at))
        return self.get_registry_release(release_id)

    def get_registry_release(self, release_id: str) -> Mapping[str, Any]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM local_blueprint_registry_releases WHERE release_id = ?", (release_id,)).fetchone()
        result = self._row(row)
        if result is None:
            raise KeyError(f"Unknown local registry release: {release_id}")
        return result

    def list_registry_releases(self) -> tuple[Mapping[str, Any], ...]:
        with self._lock:
            rows = self._conn.execute("SELECT release_id FROM local_blueprint_registry_releases ORDER BY published_at, release_id").fetchall()
        return tuple(self.get_registry_release(str(row["release_id"])) for row in rows)

    def stage_verified_registry_bundle(
        self,
        bundle: Mapping[str, Any],
        *,
        source_label: str,
        signature: bytes,
        allowed_signers_path: Path,
        principal: str,
        command: Path,
    ) -> Mapping[str, Any]:
        """Store a verified remote catalog separately from local release authority.

        Staging is deliberately not import, publication, selection, adoption, or
        employee execution.  A future operator trust policy must make those
        choices explicitly and can never infer them from a successful fetch.
        """
        if not source_label or len(source_label) > 160:
            raise ValueError("Registry source label must be a non-empty value up to 160 characters")
        from .signing import verify_openssh_signature_bytes
        from .registry_bundle import registry_bundle_signing_payload

        receipt = verify_openssh_signature_bytes(
            registry_bundle_signing_payload(bundle),
            signature=signature,
            allowed_signers_path=allowed_signers_path,
            principal=principal,
            command=command,
        )
        # Keep the state boundary cryptographically scoped even when callers
        # bypass the CLI/service verifier.  A trusted signer receipt for one
        # bundle must never authorize staging another bundle.
        expected_payload_digest = content_digest(registry_bundle_signing_payload(bundle).decode("utf-8"))
        if receipt.get("payload_digest") != expected_payload_digest:
            raise ValueError("Registry signature receipt does not bind this bundle")
        verified_at = utc_now().isoformat()
        snapshot_id = f"registry-snapshot-{uuid.uuid4()}"
        releases = tuple(bundle["releases"])
        with self._transaction() as connection:
            trusted = connection.execute(
                """
                SELECT trust_root_id FROM registry_signer_trust_roots
                 WHERE source_label = ? AND signer_principal = ?
                   AND allowed_signers_digest = ? AND status = 'ACTIVE'
                """,
                (source_label, receipt["principal"], receipt["allowed_signers_digest"]),
            ).fetchone()
            if trusted is None:
                raise ValueError("Registry signer is not an active trusted root for this source")
            duplicate = connection.execute(
                "SELECT snapshot_id FROM trusted_registry_snapshots WHERE source_label = ? AND bundle_digest = ?",
                (source_label, bundle["bundle_digest"]),
            ).fetchone()
            if duplicate is not None:
                return self.get_staged_registry_snapshot(str(duplicate["snapshot_id"]))
            connection.execute(
                """
                INSERT INTO trusted_registry_snapshots(
                    snapshot_id, source_label, registry_id, bundle_digest, signer_principal,
                    signature_digest, allowed_signers_digest, status, verified_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, 'STAGED_TRUSTED_NOT_ADOPTABLE', ?)
                """,
                (
                    snapshot_id, source_label, bundle["registry_id"], bundle["bundle_digest"],
                    receipt["principal"], receipt["signature_digest"], receipt["allowed_signers_digest"], verified_at,
                ),
            )
            for release in releases:
                connection.execute(
                    """
                    INSERT INTO staged_registry_releases(
                        snapshot_id, remote_release_id, blueprint_id, version, manifest_json,
                        manifest_digest, published_at, status
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, 'STAGED_TRUSTED_NOT_ADOPTABLE')
                    """,
                    (
                        snapshot_id, release["release_id"], release["blueprint_id"], release["version"],
                        canonical_json(release["manifest"]), release["manifest_digest"], release["published_at"],
                    ),
                )
            event = {
                "snapshot_id": snapshot_id,
                "source_label": source_label,
                "bundle_digest": bundle["bundle_digest"],
                "release_count": len(releases),
                "signer_principal": receipt["principal"],
            }
            connection.execute(
                """
                INSERT INTO evolution_evidence_events(event_type, subject_id, payload_json, payload_digest, recorded_at)
                VALUES('REMOTE_REGISTRY_SNAPSHOT_STAGED', ?, ?, ?, ?)
                """,
                (snapshot_id, canonical_json(event), content_digest(event), verified_at),
            )
        return self.get_staged_registry_snapshot(snapshot_id)

    def register_registry_signer_trust_root(
        self, *, source_label: str, signer_principal: str, allowed_signers_digest: str,
        operator_id: str,
    ) -> Mapping[str, Any]:
        if not source_label or len(source_label) > 160 or not signer_principal or not operator_id:
            raise ValueError("Registry source label, signer principal, and operator id are required")
        if len(allowed_signers_digest) != 64:
            raise ValueError("Registry signer trust root requires an allowed-signers SHA-256 digest")
        trust_root_id = f"registry-trust-{uuid.uuid4()}"
        registered_at = utc_now().isoformat()
        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT trust_root_id FROM registry_signer_trust_roots
                 WHERE source_label = ? AND signer_principal = ? AND status = 'ACTIVE'
                """, (source_label, signer_principal),
            ).fetchone()
            if existing is not None:
                raise ValueError("An active trust root already exists for this source and signer; retire it before rotation")
            connection.execute(
                """
                INSERT INTO registry_signer_trust_roots VALUES(?, ?, ?, ?, ?, 'ACTIVE', ?, NULL, NULL, NULL)
                """, (trust_root_id, source_label, signer_principal, allowed_signers_digest, operator_id, registered_at),
            )
        return self.get_registry_signer_trust_root(trust_root_id)

    def get_registry_signer_trust_root(self, trust_root_id: str) -> Mapping[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM registry_signer_trust_roots WHERE trust_root_id = ?", (trust_root_id,)
            ).fetchone()
        result = self._row(row)
        if result is None:
            raise KeyError(f"Unknown registry signer trust root: {trust_root_id}")
        return result

    def list_registry_signer_trust_roots(self) -> tuple[Mapping[str, Any], ...]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT trust_root_id FROM registry_signer_trust_roots ORDER BY registered_at, trust_root_id"
            ).fetchall()
        return tuple(self.get_registry_signer_trust_root(str(row["trust_root_id"])) for row in rows)

    def retire_registry_signer_trust_root(self, trust_root_id: str, *, operator_id: str) -> Mapping[str, Any]:
        if not operator_id:
            raise ValueError("Operator id is required to retire a registry signer trust root")
        retired_at = utc_now().isoformat()
        with self._transaction() as connection:
            updated = connection.execute(
                """UPDATE registry_signer_trust_roots SET status = 'RETIRED', retired_at = ?
                     WHERE trust_root_id = ? AND status = 'ACTIVE'""",
                (retired_at, trust_root_id),
            ).rowcount
            if not updated:
                raise ValueError("Only an active registry signer trust root may be retired")
            event = {
                "trust_root_id": trust_root_id,
                "operator_id": operator_id,
                "action": "RETIRED",
            }
            connection.execute(
                """
                INSERT INTO evolution_evidence_events(event_type, subject_id, payload_json, payload_digest, recorded_at)
                VALUES('REGISTRY_SIGNER_TRUST_ROOT_RETIRED', ?, ?, ?, ?)
                """,
                (trust_root_id, canonical_json(event), content_digest(event), retired_at),
            )
        return self.get_registry_signer_trust_root(trust_root_id)

    def revoke_registry_signer_trust_root(
        self, trust_root_id: str, *, operator_id: str, reason: str
    ) -> Mapping[str, Any]:
        if not operator_id or not reason:
            raise ValueError("Operator id and revocation reason are required")
        revoked_at = utc_now().isoformat()
        with self._transaction() as connection:
            root = connection.execute(
                "SELECT * FROM registry_signer_trust_roots WHERE trust_root_id = ?", (trust_root_id,)
            ).fetchone()
            if root is None or root["status"] not in {"ACTIVE", "RETIRED"}:
                raise ValueError("Only an active or retired registry signer trust root may be revoked")
            connection.execute(
                """UPDATE registry_signer_trust_roots SET status = 'REVOKED', revoked_at = ?, revocation_reason = ?
                     WHERE trust_root_id = ?""", (revoked_at, reason, trust_root_id),
            )
            connection.execute(
                """UPDATE trusted_registry_snapshots SET status = 'REVOKED_SIGNER_TRUST'
                     WHERE source_label = ? AND signer_principal = ? AND allowed_signers_digest = ?
                       AND status IN ('STAGED_TRUSTED_NOT_ADOPTABLE', 'REVIEW_APPROVED_NOT_ADOPTABLE')""",
                (root["source_label"], root["signer_principal"], root["allowed_signers_digest"]),
            )
            connection.execute(
                """UPDATE staged_registry_releases SET status = 'REVOKED_SIGNER_TRUST'
                     WHERE snapshot_id IN (
                        SELECT snapshot_id FROM trusted_registry_snapshots
                         WHERE source_label = ? AND signer_principal = ? AND allowed_signers_digest = ?
                           AND status = 'REVOKED_SIGNER_TRUST'
                     )""",
                (root["source_label"], root["signer_principal"], root["allowed_signers_digest"]),
            )
            connection.execute(
                """UPDATE trusted_artifact_registry_snapshots
                      SET status = 'REVOKED_SIGNER_TRUST'
                    WHERE source_label = ? AND signer_principal = ?
                      AND allowed_signers_digest = ?
                      AND status IN (
                        'STAGED_TRUSTED_NOT_IMPORTABLE',
                        'REVIEW_APPROVED_NOT_IMPORTED'
                      )""",
                (root["source_label"], root["signer_principal"], root["allowed_signers_digest"]),
            )
            connection.execute(
                """UPDATE staged_artifact_registry_entries
                      SET status = 'REVOKED_SIGNER_TRUST'
                    WHERE snapshot_id IN (
                      SELECT snapshot_id
                        FROM trusted_artifact_registry_snapshots
                       WHERE source_label = ? AND signer_principal = ?
                         AND allowed_signers_digest = ?
                         AND status = 'REVOKED_SIGNER_TRUST'
                    ) AND status != 'IMPORTED_LOCAL_CATALOG'""",
                (root["source_label"], root["signer_principal"], root["allowed_signers_digest"]),
            )
            connection.execute(
                """UPDATE remote_tenant_adoption_candidates SET status = 'REVOKED_SOURCE_TRUST', resolved_at = ?
                     WHERE snapshot_id IN (SELECT snapshot_id FROM trusted_registry_snapshots WHERE status = 'REVOKED_SIGNER_TRUST')
                       AND status IN ('PENDING_TENANT_CONFIRMATION', 'TENANT_CANDIDATE_APPROVED_NOT_APPLIED')""",
                (revoked_at,),
            )
            event = {
                "trust_root_id": trust_root_id,
                "operator_id": operator_id,
                "action": "REVOKED",
                "reason": reason,
            }
            connection.execute(
                """
                INSERT INTO evolution_evidence_events(event_type, subject_id, payload_json, payload_digest, recorded_at)
                VALUES('REGISTRY_SIGNER_TRUST_ROOT_REVOKED', ?, ?, ?, ?)
                """,
                (trust_root_id, canonical_json(event), content_digest(event), revoked_at),
            )
        return self.get_registry_signer_trust_root(trust_root_id)

    def get_staged_registry_snapshot(self, snapshot_id: str) -> Mapping[str, Any]:
        with self._lock:
            snapshot = self._conn.execute(
                "SELECT * FROM trusted_registry_snapshots WHERE snapshot_id = ?", (snapshot_id,)
            ).fetchone()
            releases = self._conn.execute(
                "SELECT * FROM staged_registry_releases WHERE snapshot_id = ? ORDER BY remote_release_id",
                (snapshot_id,),
            ).fetchall()
            review = self._conn.execute("SELECT * FROM staged_registry_snapshot_reviews WHERE snapshot_id = ?", (snapshot_id,)).fetchone()
        result = self._row(snapshot)
        if result is None:
            raise KeyError(f"Unknown staged registry snapshot: {snapshot_id}")
        result["releases"] = tuple(
            {
                **{key: value for key, value in dict(release).items() if key != "manifest_json"},
                "manifest": json.loads(str(release["manifest_json"])),
            }
            for release in releases
        )
        result["runtime_effect"] = "NONE"
        result["tenant_adoption_effect"] = "NONE"
        result["review"] = self._row(review)
        return result

    def preview_staged_registry_compatibility(self, snapshot_id: str) -> Mapping[str, Any]:
        snapshot = self.get_staged_registry_snapshot(snapshot_id)
        if snapshot["status"] != "STAGED_TRUSTED_NOT_ADOPTABLE":
            return {"snapshot": snapshot, "decision": "NOT_REVIEWABLE", "runtime_effect": "NONE"}
        with self._lock:
            trust_root = self._conn.execute(
                """
                SELECT 1 FROM registry_signer_trust_roots
                 WHERE source_label = ? AND signer_principal = ?
                   AND allowed_signers_digest = ? AND status = 'ACTIVE'
                """,
                (
                    snapshot["source_label"],
                    snapshot["signer_principal"],
                    snapshot["allowed_signers_digest"],
                ),
            ).fetchone()
        if trust_root is None:
            return {
                "snapshot": snapshot,
                "decision": "BLOCKED_SIGNER_TRUST_INACTIVE",
                "runtime_effect": "NONE",
            }
        identities = tuple((item["blueprint_id"], item["version"], item["manifest_digest"]) for item in snapshot["releases"])
        return {"snapshot": snapshot, "decision": "REQUIRES_OPERATOR_REVIEW", "compatibility_digest": content_digest({"bundle_digest": snapshot["bundle_digest"], "releases": identities}), "runtime_effect": "NONE"}

    def review_staged_registry_snapshot(self, snapshot_id: str, *, operator_id: str, decision: str, reason: str) -> Mapping[str, Any]:
        if decision not in {"APPROVE", "REJECT"} or not operator_id or not reason:
            raise ValueError("Staged registry review requires operator id, reason, and APPROVE or REJECT")
        preview = self.preview_staged_registry_compatibility(snapshot_id)
        if preview["decision"] != "REQUIRES_OPERATOR_REVIEW":
            raise ValueError("Only unreviewed staged trusted snapshots may be reviewed")
        status = "REVIEW_APPROVED_NOT_ADOPTABLE" if decision == "APPROVE" else "REVIEW_REJECTED"
        with self._transaction() as connection:
            updated = connection.execute(
                """
                UPDATE trusted_registry_snapshots
                   SET status = ?
                 WHERE snapshot_id = ?
                   AND status = 'STAGED_TRUSTED_NOT_ADOPTABLE'
                   AND EXISTS (
                     SELECT 1 FROM registry_signer_trust_roots root
                      WHERE root.source_label = trusted_registry_snapshots.source_label
                        AND root.signer_principal = trusted_registry_snapshots.signer_principal
                        AND root.allowed_signers_digest = trusted_registry_snapshots.allowed_signers_digest
                        AND root.status = 'ACTIVE'
                   )
                """,
                (status, snapshot_id),
            ).rowcount
            if not updated:
                raise ValueError("Staged registry snapshot changed or its signer trust is no longer active")
            connection.execute("INSERT INTO staged_registry_snapshot_reviews VALUES(?, ?, ?, ?, ?, ?, ?)", (f"registry-review-{uuid.uuid4()}", snapshot_id, operator_id, decision, reason, preview["compatibility_digest"], utc_now().isoformat()))
        return self.get_staged_registry_snapshot(snapshot_id)

    def list_staged_registry_snapshots(self) -> tuple[Mapping[str, Any], ...]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT snapshot_id FROM trusted_registry_snapshots ORDER BY verified_at, snapshot_id"
            ).fetchall()
        return tuple(self.get_staged_registry_snapshot(str(row["snapshot_id"])) for row in rows)

    def preview_remote_tenant_candidate(self, tenant_id: str, snapshot_id: str, remote_release_id: str) -> Mapping[str, Any]:
        snapshot = self.get_staged_registry_snapshot(snapshot_id)
        release = next((item for item in snapshot["releases"] if item["remote_release_id"] == remote_release_id), None)
        if snapshot["status"] != "REVIEW_APPROVED_NOT_ADOPTABLE" or release is None:
            return {"decision": "NOT_ELIGIBLE", "runtime_effect": "NONE", "tenant_adoption_effect": "NONE"}
        return {"decision": "REQUIRES_TENANT_CONFIRMATION", "tenant_id": tenant_id, "snapshot_id": snapshot_id, "remote_release_id": remote_release_id, "role": release["manifest"]["role"], "runtime_effect": "NONE", "tenant_adoption_effect": "NONE"}

    def propose_remote_tenant_candidate(self, tenant_id: str, snapshot_id: str, remote_release_id: str, *, operator_id: str, reason: str) -> Mapping[str, Any]:
        preview = self.preview_remote_tenant_candidate(tenant_id, snapshot_id, remote_release_id)
        if preview["decision"] != "REQUIRES_TENANT_CONFIRMATION" or not operator_id or not reason:
            raise ValueError("Eligible remote release, operator id, and reason are required for a tenant candidate")
        candidate_id = f"remote-tenant-candidate-{uuid.uuid4()}"
        with self._transaction() as connection:
            inserted = connection.execute(
                """
                INSERT INTO remote_tenant_adoption_candidates
                SELECT ?, ?, snapshot_id, ?, ?, 'PENDING_TENANT_CONFIRMATION', ?, ?, ?, NULL
                  FROM trusted_registry_snapshots
                 WHERE snapshot_id = ?
                   AND status = 'REVIEW_APPROVED_NOT_ADOPTABLE'
                   AND EXISTS (
                     SELECT 1 FROM registry_signer_trust_roots root
                      WHERE root.source_label = trusted_registry_snapshots.source_label
                        AND root.signer_principal = trusted_registry_snapshots.signer_principal
                        AND root.allowed_signers_digest = trusted_registry_snapshots.allowed_signers_digest
                        AND root.status = 'ACTIVE'
                   )
                """,
                (
                    candidate_id,
                    tenant_id,
                    remote_release_id,
                    preview["role"],
                    operator_id,
                    reason,
                    utc_now().isoformat(),
                    snapshot_id,
                ),
            ).rowcount
            if not inserted:
                raise ValueError("Staged registry snapshot changed or its signer trust is no longer active")
        return self.get_remote_tenant_candidate(candidate_id)

    def resolve_remote_tenant_candidate(self, candidate_id: str, *, operator_id: str, decision: str, reason: str) -> Mapping[str, Any]:
        if decision not in {"APPROVE", "REJECT"} or not operator_id or not reason:
            raise ValueError("Tenant candidate resolution requires operator id, reason, and APPROVE or REJECT")
        status = "TENANT_CANDIDATE_APPROVED_NOT_APPLIED" if decision == "APPROVE" else "TENANT_CANDIDATE_REJECTED"
        with self._transaction() as connection:
            updated = connection.execute("UPDATE remote_tenant_adoption_candidates SET status = ?, resolved_at = ? WHERE candidate_id = ? AND status = 'PENDING_TENANT_CONFIRMATION'", (status, utc_now().isoformat(), candidate_id)).rowcount
            if not updated: raise ValueError("Only pending tenant candidates may be resolved")
        return self.get_remote_tenant_candidate(candidate_id)

    def get_remote_tenant_candidate(self, candidate_id: str) -> Mapping[str, Any]:
        with self._lock: row = self._conn.execute("SELECT * FROM remote_tenant_adoption_candidates WHERE candidate_id = ?", (candidate_id,)).fetchone()
        result = self._row(row)
        if result is None: raise KeyError(f"Unknown remote tenant candidate: {candidate_id}")
        result["runtime_effect"] = "NONE"; result["tenant_adoption_effect"] = "NONE"
        return result

    def preview_tenant_adoption(self, tenant_id: str, release_id: str) -> Mapping[str, Any]:
        release = self.get_registry_release(release_id)
        role = str(release["manifest"]["role"])
        with self._lock:
            current = self._conn.execute("SELECT * FROM tenant_registry_adoptions WHERE tenant_id = ? AND role = ? AND status = 'ACTIVE'", (tenant_id, role)).fetchone()
        return {"tenant_id": tenant_id, "release": release, "role": role, "current_adoption": self._row(current), "runtime_effect": "NONE", "company_roster_effect": "NONE"}

    def adopt_registry_release(self, tenant_id: str, release_id: str) -> Mapping[str, Any]:
        preview = self.preview_tenant_adoption(tenant_id, release_id)
        adoption_id = f"tenant-adoption-{uuid.uuid4()}"
        adopted_at = utc_now().isoformat()
        with self._transaction() as connection:
            existing = connection.execute("SELECT adoption_id FROM tenant_registry_adoptions WHERE tenant_id = ? AND role = ? AND status = 'ACTIVE'", (tenant_id, preview["role"])).fetchone()
            if existing:
                connection.execute("UPDATE tenant_registry_adoptions SET status = 'SUPERSEDED' WHERE adoption_id = ?", (existing["adoption_id"],))
            connection.execute("INSERT INTO tenant_registry_adoptions VALUES(?, ?, ?, ?, 'ACTIVE', ?, ?)", (adoption_id, tenant_id, preview["role"], release_id, None if existing is None else existing["adoption_id"], adopted_at))
            event = {"adoption_id": adoption_id, "tenant_id": tenant_id, "release_id": release_id}
            connection.execute("INSERT INTO evolution_evidence_events(event_type, subject_id, payload_json, payload_digest, recorded_at) VALUES('TENANT_REGISTRY_ADOPTED', ?, ?, ?, ?)", (adoption_id, canonical_json(event), content_digest(event), adopted_at))
        return self.get_tenant_adoption(adoption_id)

    def get_tenant_adoption(self, adoption_id: str) -> Mapping[str, Any]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM tenant_registry_adoptions WHERE adoption_id = ?", (adoption_id,)).fetchone()
        result = self._row(row)
        if result is None:
            raise KeyError(f"Unknown tenant registry adoption: {adoption_id}")
        return result

    def rollback_tenant_adoption(self, tenant_id: str, role: str) -> Mapping[str, Any]:
        with self._transaction() as connection:
            current = connection.execute("SELECT * FROM tenant_registry_adoptions WHERE tenant_id = ? AND role = ? AND status = 'ACTIVE'", (tenant_id, role)).fetchone()
            if current is None or current["replaced_adoption_id"] is None:
                raise ValueError("Tenant adoption has no prior active release to restore")
            connection.execute("UPDATE tenant_registry_adoptions SET status = 'ROLLED_BACK' WHERE adoption_id = ?", (current["adoption_id"],))
            connection.execute("UPDATE tenant_registry_adoptions SET status = 'ACTIVE' WHERE adoption_id = ?", (current["replaced_adoption_id"],))
        return self.get_tenant_adoption(str(current["replaced_adoption_id"]))

    def list_release_candidates(self) -> tuple[Mapping[str, Any], ...]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT candidate_id FROM blueprint_release_candidates ORDER BY created_at, candidate_id"
            ).fetchall()
        return tuple(self.get_release_candidate(str(row["candidate_id"])) for row in rows)

    def review_release_candidate(
        self, candidate_id: str, *, operator_id: str, decision: str, reason: str
    ) -> Mapping[str, Any]:
        if decision not in {"APPROVE", "REJECT"}:
            raise ValueError("Release candidate review decision must be APPROVE or REJECT")
        if not operator_id or not reason:
            raise ValueError("operator_id and review reason are required")
        recorded_at = utc_now().isoformat()
        with self._transaction() as connection:
            candidate = connection.execute(
                "SELECT * FROM blueprint_release_candidates WHERE candidate_id = ?", (candidate_id,)
            ).fetchone()
            if candidate is None:
                raise KeyError(f"Unknown Blueprint release candidate: {candidate_id}")
            if candidate["status"] != "PENDING_REVIEW":
                raise ValueError("Only PENDING_REVIEW candidates may receive an operator review")
            target_status = "APPROVED_PENDING_SIGNATURE" if decision == "APPROVE" else "REJECTED"
            candidate_digest = content_digest(
                {
                    "candidate_id": candidate_id,
                    "delta_digest": candidate["delta_digest"],
                    "holdout_digest": candidate["holdout_digest"],
                }
            )
            connection.execute(
                "UPDATE blueprint_release_candidates SET status = ? WHERE candidate_id = ?",
                (target_status, candidate_id),
            )
            review_id = f"release-review-{uuid.uuid4()}"
            connection.execute(
                """
                INSERT INTO release_candidate_reviews(
                    review_id, candidate_id, operator_id, decision, reason, candidate_digest, recorded_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (review_id, candidate_id, operator_id, decision, reason, candidate_digest, recorded_at),
            )
            event = {
                "candidate_id": candidate_id,
                "review_id": review_id,
                "operator_id": operator_id,
                "decision": decision,
                "candidate_digest": candidate_digest,
            }
            connection.execute(
                """
                INSERT INTO evolution_evidence_events(event_type, subject_id, payload_json, payload_digest, recorded_at)
                VALUES(?, ?, ?, ?, ?)
                """,
                (
                    "RELEASE_CANDIDATE_OPERATOR_REVIEWED",
                    candidate_id,
                    canonical_json(event),
                    content_digest(event),
                    recorded_at,
                ),
            )
        return self.get_release_candidate(candidate_id)


