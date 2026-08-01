"""Immutable, durable execution-route bindings owned by ``RunStore``."""

from __future__ import annotations

import sqlite3

from dynamic_firm.company.execution_route_binding import ExecutionRouteBinding
from dynamic_firm.company.frozen_route_admission import FrozenRouteAdmission


class RunStoreFrozenRouteMixin:
    """Persist one exact route binding with its physical Employee run."""

    @staticmethod
    def _binding_from_row(row: sqlite3.Row) -> ExecutionRouteBinding:
        raw_json = str(row["binding_json"])
        binding = ExecutionRouteBinding.from_canonical_json(raw_json)
        if binding.canonical_json() != raw_json:
            raise ValueError("Persisted frozen route binding is not canonical")
        if binding.digest != str(row["binding_digest"]):
            raise ValueError("Persisted frozen route binding digest does not match")
        return binding

    def _get_frozen_route_binding_in_transaction(
        self, conn: sqlite3.Connection, run_id: str
    ) -> ExecutionRouteBinding | None:
        row = conn.execute(
            "SELECT binding_json, binding_digest FROM employee_run_frozen_routes WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return self._binding_from_row(row) if row is not None else None

    @staticmethod
    def _admission_from_row(row: sqlite3.Row) -> FrozenRouteAdmission:
        raw_json = str(row["admission_json"])
        admission = FrozenRouteAdmission.from_canonical_json(raw_json)
        if admission.canonical_json() != raw_json:
            raise ValueError("Persisted frozen route admission is not canonical")
        if admission.digest != str(row["admission_digest"]):
            raise ValueError("Persisted frozen route admission digest does not match")
        return admission

    def _get_frozen_route_admission_in_transaction(
        self, conn: sqlite3.Connection, run_id: str
    ) -> FrozenRouteAdmission | None:
        row = conn.execute(
            "SELECT admission_json, admission_digest FROM employee_run_frozen_route_admissions WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return self._admission_from_row(row) if row is not None else None

    @staticmethod
    def _insert_frozen_route_binding_in_transaction(
        conn: sqlite3.Connection,
        run_id: str,
        binding: ExecutionRouteBinding,
        created_at: str,
    ) -> None:
        if not isinstance(binding, ExecutionRouteBinding):
            raise TypeError("frozen_route_binding must be an ExecutionRouteBinding")
        conn.execute(
            """
            INSERT INTO employee_run_frozen_routes(
                run_id, binding_json, binding_digest, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (run_id, binding.canonical_json(), binding.digest, created_at),
        )

    @staticmethod
    def _insert_frozen_route_admission_in_transaction(
        conn: sqlite3.Connection,
        run_id: str,
        admission: FrozenRouteAdmission,
        created_at: str,
    ) -> None:
        if not isinstance(admission, FrozenRouteAdmission):
            raise TypeError("frozen_route_admission must be a FrozenRouteAdmission")
        conn.execute(
            """
            INSERT INTO employee_run_frozen_route_admissions(
                run_id, admission_json, admission_digest, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (run_id, admission.canonical_json(), admission.digest, created_at),
        )

    def get_frozen_route_binding(self, run_id: str) -> ExecutionRouteBinding | None:
        """Return the verified immutable binding, or ``None`` for an unbound run."""

        with self._lock:
            run = self._conn.execute(
                "SELECT 1 FROM employee_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise KeyError(f"Unknown run: {run_id}")
            return self._get_frozen_route_binding_in_transaction(self._conn, run_id)

    def get_frozen_route_admission(self, run_id: str) -> FrozenRouteAdmission | None:
        """Return the verified admission, or ``None`` for a legacy run.

        Admissions are inseparable from their frozen binding.  A direct storage
        tamper that leaves either side absent or divergent is rejected instead
        of exposing a partially trustworthy durable record.
        """

        with self._lock:
            run = self._conn.execute(
                "SELECT 1 FROM employee_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise KeyError(f"Unknown run: {run_id}")
            admission = self._get_frozen_route_admission_in_transaction(
                self._conn, run_id
            )
            if admission is None:
                return None
            binding = self._get_frozen_route_binding_in_transaction(self._conn, run_id)
            if binding is None or binding != admission.binding:
                raise ValueError("Persisted frozen route admission does not match binding")
            return admission

    def resolve_frozen_route_binding(self, physical_id: str) -> ExecutionRouteBinding:
        """Resolve one provider physical ID to its sole durable frozen binding.

        Ordinary and streaming calls carry a run ID; structured calls carry a
        request ID.  A value matching more than one physical run is ambiguous
        and therefore intentionally fails closed rather than choosing one.
        """

        with self._lock:
            run_id = self._resolve_frozen_run_id_in_transaction(self._conn, physical_id)
            binding = self._get_frozen_route_binding_in_transaction(self._conn, run_id)
        if binding is None:
            raise ValueError("Physical run has no frozen route binding")
        return binding

    @staticmethod
    def _validate_physical_id(physical_id: str) -> None:
        if not isinstance(physical_id, str) or not physical_id:
            raise ValueError("A nonempty physical provider identifier is required")

    def _resolve_frozen_run_id_in_transaction(
        self, conn: sqlite3.Connection, physical_id: str
    ) -> str:
        self._validate_physical_id(physical_id)
        rows = conn.execute(
            """
            SELECT run_id FROM employee_runs
            WHERE run_id = ? OR request_id = ?
            """,
            (physical_id, physical_id),
        ).fetchall()
        if not rows:
            raise KeyError("Unknown frozen-route physical identifier")
        if len(rows) != 1:
            raise ValueError("Frozen-route physical identifier is ambiguous")
        return str(rows[0]["run_id"])

    def resolve_frozen_route_admission(self, physical_id: str) -> FrozenRouteAdmission:
        """Resolve a physical provider ID to one verified durable admission.

        This is intentionally separate from the legacy binding-only resolver:
        routes that opt into admission-required dispatch must reject a missing,
        tampered, or divergent admission before provider construction.
        """

        with self._lock:
            run_id = self._resolve_frozen_run_id_in_transaction(self._conn, physical_id)
            admission = self._get_frozen_route_admission_in_transaction(
                self._conn, run_id
            )
            binding = self._get_frozen_route_binding_in_transaction(self._conn, run_id)
        if admission is None or binding is None or admission.binding != binding:
            raise ValueError("Physical run has no matching frozen route admission")
        return admission
