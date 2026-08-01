"""Future-Job compatibility invalidation with immutable active pins.

This in-memory contract is deliberately narrower than route selection or a
runtime store.  A material change can make a compatibility smoke result stale
for a later Job, but it cannot rewrite an already issued pin.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .model_compatibility import CompatibilityEvidence


class MaterialDriftReason(StrEnum):
    ADAPTER_REVISION = "ADAPTER_REVISION"
    MODEL_IDENTITY = "MODEL_IDENTITY"
    API_CONTRACT = "API_CONTRACT"
    REPEATED_CONTRACT_FAILURE = "REPEATED_CONTRACT_FAILURE"


class FutureEligibilityStatus(StrEnum):
    ELIGIBLE = "ELIGIBLE"
    REQUIRES_COMPATIBILITY_REFRESH = "REQUIRES_COMPATIBILITY_REFRESH"


@dataclass(frozen=True, slots=True)
class CompatibilityKey:
    route_id: str
    adapter_revision: str
    material_identity_digest: str

    @classmethod
    def from_evidence(cls, evidence: CompatibilityEvidence) -> "CompatibilityKey":
        if not isinstance(evidence, CompatibilityEvidence):
            raise TypeError("compatibility evidence is required")
        return cls(
            route_id=evidence.route_id,
            adapter_revision=evidence.adapter_revision,
            material_identity_digest=evidence.material_identity_digest,
        )


@dataclass(frozen=True, slots=True)
class ActiveCompatibilityPin:
    """The exact evidence used by an active or historical Job receipt."""

    job_id: str
    key: CompatibilityKey
    evidence_digest: str


@dataclass(frozen=True, slots=True)
class CompatibilityInvalidation:
    key: CompatibilityKey
    evidence_digest: str
    reason: MaterialDriftReason


@dataclass(frozen=True, slots=True)
class FutureEligibilityDecision:
    key: CompatibilityKey
    evidence_digest: str
    status: FutureEligibilityStatus
    invalidation_reason: MaterialDriftReason | None

    @property
    def eligible(self) -> bool:
        return self.status is FutureEligibilityStatus.ELIGIBLE


class CompatibilityEligibilityRegistry:
    """Keeps only current future-Job evidence; existing pins are never owned."""

    def __init__(self) -> None:
        self._eligible: dict[CompatibilityKey, str] = {}
        self._invalidated: dict[CompatibilityKey, CompatibilityInvalidation] = {}

    def record_compatible(self, evidence: CompatibilityEvidence) -> CompatibilityKey:
        if not isinstance(evidence, CompatibilityEvidence) or not evidence.is_compatible:
            raise ValueError("only fully compatible evidence may enable a future Job")
        key = CompatibilityKey.from_evidence(evidence)
        self._eligible[key] = evidence.digest
        self._invalidated.pop(key, None)
        return key

    def pin_active_job(self, job_id: object, evidence: CompatibilityEvidence) -> ActiveCompatibilityPin:
        if not isinstance(job_id, str) or not job_id:
            raise ValueError("job_id is required")
        key = CompatibilityKey.from_evidence(evidence)
        if self._eligible.get(key) != evidence.digest:
            raise ValueError("future eligibility is not current for this compatibility evidence")
        return ActiveCompatibilityPin(job_id=job_id, key=key, evidence_digest=evidence.digest)

    def invalidate(
        self, evidence: CompatibilityEvidence, reason: MaterialDriftReason | str
    ) -> CompatibilityInvalidation:
        key = CompatibilityKey.from_evidence(evidence)
        try:
            typed_reason = MaterialDriftReason(reason)
        except ValueError as exc:
            raise ValueError("material drift reason is unknown") from exc
        invalidation = CompatibilityInvalidation(key, evidence.digest, typed_reason)
        self._invalidated[key] = invalidation
        if self._eligible.get(key) == evidence.digest:
            del self._eligible[key]
        return invalidation

    def future_eligibility(self, evidence: CompatibilityEvidence) -> FutureEligibilityDecision:
        key = CompatibilityKey.from_evidence(evidence)
        if not evidence.is_compatible:
            return FutureEligibilityDecision(
                key, evidence.digest, FutureEligibilityStatus.REQUIRES_COMPATIBILITY_REFRESH, None
            )
        if self._eligible.get(key) == evidence.digest:
            return FutureEligibilityDecision(key, evidence.digest, FutureEligibilityStatus.ELIGIBLE, None)
        invalidation = self._invalidated.get(key)
        return FutureEligibilityDecision(
            key,
            evidence.digest,
            FutureEligibilityStatus.REQUIRES_COMPATIBILITY_REFRESH,
            invalidation.reason if invalidation and invalidation.evidence_digest == evidence.digest else None,
        )
