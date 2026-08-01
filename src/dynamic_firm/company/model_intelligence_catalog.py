"""Local-only catalog for already supplied Model Intelligence snapshots.

The injected verifier exists solely so provider-free tests can bind a detached
synthetic signature to exact canonical bytes.  This catalog implements neither
production cryptography nor download, provider, route, or Company authority.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .model_intelligence import ModelIntelligenceSnapshot


SyntheticVerifier = Callable[[bytes, str], bool]
_STATES = frozenset({"DOWNLOADED", "VERIFIED_CANDIDATE", "ACTIVE", "RETIRED", "REJECTED"})


def _bounded(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512 or "\x00" in value:
        raise ValueError(f"{field_name} must be a non-empty bounded reference")
    return value


@dataclass(frozen=True, slots=True)
class SnapshotCatalogRecord:
    digest: str
    snapshot_id: str
    state: str
    signature_digest: str
    signature_reference: str
    receipt_status: str


class SnapshotCatalogIntakeError(ValueError):
    """A local catalog preflight failed before it could persist any row."""


@dataclass(frozen=True, slots=True)
class _PreparedCatalogIntake:
    snapshot: ModelIntelligenceSnapshot
    digest: str
    canonical_json: str
    signature: str
    signature_digest: str
    signature_reference: str
    verified: bool
    existing: SnapshotCatalogRecord | None


class SQLiteModelIntelligenceCatalog:
    """Restart-safe candidate/active/retired state, separate from route authority."""

    def __init__(
        self,
        path: Path,
        *,
        synthetic_verifier: SyntheticVerifier,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._path = path
        self._synthetic_verifier = synthetic_verifier
        self._now = now or (lambda: datetime.now(UTC))
        self._connection = sqlite3.connect(path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS model_intelligence_snapshots (
                digest TEXT PRIMARY KEY,
                snapshot_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                detached_signature TEXT NOT NULL,
                signature_digest TEXT NOT NULL,
                signature_reference TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('DOWNLOADED','VERIFIED_CANDIDATE','ACTIVE','RETIRED','REJECTED')),
                receipt_status TEXT NOT NULL CHECK (receipt_status IN ('PENDING','VERIFIED','REJECTED'))
            );
            CREATE TABLE IF NOT EXISTS model_intelligence_catalog_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "SQLiteModelIntelligenceCatalog":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _record(self, digest: str) -> SnapshotCatalogRecord:
        row = self._connection.execute(
            "SELECT digest, snapshot_id, state, signature_digest, signature_reference, receipt_status "
            "FROM model_intelligence_snapshots WHERE digest = ?",
            (digest,),
        ).fetchone()
        if row is None:
            raise KeyError("unknown Model Intelligence snapshot digest")
        record = SnapshotCatalogRecord(**dict(row))
        if record.state not in _STATES:
            raise ValueError("catalog snapshot state is invalid")
        return record

    def _is_expired(self, snapshot: ModelIntelligenceSnapshot) -> bool:
        now = self._now()
        if now.tzinfo is None:
            raise ValueError("catalog clock must be timezone-aware")
        return snapshot.expires_at <= now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    def ingest(
        self,
        snapshot_payload: Mapping[str, object],
        *,
        detached_signature: str,
        signature_reference: str,
    ) -> SnapshotCatalogRecord:
        """Persist an intake after a no-write synthetic verification preflight."""

        return self.finalize_prepared(self.prepare_ingest(
            snapshot_payload,
            detached_signature=detached_signature,
            signature_reference=signature_reference,
        ))

    def prepare_ingest(
        self,
        snapshot_payload: Mapping[str, object],
        *,
        detached_signature: str,
        signature_reference: str,
    ) -> _PreparedCatalogIntake:
        """Check a candidate without writing a DOWNLOADED/PENDING row."""

        snapshot = ModelIntelligenceSnapshot.from_mapping(snapshot_payload)
        signature = _bounded(detached_signature, field_name="detached_signature")
        reference = _bounded(signature_reference, field_name="signature_reference")
        canonical_bytes = snapshot.canonical_bytes()
        digest = snapshot.content_digest
        signature_digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()
        canonical_json = snapshot.canonical_json()
        existing_row = self._connection.execute(
            "SELECT digest, snapshot_id, state, signature_digest, signature_reference, receipt_status "
            "FROM model_intelligence_snapshots WHERE digest = ?",
            (digest,),
        ).fetchone()
        if existing_row is not None:
            existing = SnapshotCatalogRecord(**dict(existing_row))
            if existing.signature_digest != signature_digest or existing.signature_reference != reference:
                raise SnapshotCatalogIntakeError("existing snapshot retry signature or reference mismatch")
            return _PreparedCatalogIntake(
                snapshot, digest, canonical_json, signature, signature_digest, reference,
                existing.state == "VERIFIED_CANDIDATE" and existing.receipt_status == "VERIFIED",
                existing,
            )
        try:
            signature_verified = bool(self._synthetic_verifier(canonical_bytes, signature))
            expired = self._is_expired(snapshot)
        except Exception as exc:
            raise SnapshotCatalogIntakeError("catalog synthetic verification or clock failed") from exc
        return _PreparedCatalogIntake(
            snapshot, digest, canonical_json, signature, signature_digest, reference,
            signature_verified and not expired, None,
        )

    def finalize_prepared(self, prepared: _PreparedCatalogIntake) -> SnapshotCatalogRecord:
        """Persist a fully checked candidate or rejection without PENDING state."""

        if not isinstance(prepared, _PreparedCatalogIntake):
            raise TypeError("prepared must be a catalog intake prepared by this catalog")
        if prepared.existing is not None:
            return prepared.existing
        with self._connection:
            inserted = self._connection.execute(
                "INSERT OR IGNORE INTO model_intelligence_snapshots "
                "(digest, snapshot_id, payload_json, detached_signature, signature_digest, signature_reference, state, receipt_status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    prepared.digest, prepared.snapshot.snapshot_id, prepared.canonical_json,
                    prepared.signature, prepared.signature_digest, prepared.signature_reference,
                    "VERIFIED_CANDIDATE" if prepared.verified else "REJECTED",
                    "VERIFIED" if prepared.verified else "REJECTED",
                ),
            )
            if inserted.rowcount == 0:
                existing = self._record(prepared.digest)
                if (
                    existing.signature_digest != prepared.signature_digest
                    or existing.signature_reference != prepared.signature_reference
                ):
                    raise SnapshotCatalogIntakeError("concurrent snapshot retry signature or reference mismatch")
                return existing
        return self._record(prepared.digest)

    def activate(self, digest: str) -> SnapshotCatalogRecord:
        """Explicitly select a verified candidate; no Job or provider state changes."""

        candidate = self._record(digest)
        if candidate.state != "VERIFIED_CANDIDATE":
            raise ValueError("only a verified candidate may be activated")
        if self._is_expired(self.snapshot(digest)):
            with self._connection:
                self._connection.execute(
                    "UPDATE model_intelligence_snapshots "
                    "SET state = 'REJECTED', receipt_status = 'REJECTED' "
                    "WHERE digest = ? AND state = 'VERIFIED_CANDIDATE'",
                    (digest,),
                )
            raise ValueError("only an unexpired verified candidate may be activated")
        with self._connection:
            active = self._connection.execute(
                "SELECT value FROM model_intelligence_catalog_meta WHERE key = 'active_digest'"
            ).fetchone()
            if active is not None:
                self._connection.execute(
                    "UPDATE model_intelligence_snapshots SET state = 'RETIRED' WHERE digest = ? AND state = 'ACTIVE'",
                    (str(active["value"]),),
                )
            updated = self._connection.execute(
                "UPDATE model_intelligence_snapshots SET state = 'ACTIVE' WHERE digest = ? AND state = 'VERIFIED_CANDIDATE'",
                (digest,),
            ).rowcount
            if updated != 1:
                raise ValueError("candidate state changed before activation")
            self._connection.execute(
                "INSERT OR REPLACE INTO model_intelligence_catalog_meta (key, value) VALUES ('active_digest', ?)",
                (digest,),
            )
        return self._record(digest)

    def active(self) -> SnapshotCatalogRecord | None:
        row = self._connection.execute(
            "SELECT value FROM model_intelligence_catalog_meta WHERE key = 'active_digest'"
        ).fetchone()
        return None if row is None else self._record(str(row["value"]))

    def rollback(self, digest: str) -> SnapshotCatalogRecord:
        """Explicitly restore one retained, previously verified snapshot."""

        target = self._record(digest)
        if target.state not in {"VERIFIED_CANDIDATE", "RETIRED"} or target.receipt_status != "VERIFIED":
            raise ValueError("rollback target must be a retained verified snapshot")
        with self._connection:
            current = self.active()
            if current is not None:
                self._connection.execute(
                    "UPDATE model_intelligence_snapshots SET state = 'RETIRED' WHERE digest = ? AND state = 'ACTIVE'",
                    (current.digest,),
                )
            self._connection.execute(
                "UPDATE model_intelligence_snapshots SET state = 'ACTIVE' WHERE digest = ?",
                (digest,),
            )
            self._connection.execute(
                "INSERT OR REPLACE INTO model_intelligence_catalog_meta (key, value) VALUES ('active_digest', ?)",
                (digest,),
            )
        return self._record(digest)

    def snapshot(self, digest: str) -> ModelIntelligenceSnapshot:
        """Decode and rebind stored bytes before exposing a retained snapshot."""

        row = self._connection.execute(
            "SELECT payload_json FROM model_intelligence_snapshots WHERE digest = ?", (digest,)
        ).fetchone()
        if row is None:
            raise KeyError("unknown Model Intelligence snapshot digest")
        snapshot = ModelIntelligenceSnapshot.from_mapping(json.loads(str(row["payload_json"])))
        if snapshot.content_digest != digest:
            raise ValueError("stored Model Intelligence snapshot payload digest mismatch")
        return snapshot
