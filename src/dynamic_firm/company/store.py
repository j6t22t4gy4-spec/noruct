from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from dynamic_firm.runtime.models import VersionedContent, to_primitive, utc_now

from .store_read import CompanyReadProjectionMixin
from .store_episodes import CompanyEpisodeAuditMixin
from .store_budget_policy import CompanyBudgetPolicyMixin
from .store_evolution_policy import CompanyEvolutionPolicyMixin
from .store_live_evidence import CompanyLiveEvidenceMixin
from .store_staffing_demand import CompanyStaffingDemandMixin
from .store_employee_skill_catalog import CompanyEmployeeSkillCatalogMixin
from .store_employee_skill_lifecycle import CompanyEmployeeSkillLifecycleMixin
from .store_employee_skill_observation import CompanyEmployeeSkillObservationMixin
from .store_hire_observation import CompanyHireObservationMixin
from .store_roster_patch import CompanyRosterPatchMixin
from .store_workflow_patch import CompanyWorkflowPatchMixin

from .models import (
    CompanyStateSummary,
    CompanyVersion,
    EmployeeSkillAssessment,
    EmployeeSkillAssessmentDecision,
    EmployeeSkillEvidence,
    EmployeeSkillEvidenceKind,
    EmployeeSkillObservation,
    EmployeeSkillObservationContract,
    EmployeeSkillPatchCandidate,
    EmployeeSkillPatchEvent,
    EmployeeSkillPatchEventType,
    EmployeeSkillPatchStatus,
    EmployeeSkillVersion,
    EvidenceSource,
    HireAssessment,
    HireAssessmentDecision,
    HireObservation,
    HireObservationContract,
    OrganizationEpisode,
    PlaybookVersion,
    RosterPatchCandidate,
    RosterPatchEvent,
    RosterPatchEventType,
    RosterPatchOperation,
    RosterPatchStatus,
    RosterRetentionReview,
    RetentionReviewMode,
    EvolutionAutonomyMode,
    RosterVersion,
    StaffingDemandEvidence,
    WorkflowPatchCandidate,
    WorkflowPatchAssessment,
    WorkflowPatchEvent,
    WorkflowPatchEventType,
    WorkflowPatchObservation,
    WorkflowPatchObservationContract,
    WorkflowPatchStatus,
    canonical_json,
    content_digest,
    employee_skill_assessment_from_dict,
    employee_skill_evidence_from_dict,
    employee_skill_observation_contract_from_dict,
    employee_skill_observation_from_dict,
    employee_skill_patch_from_dict,
    employee_skill_version_from_dict,
    hire_assessment_from_dict,
    hire_observation_contract_from_dict,
    hire_observation_from_dict,
    organization_episode_from_dict,
    roster_patch_from_dict,
    roster_retention_review_from_dict,
    staffing_demand_from_dict,
    workflow_patch_assessment_from_dict,
    workflow_patch_from_dict,
    workflow_patch_observation_contract_from_dict,
    workflow_patch_observation_from_dict,
    workflow_pattern_from_dict,
)


COMPANY_STATE_SCHEMA_VERSION = 9
_LIVE_PAIR_IDENTITY_FIELDS = (
    "campaign_id",
    "baseline_run_id",
    "dynamic_run_id",
    "baseline_evidence_id",
    "dynamic_evidence_id",
    "baseline_content_hash",
    "dynamic_content_hash",
    "source_revision",
    "fixture",
    "provider_kind",
    "model_id",
    "baseline_quality_score",
    "dynamic_quality_score",
    "baseline_model_calls",
    "dynamic_model_calls",
)
DEFAULT_COMPANY_PURPOSE = "Complete user goals through the smallest sufficient AI company."
DEFAULT_COMPANY_POLICIES: Mapping[str, Any] = {
    "automatic_patch_apply": False,
    "background_curator": False,
    "evolution_autonomy_mode": EvolutionAutonomyMode.PROPOSE.value,
    "high_cost_or_irreversible_requires_user_approval": True,
    "runtime_dependencies": 0,
    "roster_retention_review_mode": RetentionReviewMode.APPROVAL.value,
    "company_cost_budget": {
        "max_total_cost_usd": 0.0,
        "window_kind": "lifetime",
    },
}


