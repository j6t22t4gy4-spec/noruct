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


class EvolutionConsentCapsuleMixin:
    """Own consent, Capsule retention, submission, and revocation lifecycle."""
    def grant_consent(
        self,
        *,
        purpose: str,
        allowed_reuse: str,
        retention_days: int,
        authority: str,
    ) -> Mapping[str, Any]:
        consent_id = f"consent-{uuid.uuid4()}"
        granted_at = utc_now().isoformat()
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO evolution_consents(
                    consent_id, purpose, allowed_reuse, retention_days, authority,
                    status, granted_at, withdrawn_at
                ) VALUES(?, ?, ?, ?, ?, 'ACTIVE', ?, NULL)
                """,
                (consent_id, purpose, allowed_reuse, retention_days, authority, granted_at),
            )
        return self.get_consent(consent_id)

    def get_consent(self, consent_id: str) -> Mapping[str, Any]:
        self.purge_expired_local_capsules()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM evolution_consents WHERE consent_id = ?", (consent_id,)
            ).fetchone()
        result = self._row(row)
        if result is None:
            raise KeyError(f"Unknown evolution consent: {consent_id}")
        return result

    def purge_expired_local_capsules(self) -> Mapping[str, int]:
        """Remove local Capsule payloads once their consent retention ends.

        The hosted service has its own pending-only expiry.  This operation is
        deliberately local and idempotent: it removes only the minimized
        payload stored by this SQLite catalog, leaves content-free receipts in
        place, and never contacts a server or changes COMPANY state.
        """

        expired_at = utc_now().isoformat()
        with self._transaction() as connection:
            expired_consents = connection.execute(
                """
                SELECT consent_id
                  FROM evolution_consents
                 WHERE status = 'ACTIVE'
                   AND julianday(?) >= julianday(granted_at) + retention_days
                """,
                (expired_at,),
            ).fetchall()
            if not expired_consents:
                return {"expired_consents": 0, "purged_capsules": 0}
            consent_ids = tuple(str(row["consent_id"]) for row in expired_consents)
            placeholders = ", ".join("?" for _ in consent_ids)
            capsules = connection.execute(
                f"""
                SELECT capsule_id, status
                  FROM learning_capsules
                 WHERE consent_id IN ({placeholders})
                   AND status IN ('QUEUED_LOCAL_ONLY', 'SUBMITTED_HOSTED')
                """,
                consent_ids,
            ).fetchall()
            connection.execute(
                f"""
                UPDATE evolution_consents
                   SET status = 'EXPIRED', withdrawn_at = COALESCE(withdrawn_at, ?)
                 WHERE consent_id IN ({placeholders}) AND status = 'ACTIVE'
                """,
                (expired_at, *consent_ids),
            )
            for capsule in capsules:
                prior_status = str(capsule["status"])
                next_status = (
                    "EXPIRED_LOCAL_ONLY"
                    if prior_status == "QUEUED_LOCAL_ONLY"
                    else "EXPIRED_HOSTED_LOCAL"
                )
                connection.execute(
                    """
                    UPDATE learning_capsules
                       SET payload_json = NULL, status = ?
                     WHERE capsule_id = ? AND status = ?
                    """,
                    (next_status, capsule["capsule_id"], prior_status),
                )
                event = {
                    "capsule_id": str(capsule["capsule_id"]),
                    "prior_status": prior_status,
                    "status": next_status,
                    "reason": "CONSENT_RETENTION_EXPIRED",
                }
                connection.execute(
                    """
                    INSERT INTO evolution_evidence_events(
                        event_type, subject_id, payload_json, payload_digest, recorded_at
                    ) VALUES('LOCAL_CAPSULE_RETENTION_EXPIRED', ?, ?, ?, ?)
                    """,
                    (
                        capsule["capsule_id"],
                        canonical_json(event),
                        content_digest(event),
                        expired_at,
                    ),
                )
        return {
            "expired_consents": len(consent_ids),
            "purged_capsules": len(capsules),
        }

    def withdraw_consent(self, consent_id: str) -> Mapping[str, Any]:
        withdrawn_at = utc_now().isoformat()
        with self._transaction() as connection:
            submitted = connection.execute(
                "SELECT 1 FROM learning_capsules WHERE consent_id = ? AND status = 'SUBMITTED_HOSTED' LIMIT 1",
                (consent_id,),
            ).fetchone()
            if submitted is not None:
                raise ValueError(
                    "Hosted Capsule withdrawal is required before withdrawing this consent; "
                    "withdraw each pending hosted Capsule with its own capability"
                )
            updated = connection.execute(
                """
                UPDATE evolution_consents
                   SET status = 'WITHDRAWN', withdrawn_at = ?
                 WHERE consent_id = ? AND status = 'ACTIVE'
                """,
                (withdrawn_at, consent_id),
            ).rowcount
            if not updated:
                if connection.execute(
                    "SELECT 1 FROM evolution_consents WHERE consent_id = ?", (consent_id,)
                ).fetchone() is None:
                    raise KeyError(f"Unknown evolution consent: {consent_id}")
                raise ValueError(f"Evolution consent is not active: {consent_id}")
            connection.execute(
                """
                UPDATE learning_capsules
                   SET payload_json = NULL, status = 'WITHDRAWN', withdrawn_at = ?
                 WHERE consent_id = ? AND status = 'QUEUED_LOCAL_ONLY'
                """,
                (withdrawn_at, consent_id),
            )
            capsule_rows = connection.execute(
                "SELECT capsule_id FROM learning_capsules WHERE consent_id = ?", (consent_id,)
            ).fetchall()
            self._revoke_candidates_for_capsules(
                connection,
                tuple(str(row["capsule_id"]) for row in capsule_rows),
                "CONSENT_WITHDRAWN",
                withdrawn_at,
            )
        return self.get_consent(consent_id)

    def create_capsule(self, consent_id: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        self.purge_expired_local_capsules()
        capsule_id = f"capsule-{uuid.uuid4()}"
        created_at = utc_now().isoformat()
        payload_json = canonical_json(payload)
        with self._transaction() as connection:
            consent = connection.execute(
                "SELECT status FROM evolution_consents WHERE consent_id = ?", (consent_id,)
            ).fetchone()
            if consent is None:
                raise KeyError(f"Unknown evolution consent: {consent_id}")
            if str(consent["status"]) != "ACTIVE":
                raise ValueError("An active evolution consent is required for capsule submission")
            connection.execute(
                """
                INSERT INTO learning_capsules(
                    capsule_id, consent_id, payload_json, payload_digest, status,
                    created_at, withdrawn_at, transport_state
                ) VALUES(?, ?, ?, ?, 'QUEUED_LOCAL_ONLY', ?, NULL, 'DISABLED')
                """,
                (capsule_id, consent_id, payload_json, content_digest(payload), created_at),
            )
        return self.get_capsule(capsule_id)

    def get_capsule(self, capsule_id: str) -> Mapping[str, Any]:
        self.purge_expired_local_capsules()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM learning_capsules WHERE capsule_id = ?", (capsule_id,)
            ).fetchone()
        result = self._row(row)
        if result is None:
            raise KeyError(f"Unknown learning capsule: {capsule_id}")
        return result

    def withdraw_capsule(self, capsule_id: str) -> Mapping[str, Any]:
        withdrawn_at = utc_now().isoformat()
        with self._transaction() as connection:
            current = connection.execute(
                "SELECT status FROM learning_capsules WHERE capsule_id = ?", (capsule_id,)
            ).fetchone()
            if current is not None and str(current["status"]) == "SUBMITTED_HOSTED":
                raise ValueError(
                    "Hosted Capsule withdrawal requires a server receipt; use evolution network withdraw"
                )
            updated = connection.execute(
                """
                UPDATE learning_capsules
                   SET payload_json = NULL, status = 'WITHDRAWN', withdrawn_at = ?
                 WHERE capsule_id = ? AND status = 'QUEUED_LOCAL_ONLY'
                """,
                (withdrawn_at, capsule_id),
            ).rowcount
            if not updated:
                if connection.execute(
                    "SELECT 1 FROM learning_capsules WHERE capsule_id = ?", (capsule_id,)
                ).fetchone() is None:
                    raise KeyError(f"Unknown learning capsule: {capsule_id}")
                raise ValueError(f"Learning capsule is not queued: {capsule_id}")
            self._revoke_candidates_for_capsules(
                connection, (capsule_id,), "CAPSULE_WITHDRAWN", withdrawn_at
            )
        return self.get_capsule(capsule_id)

    def record_hosted_capsule_submission(
        self,
        capsule_id: str,
        *,
        endpoint_origin: str,
        contribution_id: str,
        receipt_digest: str,
        submitted_at: str,
    ) -> Mapping[str, Any]:
        """Commit a server receipt only after the HTTPS request completed.

        The endpoint is metadata, not a credential.  The token intentionally
        never enters SQLite; a later withdrawal requires the caller to supply
        it again from their environment.
        """
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT status FROM learning_capsules WHERE capsule_id = ?", (capsule_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown learning capsule: {capsule_id}")
            status = str(row["status"])
            existing = connection.execute(
                "SELECT * FROM hosted_capsule_receipts WHERE capsule_id = ?", (capsule_id,)
            ).fetchone()
            if status == "SUBMITTED_HOSTED":
                if existing is None or (
                    str(existing["endpoint_origin"]) != endpoint_origin
                    or str(existing["contribution_id"]) != contribution_id
                    or str(existing["submission_receipt_digest"]) != receipt_digest
                ):
                    raise ValueError("Hosted Capsule receipt conflicts with the existing local receipt")
                return self.get_capsule(capsule_id)
            if status != "QUEUED_LOCAL_ONLY":
                raise ValueError("Only a queued local Capsule may receive a hosted submission receipt")
            connection.execute(
                """
                INSERT INTO hosted_capsule_receipts(
                    capsule_id, endpoint_origin, contribution_id, submission_receipt_digest,
                    submitted_at, withdrawal_receipt_digest, withdrawn_at
                ) VALUES(?, ?, ?, ?, ?, NULL, NULL)
                """,
                (capsule_id, endpoint_origin, contribution_id, receipt_digest, submitted_at),
            )
            connection.execute(
                """
                UPDATE learning_capsules
                   SET status = 'SUBMITTED_HOSTED', transport_state = 'HOSTED_RECEIPT_RECORDED'
                 WHERE capsule_id = ?
                """,
                (capsule_id,),
            )
        return self.get_capsule(capsule_id)

    def hosted_capsule_receipt(self, capsule_id: str) -> Mapping[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM hosted_capsule_receipts WHERE capsule_id = ?", (capsule_id,)
            ).fetchone()
        result = self._row(row)
        if result is None:
            raise KeyError(f"No hosted receipt exists for learning capsule: {capsule_id}")
        return result

    def submitted_capsules_for_consent(self, consent_id: str) -> tuple[Mapping[str, Any], ...]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT capsule_id FROM learning_capsules WHERE consent_id = ? AND status = 'SUBMITTED_HOSTED' ORDER BY created_at, capsule_id",
                (consent_id,),
            ).fetchall()
        return tuple(self.get_capsule(str(row["capsule_id"])) for row in rows)

    def record_hosted_capsule_withdrawal(
        self,
        capsule_id: str,
        *,
        receipt_digest: str,
        withdrawn_at: str,
    ) -> Mapping[str, Any]:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT status FROM learning_capsules WHERE capsule_id = ?", (capsule_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown learning capsule: {capsule_id}")
            if str(row["status"]) == "WITHDRAWN":
                return self.get_capsule(capsule_id)
            if str(row["status"]) != "SUBMITTED_HOSTED":
                raise ValueError("Only a hosted-submitted Capsule may receive a hosted withdrawal receipt")
            receipt = connection.execute(
                "SELECT 1 FROM hosted_capsule_receipts WHERE capsule_id = ?", (capsule_id,)
            ).fetchone()
            if receipt is None:
                raise ValueError("Hosted Capsule is missing its local submission receipt")
            connection.execute(
                """
                UPDATE hosted_capsule_receipts
                   SET withdrawal_receipt_digest = ?, withdrawn_at = ?
                 WHERE capsule_id = ?
                """,
                (receipt_digest, withdrawn_at, capsule_id),
            )
            connection.execute(
                """
                UPDATE learning_capsules
                   SET payload_json = NULL, status = 'WITHDRAWN', withdrawn_at = ?,
                       transport_state = 'HOSTED_WITHDRAWN_RECEIPT_RECORDED'
                 WHERE capsule_id = ?
                """,
                (withdrawn_at, capsule_id),
            )
            self._revoke_candidates_for_capsules(
                connection, (capsule_id,), "CAPSULE_WITHDRAWN", withdrawn_at
            )
        return self.get_capsule(capsule_id)

    @staticmethod
    def _revoke_candidates_for_capsules(
        connection: sqlite3.Connection,
        capsule_ids: tuple[str, ...],
        reason: str,
        revoked_at: str,
    ) -> None:
        if not capsule_ids:
            return
        placeholders = ",".join("?" for _ in capsule_ids)
        rows = connection.execute(
            f"""
            SELECT DISTINCT candidate_id FROM release_candidate_capsules
             WHERE capsule_id IN ({placeholders})
            """,
            capsule_ids,
        ).fetchall()
        for row in rows:
            candidate_id = str(row["candidate_id"])
            updated = connection.execute(
                """
                UPDATE blueprint_release_candidates
                   SET status = 'REVOKED', revocation_reason = ?, revoked_at = ?
                 WHERE candidate_id = ? AND status IN ('PENDING_REVIEW', 'APPROVED_PENDING_SIGNATURE')
                """,
                (reason, revoked_at, candidate_id),
            ).rowcount
            if updated:
                payload = {"candidate_id": candidate_id, "reason": reason}
                connection.execute(
                    """
                    INSERT INTO evolution_evidence_events(
                        event_type, subject_id, payload_json, payload_digest, recorded_at
                    ) VALUES('RELEASE_CANDIDATE_REVOKED', ?, ?, ?, ?)
                    """,
                    (candidate_id, canonical_json(payload), content_digest(payload), revoked_at),
                )


