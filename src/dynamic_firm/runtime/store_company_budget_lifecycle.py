"""Company cost-budget reservation and settlement lifecycle for RunStore."""

from __future__ import annotations

import math
import sqlite3
import uuid
from datetime import datetime
from typing import Any, Mapping

from .models import utc_now


class RunStoreCompanyBudgetLifecycleMixin:
    """Durable company budget lease, incident and settlement transitions."""

    @staticmethod
    def _company_budget_incident(row: Mapping[str, Any]):
        from .company_budget import CompanyBudgetIncident

        return CompanyBudgetIncident(
            incident_id=str(row["incident_id"]),
            company_revision=int(row["company_revision"]),
            window_kind=str(row["window_kind"]),
            window_start=str(row["window_start"]),
            window_end=str(row["window_end"]),
            budget_limit_usd=float(row["budget_limit_usd"]),
            observed_cost_usd=float(row["observed_cost_usd"]),
            reserved_cost_usd=float(row["reserved_cost_usd"]),
            requested_cost_usd=float(row["requested_cost_usd"]),
            status=str(row["status"]),
            created_at=str(row["created_at"]),
            resolved_at=None if row["resolved_at"] is None else str(row["resolved_at"]),
        )

    @staticmethod
    def _company_budget_lease(row: Mapping[str, Any]):
        from .company_budget import CompanyBudgetLease

        return CompanyBudgetLease(
            job_id=str(row["job_id"]),
            request_id=str(row["request_id"]),
            company_revision=int(row["company_revision"]),
            window_start=str(row["window_start"]),
            window_end=str(row["window_end"]),
            reserved_cost_usd=float(row["reserved_cost_usd"]),
        )

    @staticmethod
    def _validate_company_budget_lease_identity(
        row: Mapping[str, Any],
        lease: Any,
    ) -> None:
        """Reject stale or forged lease projections before terminalization."""

        stored_reservation = float(row["reserved_cost_usd"])
        if (
            not math.isfinite(stored_reservation)
            or not math.isfinite(lease.reserved_cost_usd)
            or str(row["request_id"]) != lease.request_id
            or int(row["company_revision"]) != lease.company_revision
            or str(row["window_start"]) != lease.window_start
            or str(row["window_end"]) != lease.window_end
            or abs(stored_reservation - lease.reserved_cost_usd) > 1e-12
        ):
            raise ValueError("Company budget lease identity mismatch")

    @staticmethod
    def _company_budget_totals(
        conn: sqlite3.Connection,
        *,
        window_start: str,
        window_end: str,
    ) -> tuple[float, float]:
        observed = conn.execute(
            """
            SELECT COALESCE(SUM(actual_cost_usd), 0) AS total
            FROM company_budget_leases
            WHERE status = 'SETTLED' AND settled_at >= ? AND settled_at < ?
            """,
            (window_start, window_end),
        ).fetchone()
        reserved = conn.execute(
            """
            SELECT COALESCE(SUM(reserved_cost_usd), 0) AS total
            FROM company_budget_leases
            WHERE status = 'ACTIVE' AND admitted_at >= ? AND admitted_at < ?
            """,
            (window_start, window_end),
        ).fetchone()
        return float(observed["total"]), float(reserved["total"])

    @staticmethod
    def _open_company_budget_incident(
        conn: sqlite3.Connection,
        *,
        window_start: str | None = None,
    ) -> sqlite3.Row | None:
        if window_start is None:
            return conn.execute(
                """
                SELECT * FROM company_budget_incidents
                WHERE status = 'OPEN'
                ORDER BY created_at, incident_id
                LIMIT 1
                """
            ).fetchone()
        return conn.execute(
            """
            SELECT * FROM company_budget_incidents
            WHERE status = 'OPEN' AND window_start = ?
            ORDER BY created_at, incident_id
            LIMIT 1
            """,
            (window_start,),
        ).fetchone()

    def _create_company_budget_incident(
        self,
        conn: sqlite3.Connection,
        *,
        company_revision: int,
        window_kind: str,
        window_start: str,
        window_end: str,
        budget_limit_usd: float,
        observed_cost_usd: float,
        reserved_cost_usd: float,
        requested_cost_usd: float,
        now: str,
    ):
        existing = self._open_company_budget_incident(conn, window_start=window_start)
        if existing is not None:
            return self._company_budget_incident(existing)
        incident_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO company_budget_incidents(
                incident_id, company_revision, window_kind, window_start, window_end,
                budget_limit_usd, observed_cost_usd, reserved_cost_usd,
                requested_cost_usd, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)
            """,
            (
                incident_id,
                company_revision,
                window_kind,
                window_start,
                window_end,
                budget_limit_usd,
                observed_cost_usd,
                reserved_cost_usd,
                requested_cost_usd,
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO company_budget_pause_state(scope, incident_id, paused_at)
            VALUES ('company', ?, ?)
            ON CONFLICT(scope) DO UPDATE SET incident_id = excluded.incident_id,
                paused_at = excluded.paused_at
            """,
            (incident_id, now),
        )
        row = conn.execute(
            "SELECT * FROM company_budget_incidents WHERE incident_id = ?", (incident_id,)
        ).fetchone()
        assert row is not None
        return self._company_budget_incident(row)

    def admit_company_budget_job(
        self,
        *,
        job_id: str,
        request_id: str,
        company_revision: int,
        window_start: datetime,
        window_end: datetime,
        budget_limit_usd: float,
        reserved_cost_usd: float,
    ):
        """Reserve one full Job ceiling or atomically open the company pause."""

        from .company_budget import CompanyBudgetAdmission

        if not job_id.strip() or not request_id.strip():
            raise ValueError("Company budget admission requires job and request identity")
        if (
            company_revision < 0
            or not math.isfinite(budget_limit_usd)
            or not math.isfinite(reserved_cost_usd)
            or budget_limit_usd <= 0
            or reserved_cost_usd < 0
        ):
            raise ValueError("Company budget admission values are invalid")
        if window_start.tzinfo is None or window_end.tzinfo is None or window_start >= window_end:
            raise ValueError("Company budget window is invalid")
        start = window_start.isoformat()
        end = window_end.isoformat()
        window_kind = "lifetime" if window_start.year == 1970 else "calendar_month_utc"
        now = utc_now().isoformat()
        with self._transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM company_budget_leases WHERE job_id = ?", (job_id,)
            ).fetchone()
            if existing is not None:
                identity_matches = (
                    str(existing["request_id"]) == request_id
                    and int(existing["company_revision"]) == company_revision
                    and str(existing["window_start"]) == start
                    and str(existing["window_end"]) == end
                    and abs(float(existing["budget_limit_usd"]) - budget_limit_usd)
                    <= 1e-12
                    and abs(float(existing["reserved_cost_usd"]) - reserved_cost_usd)
                    <= 1e-12
                )
                if not identity_matches:
                    raise ValueError(
                        "Company budget job identity conflicts with an existing lease"
                    )
                if str(existing["status"]) == "ACTIVE":
                    return CompanyBudgetAdmission(
                        True, self._company_budget_lease(existing), None
                    )
                raise ValueError("A settled company budget job cannot be admitted again")
            paused = conn.execute(
                "SELECT incident_id FROM company_budget_pause_state WHERE scope = 'company'"
            ).fetchone()
            if paused is not None:
                incident = conn.execute(
                    "SELECT * FROM company_budget_incidents WHERE incident_id = ?",
                    (str(paused["incident_id"]),),
                ).fetchone()
                if incident is None:
                    raise RuntimeError("Company budget pause references a missing incident")
                return CompanyBudgetAdmission(
                    False,
                    None,
                    self._company_budget_incident(incident),
                    "Company cost budget is paused pending explicit operator resolution.",
                )
            observed, reserved = self._company_budget_totals(
                conn, window_start=start, window_end=end
            )
            if observed + reserved + reserved_cost_usd > budget_limit_usd + 1e-12:
                incident = self._create_company_budget_incident(
                    conn,
                    company_revision=company_revision,
                    window_kind=window_kind,
                    window_start=start,
                    window_end=end,
                    budget_limit_usd=budget_limit_usd,
                    observed_cost_usd=observed,
                    reserved_cost_usd=reserved,
                    requested_cost_usd=reserved_cost_usd,
                    now=now,
                )
                return CompanyBudgetAdmission(
                    False,
                    None,
                    incident,
                    "Admitting this job would exceed the company cost budget.",
                )
            conn.execute(
                """
                INSERT INTO company_budget_leases(
                    job_id, request_id, company_revision, window_kind, window_start, window_end,
                    budget_limit_usd, reserved_cost_usd, status, admitted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?)
                """,
                (
                    job_id,
                    request_id,
                    company_revision,
                    window_kind,
                    start,
                    end,
                    budget_limit_usd,
                    reserved_cost_usd,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM company_budget_leases WHERE job_id = ?", (job_id,)
            ).fetchone()
            assert row is not None
            return CompanyBudgetAdmission(True, self._company_budget_lease(row), None)

    def settle_company_budget_job(self, lease, *, actual_cost_usd: float):
        """Replace an active conservative reservation with observed cost once."""

        from .company_budget import CompanyBudgetSettlement

        if not math.isfinite(actual_cost_usd) or actual_cost_usd < 0:
            raise ValueError("Company budget settlement cost must be finite and non-negative")
        now = utc_now().isoformat()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM company_budget_leases WHERE job_id = ?", (lease.job_id,)
            ).fetchone()
            if row is None:
                raise ValueError("Company budget lease does not exist")
            self._validate_company_budget_lease_identity(row, lease)
            if str(row["status"]) == "SETTLED":
                forfeited = conn.execute(
                    "SELECT 1 FROM company_budget_forfeits WHERE job_id = ?",
                    (lease.job_id,),
                ).fetchone()
                if forfeited is not None:
                    raise ValueError("Company budget lease was forfeited without observed usage")
                existing_actual = float(row["actual_cost_usd"])
                if abs(existing_actual - actual_cost_usd) > 1e-12:
                    raise ValueError("Company budget lease already settled with another cost")
                incident = self._open_company_budget_incident(conn)
                return CompanyBudgetSettlement(
                    lease, actual_cost_usd, None if incident is None else self._company_budget_incident(incident)
                )
            conn.execute(
                """
                UPDATE company_budget_leases
                SET status = 'SETTLED', actual_cost_usd = ?, settled_at = ?
                WHERE job_id = ? AND status = 'ACTIVE'
                """,
                (actual_cost_usd, now, lease.job_id),
            )
            start = str(row["window_start"])
            end = str(row["window_end"])
            observed, reserved = self._company_budget_totals(
                conn, window_start=start, window_end=end
            )
            incident = None
            if observed >= float(row["budget_limit_usd"]) - 1e-12:
                incident = self._create_company_budget_incident(
                    conn,
                    company_revision=int(row["company_revision"]),
                    window_kind=str(row["window_kind"]),
                    window_start=start,
                    window_end=end,
                    budget_limit_usd=float(row["budget_limit_usd"]),
                    observed_cost_usd=observed,
                    reserved_cost_usd=reserved,
                    requested_cost_usd=0.0,
                    now=now,
                )
            return CompanyBudgetSettlement(lease, actual_cost_usd, incident)

    def forfeit_company_budget_job(self, lease, *, reason: str):
        """Terminalize an indeterminate lease at its full reserved ceiling.

        Cancellation and unexpected boundary failures may prevent the runtime
        from receiving trustworthy observed usage.  Releasing such a lease at
        zero would fail open.  Instead this transaction replaces ``ACTIVE``
        with a conservative ``SETTLED`` charge and appends an immutable
        forfeiture record.  Repeating the exact forfeiture is idempotent.
        """

        from .company_budget import CompanyBudgetForfeit

        normalized_reason = reason.strip()
        if (
            not normalized_reason
            or normalized_reason != reason
            or len(normalized_reason) > 64
            or normalized_reason.upper() != normalized_reason
            or not normalized_reason.replace("_", "").isalnum()
        ):
            raise ValueError("Company budget forfeiture requires a stable reason code")
        now = utc_now().isoformat()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM company_budget_leases WHERE job_id = ?", (lease.job_id,)
            ).fetchone()
            if row is None:
                raise ValueError("Company budget lease does not exist")
            self._validate_company_budget_lease_identity(row, lease)
            existing = conn.execute(
                "SELECT * FROM company_budget_forfeits WHERE job_id = ?", (lease.job_id,)
            ).fetchone()
            if str(row["status"]) == "SETTLED":
                if existing is None:
                    raise ValueError("Company budget lease already settled with observed cost")
                if str(existing["reason"]) != normalized_reason:
                    raise ValueError("Company budget lease already forfeited for another reason")
                incident = self._open_company_budget_incident(conn)
                return CompanyBudgetForfeit(
                    lease=lease,
                    charged_cost_usd=float(existing["charged_cost_usd"]),
                    reason=str(existing["reason"]),
                    forfeited_at=str(existing["forfeited_at"]),
                    incident=(
                        None
                        if incident is None
                        else self._company_budget_incident(incident)
                    ),
                )
            if existing is not None:
                raise RuntimeError("Active company budget lease has a terminal forfeiture")

            charged_cost_usd = float(row["reserved_cost_usd"])
            updated = conn.execute(
                """
                UPDATE company_budget_leases
                SET status = 'SETTLED', actual_cost_usd = ?, settled_at = ?
                WHERE job_id = ? AND status = 'ACTIVE'
                """,
                (charged_cost_usd, now, lease.job_id),
            )
            if updated.rowcount != 1:
                raise RuntimeError("Company budget lease forfeiture lost its active lease")
            conn.execute(
                """
                INSERT INTO company_budget_forfeits(
                    job_id, request_id, company_revision, charged_cost_usd,
                    reason, forfeited_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    lease.job_id,
                    lease.request_id,
                    lease.company_revision,
                    charged_cost_usd,
                    normalized_reason,
                    now,
                ),
            )
            start = str(row["window_start"])
            end = str(row["window_end"])
            observed, reserved = self._company_budget_totals(
                conn, window_start=start, window_end=end
            )
            incident = None
            if observed >= float(row["budget_limit_usd"]) - 1e-12:
                incident = self._create_company_budget_incident(
                    conn,
                    company_revision=int(row["company_revision"]),
                    window_kind=str(row["window_kind"]),
                    window_start=start,
                    window_end=end,
                    budget_limit_usd=float(row["budget_limit_usd"]),
                    observed_cost_usd=observed,
                    reserved_cost_usd=reserved,
                    requested_cost_usd=0.0,
                    now=now,
                )
            return CompanyBudgetForfeit(
                lease=lease,
                charged_cost_usd=charged_cost_usd,
                reason=normalized_reason,
                forfeited_at=now,
                incident=incident,
            )