def _loads(raw: str) -> Any:
    return json.loads(raw)


class CompanyStateStore(
    CompanyStaffingDemandMixin,
    CompanyWorkflowPatchMixin,
    CompanyHireObservationMixin,
    CompanyRosterPatchMixin,
    CompanyEmployeeSkillObservationMixin,
    CompanyEmployeeSkillLifecycleMixin,
    CompanyEmployeeSkillCatalogMixin,
    CompanyLiveEvidenceMixin,
    CompanyEvolutionPolicyMixin,
    CompanyBudgetPolicyMixin,
    CompanyReadProjectionMixin,
    CompanyEpisodeAuditMixin,
):
    """Versioned company state and append-only organization-learning evidence."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._initialize_schema()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> CompanyStateStore:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
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

    def _migrate_live_evidence_v3(self) -> None:
        rows = self._conn.execute(
            "SELECT * FROM verified_live_evidence_pairs ORDER BY imported_at, pair_id"
        ).fetchall()
        self._conn.execute(
            """
            CREATE TABLE verified_live_evidence_pairs_v4 (
                pair_id TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL,
                baseline_run_id TEXT NOT NULL UNIQUE,
                dynamic_run_id TEXT NOT NULL UNIQUE,
                baseline_evidence_id TEXT NOT NULL UNIQUE,
                dynamic_evidence_id TEXT NOT NULL UNIQUE,
                baseline_content_hash TEXT NOT NULL UNIQUE,
                dynamic_content_hash TEXT NOT NULL UNIQUE,
                source_revision TEXT NOT NULL,
                fixture TEXT NOT NULL,
                provider_kind TEXT NOT NULL,
                model_id TEXT NOT NULL,
                episode_id TEXT NOT NULL UNIQUE
                    REFERENCES organization_episodes(episode_id),
                payload_json TEXT NOT NULL,
                content_hash TEXT NOT NULL UNIQUE,
                imported_at TEXT NOT NULL,
                CHECK(baseline_run_id <> dynamic_run_id)
            )
            """
        )
        for row in rows:
            payload = _loads(row["payload_json"])
            legacy_campaign = content_digest(
                {
                    "contract": "noruct.legacy-live-campaign.v1",
                    "source_revision": row["source_revision"],
                    "fixture": row["fixture"],
                    "provider_kind": row["provider_kind"],
                    "model_id": row["model_id"],
                }
            )
            payload.update(
                {
                    "campaign_id": f"legacy-campaign-{legacy_campaign[:24]}",
                    "baseline_run_id": f"legacy-{row['baseline_evidence_id']}",
                    "dynamic_run_id": f"legacy-{row['dynamic_evidence_id']}",
                }
            )
            identity = {key: payload[key] for key in _LIVE_PAIR_IDENTITY_FIELDS}
            migrated_hash = content_digest(identity)
            payload["content_hash"] = migrated_hash
            self._conn.execute(
                """
                INSERT INTO verified_live_evidence_pairs_v4(
                    pair_id, campaign_id, baseline_run_id, dynamic_run_id,
                    baseline_evidence_id, dynamic_evidence_id,
                    baseline_content_hash, dynamic_content_hash, source_revision,
                    fixture, provider_kind, model_id, episode_id, payload_json,
                    content_hash, imported_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["pair_id"],
                    payload["campaign_id"],
                    payload["baseline_run_id"],
                    payload["dynamic_run_id"],
                    row["baseline_evidence_id"],
                    row["dynamic_evidence_id"],
                    row["baseline_content_hash"],
                    row["dynamic_content_hash"],
                    row["source_revision"],
                    row["fixture"],
                    row["provider_kind"],
                    row["model_id"],
                    row["episode_id"],
                    canonical_json(payload),
                    migrated_hash,
                    row["imported_at"],
                ),
            )
        self._conn.execute("DROP TABLE verified_live_evidence_pairs")
        self._conn.execute(
            "ALTER TABLE verified_live_evidence_pairs_v4 RENAME TO verified_live_evidence_pairs"
        )

    def _initialize_schema(self) -> None:
        now = utc_now().isoformat()
        with self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS company_state_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS company_versions (
                    revision INTEGER PRIMARY KEY,
                    parent_revision INTEGER,
                    purpose TEXT NOT NULL,
                    policies_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS company_policy_events (
                    event_id TEXT PRIMARY KEY,
                    company_revision INTEGER NOT NULL
                        REFERENCES company_versions(revision),
                    policy_name TEXT NOT NULL,
                    previous_value_json TEXT NOT NULL,
                    new_value_json TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    UNIQUE(company_revision, policy_name)
                );

                CREATE TABLE IF NOT EXISTS roster_versions (
                    revision INTEGER PRIMARY KEY,
                    parent_revision INTEGER,
                    employees_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS playbook_versions (
                    revision INTEGER PRIMARY KEY,
                    parent_revision INTEGER,
                    patterns_json TEXT NOT NULL,
                    source_patch_id TEXT,
                    rolled_back_from_revision INTEGER,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS organization_episodes (
                    episode_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL UNIQUE,
                    source_kind TEXT NOT NULL,
                    task_family TEXT NOT NULL,
                    context_fingerprint TEXT NOT NULL,
                    execution_profile TEXT NOT NULL,
                    plan_digest TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS organization_episode_group_idx
                    ON organization_episodes(
                        task_family, context_fingerprint, execution_profile, plan_digest
                    );

                CREATE TABLE IF NOT EXISTS workflow_patch_candidates (
                    patch_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    base_playbook_revision INTEGER NOT NULL,
                    pattern_id TEXT NOT NULL,
                    task_family TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL UNIQUE,
                    eligible_for_apply INTEGER NOT NULL,
                    applied_revision INTEGER,
                    rolled_back_revision INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS workflow_patch_status_idx
                    ON workflow_patch_candidates(status, created_at);

                CREATE TABLE IF NOT EXISTS workflow_patch_events (
                    event_id TEXT PRIMARY KEY,
                    patch_id TEXT NOT NULL REFERENCES workflow_patch_candidates(patch_id),
                    seq INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    UNIQUE(patch_id, seq)
                );

                CREATE TABLE IF NOT EXISTS roster_patch_candidates (
                    patch_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    operation_type TEXT NOT NULL,
                    base_roster_revision INTEGER NOT NULL,
                    employee_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL UNIQUE,
                    applied_revision INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS roster_patch_status_idx
                    ON roster_patch_candidates(status, created_at);

                CREATE TABLE IF NOT EXISTS roster_patch_events (
                    event_id TEXT PRIMARY KEY,
                    patch_id TEXT NOT NULL REFERENCES roster_patch_candidates(patch_id),
                    seq INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    UNIQUE(patch_id, seq)
                );

                CREATE TABLE IF NOT EXISTS staffing_demand_evidence (
                    evidence_id TEXT PRIMARY KEY,
                    episode_id TEXT NOT NULL REFERENCES organization_episodes(episode_id),
                    job_id TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    context_fingerprint TEXT NOT NULL,
                    execution_profile TEXT NOT NULL,
                    base_roster_revision INTEGER NOT NULL,
                    capability TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL UNIQUE,
                    recorded_at TEXT NOT NULL,
                    UNIQUE(episode_id, capability)
                );

                CREATE INDEX IF NOT EXISTS staffing_demand_group_idx
                    ON staffing_demand_evidence(
                        context_fingerprint, capability, source_kind, recorded_at
                    );

                CREATE TABLE IF NOT EXISTS roster_patch_staffing_evidence (
                    patch_id TEXT NOT NULL REFERENCES roster_patch_candidates(patch_id),
                    evidence_id TEXT NOT NULL REFERENCES staffing_demand_evidence(evidence_id),
                    PRIMARY KEY(patch_id, evidence_id)
                );

                CREATE TABLE IF NOT EXISTS hire_observation_contracts (
                    patch_id TEXT PRIMARY KEY
                        REFERENCES roster_patch_candidates(patch_id),
                    applied_roster_revision INTEGER NOT NULL,
                    employee_id TEXT NOT NULL,
                    capability TEXT NOT NULL,
                    context_fingerprint TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS hire_observations (
                    observation_id TEXT PRIMARY KEY,
                    patch_id TEXT NOT NULL
                        REFERENCES hire_observation_contracts(patch_id),
                    episode_id TEXT NOT NULL REFERENCES organization_episodes(episode_id),
                    job_id TEXT NOT NULL,
                    attribution_eligible INTEGER NOT NULL,
                    cohort_eligible INTEGER NOT NULL,
                    persistent_employee_assigned INTEGER NOT NULL,
                    temporary_fallback_used INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    UNIQUE(patch_id, episode_id),
                    UNIQUE(patch_id, job_id),
                    UNIQUE(patch_id, content_hash)
                );

                CREATE INDEX IF NOT EXISTS hire_observation_cohort_idx
                    ON hire_observations(
                        patch_id, attribution_eligible, cohort_eligible, recorded_at
                    );

                CREATE TABLE IF NOT EXISTS hire_assessments (
                    assessment_id TEXT PRIMARY KEY,
                    patch_id TEXT NOT NULL
                        REFERENCES hire_observation_contracts(patch_id),
                    seq INTEGER NOT NULL,
                    decision TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    assessed_at TEXT NOT NULL,
                    UNIQUE(patch_id, seq),
                    UNIQUE(patch_id, content_hash)
                );

                CREATE TABLE IF NOT EXISTS roster_patch_hire_assessments (
                    patch_id TEXT NOT NULL
                        REFERENCES roster_patch_candidates(patch_id),
                    assessment_id TEXT NOT NULL
                        REFERENCES hire_assessments(assessment_id),
                    PRIMARY KEY(patch_id, assessment_id)
                );

                CREATE TABLE IF NOT EXISTS roster_retention_reviews (
                    review_id TEXT PRIMARY KEY,
                    roster_patch_id TEXT NOT NULL
                        REFERENCES roster_patch_candidates(patch_id),
                    hire_patch_id TEXT NOT NULL
                        REFERENCES hire_observation_contracts(patch_id),
                    assessment_id TEXT NOT NULL
                        REFERENCES hire_assessments(assessment_id),
                    company_revision INTEGER NOT NULL
                        REFERENCES company_versions(revision),
                    mode TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL UNIQUE,
                    reviewed_at TEXT NOT NULL,
                    UNIQUE(roster_patch_id, company_revision, mode, assessment_id)
                );

                CREATE TABLE IF NOT EXISTS employee_skill_evidence (
                    evidence_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    source_ref TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    employee_id TEXT NOT NULL,
                    skill_key TEXT NOT NULL,
                    context_key TEXT NOT NULL,
                    procedure_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL UNIQUE,
                    recorded_at TEXT NOT NULL,
                    UNIQUE(kind, source_ref, employee_id, skill_key, context_key)
                );

                CREATE INDEX IF NOT EXISTS employee_skill_evidence_group_idx
                    ON employee_skill_evidence(
                        employee_id, skill_key, context_key, procedure_hash, recorded_at
                    );

                CREATE TABLE IF NOT EXISTS employee_skill_patch_candidates (
                    patch_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    employee_id TEXT NOT NULL,
                    skill_key TEXT NOT NULL,
                    context_key TEXT NOT NULL,
                    base_skill_revision INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL UNIQUE,
                    applied_skill_revision INTEGER,
                    rolled_back_skill_revision INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS employee_skill_patch_status_idx
                    ON employee_skill_patch_candidates(status, created_at);

                CREATE TABLE IF NOT EXISTS employee_skill_patch_evidence (
                    patch_id TEXT NOT NULL
                        REFERENCES employee_skill_patch_candidates(patch_id),
                    evidence_id TEXT NOT NULL
                        REFERENCES employee_skill_evidence(evidence_id),
                    PRIMARY KEY(patch_id, evidence_id)
                );

                CREATE TABLE IF NOT EXISTS employee_skill_patch_events (
                    event_id TEXT PRIMARY KEY,
                    patch_id TEXT NOT NULL
                        REFERENCES employee_skill_patch_candidates(patch_id),
                    seq INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    UNIQUE(patch_id, seq)
                );

                CREATE TABLE IF NOT EXISTS employee_skill_versions (
                    version_id TEXT PRIMARY KEY,
                    employee_id TEXT NOT NULL,
                    skill_key TEXT NOT NULL,
                    context_key TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    active INTEGER NOT NULL,
                    source_patch_id TEXT NOT NULL
                        REFERENCES employee_skill_patch_candidates(patch_id),
                    payload_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(employee_id, skill_key, context_key, revision)
                );

                CREATE TABLE IF NOT EXISTS employee_skill_heads (
                    employee_id TEXT NOT NULL,
                    skill_key TEXT NOT NULL,
                    context_key TEXT NOT NULL,
                    current_version_id TEXT NOT NULL
                        REFERENCES employee_skill_versions(version_id),
                    PRIMARY KEY(employee_id, skill_key, context_key)
                );

                CREATE TABLE IF NOT EXISTS employee_skill_observation_contracts (
                    patch_id TEXT PRIMARY KEY
                        REFERENCES employee_skill_patch_candidates(patch_id),
                    employee_id TEXT NOT NULL,
                    skill_key TEXT NOT NULL,
                    context_key TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS employee_skill_observations (
                    observation_id TEXT PRIMARY KEY,
                    patch_id TEXT NOT NULL
                        REFERENCES employee_skill_observation_contracts(patch_id),
                    episode_id TEXT NOT NULL
                        REFERENCES organization_episodes(episode_id),
                    job_id TEXT NOT NULL,
                    attribution_eligible INTEGER NOT NULL,
                    cohort_eligible INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    UNIQUE(patch_id, episode_id),
                    UNIQUE(patch_id, content_hash)
                );

                CREATE TABLE IF NOT EXISTS employee_skill_assessments (
                    assessment_id TEXT PRIMARY KEY,
                    patch_id TEXT NOT NULL
                        REFERENCES employee_skill_observation_contracts(patch_id),
                    seq INTEGER NOT NULL,
                    decision TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    assessed_at TEXT NOT NULL,
                    UNIQUE(patch_id, seq),
                    UNIQUE(patch_id, content_hash)
                );
                """
            )
            row = self._conn.execute(
                "SELECT value FROM company_state_meta WHERE key = 'schema_version'"
            ).fetchone()
            existing_version = int(row["value"]) if row else None
            if existing_version not in {
                None,
                1,
                2,
                3,
                4,
                5,
                6,
                7,
                8,
                COMPANY_STATE_SCHEMA_VERSION,
            }:
                raise RuntimeError(
                    f"Unsupported company state schema version: {row['value']}"
                )
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS workflow_patch_observation_contracts (
                    patch_id TEXT PRIMARY KEY
                        REFERENCES workflow_patch_candidates(patch_id),
                    pattern_id TEXT NOT NULL,
                    context_fingerprint TEXT NOT NULL,
                    execution_profile TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS workflow_patch_observations (
                    observation_id TEXT PRIMARY KEY,
                    patch_id TEXT NOT NULL
                        REFERENCES workflow_patch_observation_contracts(patch_id),
                    episode_id TEXT NOT NULL REFERENCES organization_episodes(episode_id),
                    attribution_eligible INTEGER NOT NULL,
                    cohort_eligible INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    UNIQUE(patch_id, episode_id),
                    UNIQUE(patch_id, content_hash)
                );

                CREATE INDEX IF NOT EXISTS workflow_patch_observation_cohort_idx
                    ON workflow_patch_observations(
                        patch_id, attribution_eligible, cohort_eligible, recorded_at
                    );

                CREATE TABLE IF NOT EXISTS workflow_patch_assessments (
                    assessment_id TEXT PRIMARY KEY,
                    patch_id TEXT NOT NULL
                        REFERENCES workflow_patch_observation_contracts(patch_id),
                    seq INTEGER NOT NULL,
                    decision TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    assessed_at TEXT NOT NULL,
                    UNIQUE(patch_id, seq),
                    UNIQUE(patch_id, content_hash)
                );

                CREATE TABLE IF NOT EXISTS verified_live_evidence_pairs (
                    pair_id TEXT PRIMARY KEY,
                    campaign_id TEXT NOT NULL,
                    baseline_run_id TEXT NOT NULL UNIQUE,
                    dynamic_run_id TEXT NOT NULL UNIQUE,
                    baseline_evidence_id TEXT NOT NULL UNIQUE,
                    dynamic_evidence_id TEXT NOT NULL UNIQUE,
                    baseline_content_hash TEXT NOT NULL UNIQUE,
                    dynamic_content_hash TEXT NOT NULL UNIQUE,
                    source_revision TEXT NOT NULL,
                    fixture TEXT NOT NULL,
                    provider_kind TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    episode_id TEXT NOT NULL UNIQUE
                        REFERENCES organization_episodes(episode_id),
                    payload_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL UNIQUE,
                    imported_at TEXT NOT NULL,
                    CHECK(baseline_run_id <> dynamic_run_id)
                );
                """
            )
            if existing_version == 3:
                self._migrate_live_evidence_v3()
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS verified_live_evidence_campaign_idx
                ON verified_live_evidence_pairs(
                    campaign_id, source_revision, fixture, provider_kind, model_id, imported_at
                )
                """
            )
            self._conn.execute(
                """
                INSERT INTO company_state_meta(key, value) VALUES('schema_version', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(COMPANY_STATE_SCHEMA_VERSION),),
            )
            self._conn.execute(
                """
                INSERT OR IGNORE INTO company_versions(
                    revision, parent_revision, purpose, policies_json, created_at
                ) VALUES(1, NULL, ?, ?, ?)
                """,
                (DEFAULT_COMPANY_PURPOSE, canonical_json(DEFAULT_COMPANY_POLICIES), now),
            )
            self._conn.execute(
                """
                INSERT OR IGNORE INTO roster_versions(
                    revision, parent_revision, employees_json, created_at
                ) VALUES(1, NULL, '[]', ?)
                """,
                (now,),
            )
            self._conn.execute(
                """
                INSERT OR IGNORE INTO playbook_versions(
                    revision, parent_revision, patterns_json, source_patch_id,
                    rolled_back_from_revision, created_at
                ) VALUES(1, NULL, '[]', NULL, NULL, ?)
                """,
                (now,),
            )
            for key in ("active_company_revision", "active_roster_revision", "active_playbook_revision"):
                self._conn.execute(
                    "INSERT OR IGNORE INTO company_state_meta(key, value) VALUES(?, '1')",
                    (key,),
                )
            if existing_version == 1:
                rows = self._conn.execute(
                    """
                    SELECT payload_json FROM workflow_patch_candidates
                    WHERE status = ?
                    """,
                    (WorkflowPatchStatus.APPLIED.value,),
                ).fetchall()
                for applied in rows:
                    candidate = workflow_patch_from_dict(_loads(applied["payload_json"]))
                    contract = WorkflowPatchObservationContract.create(
                        candidate,
                        created_at=candidate.updated_at,
                    )
                    self._insert_observation_contract(self._conn, contract)
            if existing_version is not None and existing_version <= 6:
                rows = self._conn.execute(
                    """
                    SELECT payload_json FROM roster_patch_candidates
                    WHERE status = ? ORDER BY created_at, patch_id
                    """,
                    (RosterPatchStatus.APPLIED.value,),
                ).fetchall()
                for applied in rows:
                    candidate = roster_patch_from_dict(_loads(applied["payload_json"]))
                    self._validate_roster_patch_content(candidate)
                    if not candidate.evidence_ids:
                        continue
                    evidence = self._roster_patch_evidence_in(self._conn, candidate)
                    contract = HireObservationContract.create(
                        candidate,
                        evidence,
                        created_at=candidate.updated_at,
                    )
                    self._insert_hire_observation_contract(self._conn, contract)
