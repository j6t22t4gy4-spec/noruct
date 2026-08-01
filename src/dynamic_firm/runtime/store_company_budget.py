"""Company budget operator projections composed into the canonical Run Store.

Job reservation, charge, settlement and forfeit remain on ``RunStore``.  This
component only exposes the bounded status view and an explicit incident
resolution operation through the same connection and transaction owner.
"""

from __future__ import annotations

from datetime import datetime


class RunStoreCompanyBudgetMixin:
    def company_budget_status(self, policy) -> dict[str, object]:
        from .company_budget import CompanyCostBudgetPolicy
        from dynamic_firm._vendor.paperclip_runtime.budget import (
            budget_status_from_observed,
            resolve_budget_window,
        )

        if not isinstance(policy, CompanyCostBudgetPolicy):
            raise ValueError("Company budget status requires a validated policy")
        with self._lock:
            paused = self._conn.execute(
                "SELECT incident_id, paused_at FROM company_budget_pause_state WHERE scope = 'company'"
            ).fetchone()
            incident = None
            if paused is not None:
                incident = self._conn.execute(
                    "SELECT * FROM company_budget_incidents WHERE incident_id = ?",
                    (str(paused["incident_id"]),),
                ).fetchone()
            if not policy.enabled:
                return {
                    "enabled": False,
                    "paused": paused is not None,
                    "policy": policy.as_mapping(),
                    "incident": None if incident is None else self._company_budget_incident(incident),
                }
            start, end = resolve_budget_window(policy.window_kind, self._utc_now())
            observed, reserved = self._company_budget_totals(
                self._conn, window_start=start.isoformat(), window_end=end.isoformat()
            )
        return {
            "enabled": True,
            "paused": paused is not None,
            "policy": policy.as_mapping(),
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
            "observed_cost_usd": observed,
            "reserved_cost_usd": reserved,
            "remaining_cost_usd": max(0.0, policy.max_total_cost_usd - observed - reserved),
            "status": budget_status_from_observed(observed, policy.max_total_cost_usd, warn_percent=80),
            "incident": None if incident is None else self._company_budget_incident(incident),
        }

    @staticmethod
    def _utc_now():
        from .models import utc_now

        return utc_now()

    def resolve_company_budget_incident(self, incident_id: str, *, actor: str, policy, resolved_at: datetime):
        from .company_budget import CompanyCostBudgetPolicy

        if not incident_id.strip() or not actor.strip() or not isinstance(policy, CompanyCostBudgetPolicy):
            raise ValueError("Company budget incident resolution input is invalid")
        if resolved_at.tzinfo is None:
            raise ValueError("Company budget resolution time must be timezone-aware")
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM company_budget_incidents WHERE incident_id = ?", (incident_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Company budget incident not found: {incident_id}")
            if str(row["status"]) == "RESOLVED":
                return self._company_budget_incident(row)
            if policy.window_kind != str(row["window_kind"]):
                raise ValueError("Company budget window kind must match the paused incident")
            observed, reserved = self._company_budget_totals(
                conn, window_start=str(row["window_start"]), window_end=str(row["window_end"])
            )
            if policy.max_total_cost_usd + 1e-12 < observed + reserved:
                raise ValueError("Raised company budget is still below observed and reserved cost")
            now = resolved_at.isoformat()
            conn.execute(
                "UPDATE company_budget_incidents SET status = 'RESOLVED', resolved_at = ?, resolved_by = ? WHERE incident_id = ? AND status = 'OPEN'",
                (now, actor, incident_id),
            )
            conn.execute(
                "DELETE FROM company_budget_pause_state WHERE scope = 'company' AND incident_id = ?",
                (incident_id,),
            )
            updated = conn.execute(
                "SELECT * FROM company_budget_incidents WHERE incident_id = ?", (incident_id,)
            ).fetchone()
            assert updated is not None
            return self._company_budget_incident(updated)
