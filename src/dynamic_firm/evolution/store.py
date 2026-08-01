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
from .store_artifact_network import EvolutionArtifactNetworkMixin
from .store_artifact_regression import EvolutionArtifactRegressionMixin
from .store_artifact_shadow import EvolutionArtifactShadowMixin
from .store_artifact_registry import EvolutionArtifactRegistryMixin
from .store_blueprints import EvolutionBlueprintMixin
from .store_consents import EvolutionConsentCapsuleMixin
from .store_release_registry import EvolutionReleaseRegistryMixin
from .store_schema import EvolutionStoreSchemaMixin


class EvolutionStore(
    EvolutionStoreSchemaMixin,
    EvolutionConsentCapsuleMixin,
    EvolutionReleaseRegistryMixin,
    EvolutionBlueprintMixin,
    EvolutionArtifactNetworkMixin,
    EvolutionArtifactShadowMixin,
    EvolutionArtifactRegressionMixin,
    EvolutionArtifactRegistryMixin,
):
    """Separate local state owner for consented, non-raw learning metadata.

    This database is intentionally not a COMPANY/ROSTER store.  Selecting a
    shared Blueprint only changes this local catalog selection and cannot hire,
    modify, or execute an employee.
    """

    def __init__(self, path: str | Path, *, timeout_seconds: float = 5.0) -> None:
        self.path = Path(path).expanduser().resolve()
        if not 0 <= timeout_seconds <= 60:
            raise ValueError("Evolution store timeout_seconds must be from 0 to 60")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.path,
            timeout=timeout_seconds,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._initialize_schema()
        # A local-only Capsule is still consent-bound.  Opening the optional
        # catalog must never extend the retention window merely because no
        # later CLI command happened to touch that Capsule.
        self.purge_expired_local_capsules()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "EvolutionStore":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        payload = result.get("payload_json")
        manifest = result.get("manifest_json")
        if payload is not None:
            result["payload"] = json.loads(str(payload))
        result.pop("payload_json", None)
        if manifest is not None:
            result["manifest"] = json.loads(str(manifest))
        result.pop("manifest_json", None)
        return result

    def status(self) -> Mapping[str, Any]:
        with self._lock:
            counts = {
                key: int(value)
                for key, value in self._conn.execute(
                    """
                    SELECT 'active_consents' AS key, COUNT(*) AS value
                      FROM evolution_consents WHERE status = 'ACTIVE'
                    UNION ALL SELECT 'queued_local_only_capsules', COUNT(*)
                      FROM learning_capsules WHERE status = 'QUEUED_LOCAL_ONLY'
                    UNION ALL SELECT 'submitted_hosted_capsules', COUNT(*)
                      FROM learning_capsules WHERE status = 'SUBMITTED_HOSTED'
                    UNION ALL SELECT 'withdrawn_capsules', COUNT(*)
                      FROM learning_capsules WHERE status = 'WITHDRAWN'
                    UNION ALL SELECT 'retention_expired_local_capsules', COUNT(*)
                      FROM learning_capsules
                     WHERE status IN ('EXPIRED_LOCAL_ONLY', 'EXPIRED_HOSTED_LOCAL')
                    UNION ALL SELECT 'blueprints', COUNT(*) FROM employee_blueprints
                    UNION ALL SELECT 'active_selections', COUNT(*)
                      FROM blueprint_selections WHERE status = 'ACTIVE'
                    UNION ALL SELECT 'release_candidates', COUNT(*)
                      FROM blueprint_release_candidates
                    UNION ALL SELECT 'revoked_release_candidates', COUNT(*)
                      FROM blueprint_release_candidates WHERE status = 'REVOKED'
                    UNION ALL SELECT 'staged_registry_snapshots', COUNT(*)
                      FROM trusted_registry_snapshots WHERE status = 'STAGED_TRUSTED_NOT_ADOPTABLE'
                    UNION ALL SELECT 'available_artifact_versions', COUNT(*)
                      FROM evolution_artifact_versions
                    UNION ALL SELECT 'installed_artifact_versions', COUNT(*)
                      FROM evolution_artifact_installations WHERE status = 'INSTALLED'
                    UNION ALL SELECT 'active_artifact_versions', COUNT(*)
                      FROM evolution_artifact_activations WHERE status = 'ACTIVE'
                    UNION ALL SELECT 'tracking_artifact_subscriptions', COUNT(*)
                      FROM evolution_update_subscriptions WHERE mode = 'TRACK_STABLE' AND status = 'ACTIVE'
                    UNION ALL SELECT 'artifact_shadow_receipts', COUNT(*)
                      FROM evolution_artifact_shadow_receipts
                    UNION ALL SELECT 'passed_artifact_shadow_receipts', COUNT(*)
                      FROM evolution_artifact_shadow_receipts WHERE result = 'PASS'
                    UNION ALL SELECT 'artifact_regression_signals', COUNT(*)
                      FROM evolution_artifact_regression_signals
                    """
                ).fetchall()
            }
        return {
            "schema_version": EVOLUTION_STORE_SCHEMA_VERSION,
            "network_transport": "DISABLED",
            "remote_worker_execution": "DISABLED",
            **counts,
        }

    def export_payload(self) -> Mapping[str, Any]:
        with self._lock:
            consents = self._conn.execute(
                "SELECT * FROM evolution_consents ORDER BY granted_at, consent_id"
            ).fetchall()
            capsules = self._conn.execute(
                "SELECT * FROM learning_capsules ORDER BY created_at, capsule_id"
            ).fetchall()
            blueprints = self._conn.execute(
                "SELECT * FROM employee_blueprints ORDER BY role, blueprint_id, version"
            ).fetchall()
            selections = self._conn.execute(
                "SELECT * FROM blueprint_selections ORDER BY selected_at, selection_id"
            ).fetchall()
            events = self._conn.execute(
                "SELECT * FROM evolution_evidence_events ORDER BY event_id"
            ).fetchall()
        return {
            "schema": "noruct.evolution-export.v1",
            "status": self.status(),
            "consents": tuple(self._row(row) for row in consents),
            "capsules": tuple(self._row(row) for row in capsules),
            "blueprints": tuple(self._row(row) for row in blueprints),
            "selections": tuple(self._row(row) for row in selections),
            "release_candidates": self.list_release_candidates(),
            "staged_registry_snapshots": self.list_staged_registry_snapshots(),
            "artifacts": self.list_artifact_versions(),
            "artifact_installations": self.list_artifact_installations(),
            "artifact_shadow_receipts": self.list_artifact_shadow_receipts(),
            "artifact_regression_signals": self.list_artifact_regression_signals(),
            "evidence_events": tuple(self._row(row) for row in events),
        }
