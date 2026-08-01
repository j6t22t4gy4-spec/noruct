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


class EvolutionBlueprintMixin:
    """Own local Employee Blueprint import, selection, and rollback lifecycle."""
    def import_blueprint(self, manifest: Mapping[str, Any]) -> Mapping[str, Any]:
        blueprint_id = str(manifest["blueprint_id"])
        version = str(manifest["version"])
        manifest_json = canonical_json(manifest)
        digest = content_digest(manifest)
        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT manifest_digest FROM employee_blueprints
                 WHERE blueprint_id = ? AND version = ?
                """,
                (blueprint_id, version),
            ).fetchone()
            if existing is not None:
                if str(existing["manifest_digest"]) != digest:
                    raise ValueError(
                        "A Blueprint id/version is immutable; import a new version for changed content"
                    )
                return self.get_blueprint(blueprint_id, version)
            connection.execute(
                """
                INSERT INTO employee_blueprints(
                    blueprint_id, version, role, manifest_json, manifest_digest, imported_at
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                (blueprint_id, version, manifest["role"], manifest_json, digest, utc_now().isoformat()),
            )
        return self.get_blueprint(blueprint_id, version)

    def get_blueprint(self, blueprint_id: str, version: str) -> Mapping[str, Any]:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM employee_blueprints
                 WHERE blueprint_id = ? AND version = ?
                """,
                (blueprint_id, version),
            ).fetchone()
        result = self._row(row)
        if result is None:
            raise KeyError(f"Unknown Employee Blueprint: {blueprint_id}@{version}")
        return result

    def list_blueprints(self) -> tuple[Mapping[str, Any], ...]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM employee_blueprints ORDER BY role, blueprint_id, version"
            ).fetchall()
        return tuple(self._row(row) for row in rows)

    def select_blueprint(self, blueprint_id: str, version: str) -> Mapping[str, Any]:
        blueprint = self.get_blueprint(blueprint_id, version)
        selection_id = f"selection-{uuid.uuid4()}"
        with self._transaction() as connection:
            previous = connection.execute(
                """
                SELECT selection_id FROM blueprint_selections
                 WHERE role = ? AND status = 'ACTIVE'
                """,
                (blueprint["role"],),
            ).fetchone()
            if previous is not None:
                connection.execute(
                    "UPDATE blueprint_selections SET status = 'SUPERSEDED' WHERE selection_id = ?",
                    (previous["selection_id"],),
                )
            connection.execute(
                """
                INSERT INTO blueprint_selections(
                    selection_id, role, blueprint_id, version, selected_at, status,
                    replaced_selection_id
                ) VALUES(?, ?, ?, ?, ?, 'ACTIVE', ?)
                """,
                (
                    selection_id,
                    blueprint["role"],
                    blueprint_id,
                    version,
                    utc_now().isoformat(),
                    None if previous is None else previous["selection_id"],
                ),
            )
        return self.get_selection(selection_id)

    def get_selection(self, selection_id: str) -> Mapping[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM blueprint_selections WHERE selection_id = ?", (selection_id,)
            ).fetchone()
        result = self._row(row)
        if result is None:
            raise KeyError(f"Unknown Blueprint selection: {selection_id}")
        return result

    def rollback_selection(self, role: str) -> Mapping[str, Any]:
        with self._transaction() as connection:
            current = connection.execute(
                """
                SELECT * FROM blueprint_selections
                 WHERE role = ? AND status = 'ACTIVE'
                """,
                (role,),
            ).fetchone()
            if current is None:
                raise ValueError(f"No active Blueprint selection for role: {role}")
            previous_id = current["replaced_selection_id"]
            if previous_id is None:
                raise ValueError("The first Blueprint selection has no prior selection to restore")
            connection.execute(
                "UPDATE blueprint_selections SET status = 'ROLLED_BACK' WHERE selection_id = ?",
                (current["selection_id"],),
            )
            restored = connection.execute(
                """
                UPDATE blueprint_selections SET status = 'ACTIVE'
                 WHERE selection_id = ? AND status = 'SUPERSEDED'
                """,
                (previous_id,),
            ).rowcount
            if not restored:
                raise ValueError("The prior Blueprint selection cannot be restored")
        return self.get_selection(str(previous_id))

