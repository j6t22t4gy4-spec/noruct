"""Provider-free composition of strict snapshot intake and local cataloging.

This adapter deliberately stops before activation.  It validates canonical
bytes with the synthetic trust gate, records a local candidate, and advances
the verifier's replay cursor only if the catalog independently records a
verified candidate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum

from .model_intelligence_catalog import (
    SQLiteModelIntelligenceCatalog,
    SnapshotCatalogIntakeError,
    SnapshotCatalogRecord,
    _PreparedCatalogIntake,
)
from .snapshot_intake_security import IntakeStatus, SnapshotIntakeEnvelope, SnapshotIntakeReceipt, SnapshotIntakeVerifier


@dataclass(frozen=True, slots=True)
class SecureSnapshotCatalogRecord:
    """Safe catalog projection; the signature reference remains catalog-internal."""

    digest: str
    snapshot_id: str
    state: str
    signature_digest: str
    receipt_status: str

    @classmethod
    def from_catalog_record(cls, record: SnapshotCatalogRecord) -> "SecureSnapshotCatalogRecord":
        return cls(
            digest=record.digest,
            snapshot_id=record.snapshot_id,
            state=record.state,
            signature_digest=record.signature_digest,
            receipt_status=record.receipt_status,
        )


class SecureSnapshotCatalogFinalStatus(StrEnum):
    INTAKE_REJECTED = "INTAKE_REJECTED"
    CATALOG_REJECTED = "CATALOG_REJECTED"
    ACCEPTED = "ACCEPTED"


@dataclass(frozen=True, slots=True)
class SecureSnapshotCatalogIntakeReceipt:
    """Content-free composition result; raw snapshot and signature stay internal."""

    final_status: SecureSnapshotCatalogFinalStatus
    intake: SnapshotIntakeReceipt
    catalog_record: SecureSnapshotCatalogRecord | None


class SecureSnapshotCatalogIntake:
    """Locally compose prepare/catalog/commit without route or activation authority."""

    def __init__(self, verifier: SnapshotIntakeVerifier, catalog: SQLiteModelIntelligenceCatalog) -> None:
        if not isinstance(verifier, SnapshotIntakeVerifier):
            raise TypeError("verifier must be SnapshotIntakeVerifier")
        if not isinstance(catalog, SQLiteModelIntelligenceCatalog):
            raise TypeError("catalog must be SQLiteModelIntelligenceCatalog")
        self._verifier = verifier
        self._catalog = catalog

    def ingest(
        self,
        envelope: SnapshotIntakeEnvelope,
        *,
        detached_signature: str,
    ) -> SecureSnapshotCatalogIntakeReceipt:
        """Record only a security-approved candidate; never activate it."""

        catalog_prepared: _PreparedCatalogIntake | None = None
        catalog_record: SecureSnapshotCatalogRecord | None = None
        final_status: SecureSnapshotCatalogFinalStatus | None = None

        def approve(_: SnapshotIntakeReceipt) -> bool:
            nonlocal catalog_prepared, catalog_record, final_status
            payload = json.loads(envelope.snapshot_canonical_bytes.decode("utf-8"))
            if not isinstance(payload, dict):  # Defensive: verifier accepted only a mapping.
                raise ValueError("accepted snapshot canonical payload must be an object")
            try:
                catalog_prepared = self._catalog.prepare_ingest(
                    payload,
                    detached_signature=detached_signature,
                    signature_reference=envelope.synthetic_signature_reference,
                )
            except SnapshotCatalogIntakeError:
                final_status = SecureSnapshotCatalogFinalStatus.CATALOG_REJECTED
                return False
            if catalog_prepared.verified:
                return True
            record = self._catalog.finalize_prepared(catalog_prepared)
            catalog_record = SecureSnapshotCatalogRecord.from_catalog_record(record)
            final_status = SecureSnapshotCatalogFinalStatus.CATALOG_REJECTED
            return False

        def finalize() -> None:
            nonlocal catalog_record, final_status
            if catalog_prepared is None:  # Defensive: approval is required before finalization.
                raise ValueError("catalog finalization lacks a prepared intake")
            record = self._catalog.finalize_prepared(catalog_prepared)
            catalog_record = SecureSnapshotCatalogRecord.from_catalog_record(record)
            final_status = SecureSnapshotCatalogFinalStatus.ACCEPTED

        receipt = self._verifier.commit_if_current(envelope, approve=approve, finalize=finalize)
        if final_status is None:
            final_status = (
                SecureSnapshotCatalogFinalStatus.INTAKE_REJECTED
                if receipt.status is IntakeStatus.REJECTED
                else SecureSnapshotCatalogFinalStatus.CATALOG_REJECTED
            )
        return SecureSnapshotCatalogIntakeReceipt(
            final_status=final_status,
            intake=receipt,
            catalog_record=catalog_record,
        )
