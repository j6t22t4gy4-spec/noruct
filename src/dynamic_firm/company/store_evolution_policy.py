"""Versioned COMPANY evolution and retention-policy lifecycle."""

from __future__ import annotations

import json
import uuid

from dynamic_firm.runtime.models import utc_now

from .models import CompanyVersion, EvolutionAutonomyMode, RetentionReviewMode, canonical_json


class CompanyEvolutionPolicyMixin:
    """Use the owner Store's Company revision, lock and transaction only."""

    def retention_review_mode(self) -> RetentionReviewMode:
        raw = self.company().policies.get("roster_retention_review_mode", RetentionReviewMode.APPROVAL.value)
        try:
            return RetentionReviewMode(str(raw))
        except ValueError as exc:
            raise RuntimeError(f"Invalid retention review mode in COMPANY: {raw}") from exc

    def evolution_autonomy_mode(self) -> EvolutionAutonomyMode:
        raw = self.company().policies.get("evolution_autonomy_mode", EvolutionAutonomyMode.PROPOSE.value)
        try:
            return EvolutionAutonomyMode(str(raw))
        except ValueError as exc:
            raise RuntimeError(f"Invalid evolution autonomy mode in COMPANY: {raw}") from exc

    def _policy_row(self, conn):
        active = self._active_revision("active_company_revision", conn)
        row = conn.execute("SELECT * FROM company_versions WHERE revision = ?", (active,)).fetchone()
        assert row is not None
        policies = json.loads(row["policies_json"])
        if not isinstance(policies, dict):
            raise RuntimeError("COMPANY policies must be an object")
        return active, row, policies

    @staticmethod
    def _version_from_row(active, row, policies):
        return CompanyVersion(revision=active, parent_revision=row["parent_revision"], purpose=str(row["purpose"]), policies=policies, created_at=str(row["created_at"]))

    def _commit_policy(self, conn, *, active, row, policies, name, previous, next_value, actor):
        revision = int(conn.execute("SELECT COALESCE(MAX(revision), 0) + 1 AS revision FROM company_versions").fetchone()["revision"])
        now = utc_now().isoformat()
        conn.execute("INSERT INTO company_versions(revision, parent_revision, purpose, policies_json, created_at) VALUES(?, ?, ?, ?, ?)", (revision, active, str(row["purpose"]), canonical_json(policies), now))
        conn.execute("UPDATE company_state_meta SET value = ? WHERE key = 'active_company_revision'", (str(revision),))
        conn.execute("INSERT INTO company_policy_events(event_id, company_revision, policy_name, previous_value_json, new_value_json, actor, occurred_at) VALUES(?, ?, ?, ?, ?, ?, ?)", (str(uuid.uuid4()), revision, name, canonical_json(previous), canonical_json(next_value), actor.strip(), now))

    def set_evolution_autonomy_mode(self, mode: EvolutionAutonomyMode, *, actor: str) -> tuple[CompanyVersion, bool]:
        if not actor.strip():
            raise ValueError("Company evolution policy actor must be explicit")
        if not isinstance(mode, EvolutionAutonomyMode):
            mode = EvolutionAutonomyMode(mode)
        with self._transaction() as conn:
            active, row, policies = self._policy_row(conn)
            previous = EvolutionAutonomyMode(str(policies.get("evolution_autonomy_mode", EvolutionAutonomyMode.PROPOSE.value)))
            if previous == mode:
                return self._version_from_row(active, row, policies), False
            policies["evolution_autonomy_mode"] = mode.value
            policies["roster_retention_review_mode"] = RetentionReviewMode.ALWAYS_APPROVE.value if mode == EvolutionAutonomyMode.ALWAYS_APPROVE else RetentionReviewMode.APPROVAL.value
            policies["automatic_patch_apply"] = mode == EvolutionAutonomyMode.ALWAYS_APPROVE
            policies["background_curator"] = mode == EvolutionAutonomyMode.ALWAYS_APPROVE
            self._commit_policy(conn, active=active, row=row, policies=policies, name="evolution_autonomy_mode", previous=previous.value, next_value=mode.value, actor=actor)
        return self.company(), True

    def set_retention_review_mode(self, mode: RetentionReviewMode, *, actor: str) -> tuple[CompanyVersion, bool]:
        if not actor.strip():
            raise ValueError("Company review policy actor must be explicit")
        if not isinstance(mode, RetentionReviewMode):
            mode = RetentionReviewMode(mode)
        with self._transaction() as conn:
            active, row, policies = self._policy_row(conn)
            previous = RetentionReviewMode(str(policies.get("roster_retention_review_mode", RetentionReviewMode.APPROVAL.value)))
            if previous == mode:
                return self._version_from_row(active, row, policies), False
            policies["roster_retention_review_mode"] = mode.value
            self._commit_policy(conn, active=active, row=row, policies=policies, name="roster_retention_review_mode", previous=previous.value, next_value=mode.value, actor=actor)
        return self.company(), True
