"""Epistemic, oracle, and delayed-outcome contracts for User Knowledge.

These records deliberately contain references and bounded metadata rather than
Knowledge bodies, model transcripts, Job graphs, or tool payloads.  They make
the reason for an execution reproducible without turning Knowledge content into
Company authority.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping


class EpistemicStatus(StrEnum):
    OBSERVED = "OBSERVED"
    INFERRED = "INFERRED"
    ASSUMED = "ASSUMED"
    DECIDED = "DECIDED"
    DISPUTED = "DISPUTED"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"


class ContentTrustClass(StrEnum):
    TRUSTED_SOURCE = "TRUSTED_SOURCE"
    USER_ASSERTED = "USER_ASSERTED"
    UNTRUSTED_EXTERNAL = "UNTRUSTED_EXTERNAL"
    DERIVED = "DERIVED"
    MODEL_GENERATED = "MODEL_GENERATED"
    UNSPECIFIED = "UNSPECIFIED"


class OracleValidatorType(StrEnum):
    DETERMINISTIC = "DETERMINISTIC"
    EXTERNAL_EVIDENCE = "EXTERNAL_EVIDENCE"
    HUMAN_JUDGMENT = "HUMAN_JUDGMENT"
    MODEL_REVIEW = "MODEL_REVIEW"
    UNVERIFIABLE = "UNVERIFIABLE"


class ValidatorIndependence(StrEnum):
    DETERMINISTIC = "DETERMINISTIC"
    INDEPENDENT_SOURCE = "INDEPENDENT_SOURCE"
    INDEPENDENT_MODEL = "INDEPENDENT_MODEL"
    HUMAN = "HUMAN"
    SAME_SOURCE_OR_MODEL = "SAME_SOURCE_OR_MODEL"
    NONE = "NONE"


class OutcomeVerdict(StrEnum):
    NOT_YET_OBSERVED = "NOT_YET_OBSERVED"
    PASSED = "PASSED"
    FAILED = "FAILED"
    INCONCLUSIVE = "INCONCLUSIVE"


class AttributionStatus(StrEnum):
    UNASSESSED = "UNASSESSED"
    INFORMATION_GAP = "INFORMATION_GAP"
    DECISION_ERROR = "DECISION_ERROR"
    EXECUTION_ERROR = "EXECUTION_ERROR"
    ORACLE_GAP = "ORACLE_GAP"
    CONFOUNDED = "CONFOUNDED"
    QUALIFIED = "QUALIFIED"


def canonical_digest(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        dict(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class EpistemicAnnotation:
    subject_type: str
    subject_id: str
    epistemic_status: EpistemicStatus
    trust_class: ContentTrustClass
    freshness_expires_at: str | None
    conflict_refs: tuple[str, ...]
    unknown_refs: tuple[str, ...]
    source_revision: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class DecisionContextSnapshot:
    snapshot_id: str
    binding_id: str
    request_id: str
    job_id: str
    intent_id: str
    intent_revision: int
    intent_hash: str
    decision_id: str | None
    decision_revision: int | None
    evidence_pack_id: str
    evidence_pack_revision: int
    evidence_pack_digest: str
    known_refs: tuple[str, ...]
    unknown_refs: tuple[str, ...]
    assumptions: tuple[str, ...]
    constraints: tuple[str, ...]
    excluded_alternatives: tuple[str, ...]
    owner_ref: str
    authority_ref: str
    supersedes_snapshot_id: str | None
    content_digest: str
    created_at: str

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema": "noruct.decision-context.v1",
            "snapshot_id": self.snapshot_id,
            "binding_id": self.binding_id,
            "request_id": self.request_id,
            "job_id": self.job_id,
            "intent_id": self.intent_id,
            "intent_revision": self.intent_revision,
            "intent_hash": self.intent_hash,
            "decision_id": self.decision_id,
            "decision_revision": self.decision_revision,
            "evidence_pack_id": self.evidence_pack_id,
            "evidence_pack_revision": self.evidence_pack_revision,
            "evidence_pack_digest": self.evidence_pack_digest,
            "known_refs": list(self.known_refs),
            "unknown_refs": list(self.unknown_refs),
            "assumptions": list(self.assumptions),
            "constraints": list(self.constraints),
            "excluded_alternatives": list(self.excluded_alternatives),
            "owner_ref": self.owner_ref,
            "authority_ref": self.authority_ref,
            "supersedes_snapshot_id": self.supersedes_snapshot_id,
            "created_at": self.created_at,
        }

    def verify(self) -> None:
        if canonical_digest(self.canonical_payload()) != self.content_digest:
            raise ValueError("Decision Context Snapshot digest is invalid")


@dataclass(frozen=True, slots=True)
class OracleContract:
    oracle_contract_id: str
    binding_id: str
    request_id: str
    job_id: str
    revision: int
    acceptance_criteria: tuple[str, ...]
    failure_criteria: tuple[str, ...]
    observable_signals: tuple[str, ...]
    observation_channel: str
    validator_type: OracleValidatorType
    independence_class: ValidatorIndependence
    accountable_owner_ref: str
    authority_ref: str
    feedback_due_at: str | None
    reversibility_class: str
    risk_class: str
    proxy_metric: str | None
    proxy_failure_modes: tuple[str, ...]
    inconclusive_policy: str
    max_attempts: int
    max_evidence_items: int
    content_digest: str
    created_at: str

    @property
    def has_executable_oracle(self) -> bool:
        return (
            self.validator_type is not OracleValidatorType.UNVERIFIABLE
            and bool(self.observable_signals)
        )

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema": "noruct.oracle-contract.v1",
            "oracle_contract_id": self.oracle_contract_id,
            "binding_id": self.binding_id,
            "request_id": self.request_id,
            "job_id": self.job_id,
            "revision": self.revision,
            "acceptance_criteria": list(self.acceptance_criteria),
            "failure_criteria": list(self.failure_criteria),
            "observable_signals": list(self.observable_signals),
            "observation_channel": self.observation_channel,
            "validator_type": self.validator_type.value,
            "independence_class": self.independence_class.value,
            "accountable_owner_ref": self.accountable_owner_ref,
            "authority_ref": self.authority_ref,
            "feedback_due_at": self.feedback_due_at,
            "reversibility_class": self.reversibility_class,
            "risk_class": self.risk_class,
            "proxy_metric": self.proxy_metric,
            "proxy_failure_modes": list(self.proxy_failure_modes),
            "inconclusive_policy": self.inconclusive_policy,
            "max_attempts": self.max_attempts,
            "max_evidence_items": self.max_evidence_items,
            "created_at": self.created_at,
        }

    def verify(self) -> None:
        if canonical_digest(self.canonical_payload()) != self.content_digest:
            raise ValueError("Oracle Contract digest is invalid")
        if self.validator_type is OracleValidatorType.UNVERIFIABLE and self.observable_signals:
            raise ValueError("An UNVERIFIABLE Oracle cannot claim observable signals")
        if self.max_attempts < 1 or self.max_attempts > 100:
            raise ValueError("Oracle Contract attempt bound is invalid")
        if self.max_evidence_items < 0 or self.max_evidence_items > 1000:
            raise ValueError("Oracle Contract evidence bound is invalid")


@dataclass(frozen=True, slots=True)
class OutcomeObservation:
    outcome_id: str
    oracle_contract_id: str
    binding_id: str
    request_id: str
    job_id: str
    result_digest: str
    expected_signal: str
    observed_signal: str
    observed_at: str | None
    source_ref: str | None
    verdict: OutcomeVerdict
    confounders: tuple[str, ...]
    attribution_status: AttributionStatus
    reviewer_ref: str | None
    created_at: str
    updated_at: str

