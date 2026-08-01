"""COMPANY cost-policy projection and mutation composed into CompanyStateStore.

The owner Store continues to own the SQLite connection, lock, transaction and
COMPANY revision pointer.  This mixin only isolates one cohesive versioned
policy lifecycle; it is intentionally not an independent budget database.
"""

from __future__ import annotations

import sqlite3
import uuid
from typing import Any, Mapping

from dynamic_firm.runtime.models import utc_now

from .models import CompanyVersion, canonical_json


class CompanyBudgetPolicyMixin:
    """Read and version the Company-wide cost policy through the owner Store."""

    def company_cost_budget_policy(self) -> Mapping[str, Any]:
        from dynamic_firm.runtime.company_budget import (
            COMPANY_COST_BUDGET_POLICY_KEY,
            CompanyCostBudgetPolicy,
        )

        raw = self.company().policies.get(COMPANY_COST_BUDGET_POLICY_KEY)
        return CompanyCostBudgetPolicy.from_mapping(raw).as_mapping()

    def set_company_cost_budget_policy(
        self,
        policy: Mapping[str, Any],
        *,
        actor: str,
    ) -> tuple[CompanyVersion, bool]:
        from dynamic_firm.runtime.company_budget import (
            COMPANY_COST_BUDGET_POLICY_KEY,
            CompanyCostBudgetPolicy,
        )

        if not actor.strip():
            raise ValueError("Company budget policy actor must be explicit")
        normalized = CompanyCostBudgetPolicy.from_mapping(dict(policy)).as_mapping()
        with self._transaction() as conn:
            active = self._active_revision("active_company_revision", conn)
            row = conn.execute(
                "SELECT * FROM company_versions WHERE revision = ?", (active,)
            ).fetchone()
            assert row is not None
            policies = self._loads_company_policy(row["policies_json"])
            previous = CompanyCostBudgetPolicy.from_mapping(
                policies.get(COMPANY_COST_BUDGET_POLICY_KEY)
            ).as_mapping()
            if previous == normalized:
                return (
                    CompanyVersion(
                        revision=active,
                        parent_revision=row["parent_revision"],
                        purpose=str(row["purpose"]),
                        policies=policies,
                        created_at=str(row["created_at"]),
                    ),
                    False,
                )
            policies[COMPANY_COST_BUDGET_POLICY_KEY] = normalized
            revision = int(
                conn.execute(
                    "SELECT COALESCE(MAX(revision), 0) + 1 AS revision FROM company_versions"
                ).fetchone()["revision"]
            )
            now = utc_now().isoformat()
            conn.execute(
                "INSERT INTO company_versions(revision, parent_revision, purpose, policies_json, created_at) VALUES(?, ?, ?, ?, ?)",
                (revision, active, str(row["purpose"]), canonical_json(policies), now),
            )
            conn.execute(
                "UPDATE company_state_meta SET value = ? WHERE key = 'active_company_revision'",
                (str(revision),),
            )
            conn.execute(
                "INSERT INTO company_policy_events(event_id, company_revision, policy_name, previous_value_json, new_value_json, actor, occurred_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    str(uuid.uuid4()),
                    revision,
                    COMPANY_COST_BUDGET_POLICY_KEY,
                    canonical_json(previous),
                    canonical_json(normalized),
                    actor.strip(),
                    now,
                ),
            )
        return self.company(), True

    def list_company_policy_events(self) -> tuple[Mapping[str, Any], ...]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM company_policy_events ORDER BY occurred_at, company_revision, policy_name"
            ).fetchall()
        return tuple(
            {
                "event_id": str(row["event_id"]),
                "company_revision": int(row["company_revision"]),
                "policy_name": str(row["policy_name"]),
                "previous_value": self._loads_company_value(row["previous_value_json"]),
                "new_value": self._loads_company_value(row["new_value_json"]),
                "actor": str(row["actor"]),
                "occurred_at": str(row["occurred_at"]),
            }
            for row in rows
        )

    @staticmethod
    def _loads_company_policy(raw: str) -> dict[str, Any]:
        import json

        value = json.loads(raw)
        if not isinstance(value, dict):
            raise RuntimeError("COMPANY policies must be an object")
        return value

    @staticmethod
    def _loads_company_value(raw: str) -> Any:
        import json

        return json.loads(raw)
