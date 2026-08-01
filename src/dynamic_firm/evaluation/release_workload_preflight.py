"""Content-free, read-only preflight for the D07 workload-card contract.

The contract records only fixture and review identities.  It does not contain
workload content, customer data, secrets, prompts, provider configuration, or
an approval decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Iterable, Mapping


WORKLOAD_CARD_SCHEMA = "noruct.release-workload-card.v1"
PREFLIGHT_SCHEMA = "noruct.release-workload-preflight.v1"

LOCAL_PREPARATION_COMPLETE = "LOCAL_PREPARATION_COMPLETE"
H4_WORKLOAD_APPROVAL_DEFERRED = "H4_WORKLOAD_APPROVAL_DEFERRED"

EXCLUSION_ALWAYS_WIN_PARALLEL = "ALWAYS_WIN_PARALLEL"
EXCLUSION_MANAGER_PROMPT_SPECIFIC = "MANAGER_PROMPT_SPECIFIC"
EXCLUSION_CONTENT_NOT_FREE = "CONTENT_NOT_FREE"
EXCLUSION_CUSTOMER_SECRET_POSTURE_NOT_DECLARED = (
    "CUSTOMER_SECRET_POSTURE_NOT_DECLARED"
)


class ReleaseWorkloadPreflightError(ValueError):
    """A candidate card or preflight manifest is malformed."""


def _require_identity(name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReleaseWorkloadPreflightError(f"{name} is required")
    return value


def _require_flag(name: str, value: bool) -> bool:
    if type(value) is not bool:
        raise ReleaseWorkloadPreflightError(f"{name} must be a boolean")
    return value


@dataclass(frozen=True, slots=True)
class CandidateWorkloadCard:
    """Immutable metadata for a candidate; no workload content is representable."""

    workload_id: str
    fixture_revision: str
    acceptance_identity: str
    evaluator_identity: str
    privacy_identity: str
    cost_identity: str
    always_win_parallel: bool = False
    manager_prompt_specific: bool = False
    content_free: bool = True
    customer_secret_free: bool = True

    def __post_init__(self) -> None:
        _require_identity("workload_id", self.workload_id)
        _require_identity("fixture_revision", self.fixture_revision)
        _require_identity("acceptance_identity", self.acceptance_identity)
        _require_identity("evaluator_identity", self.evaluator_identity)
        _require_identity("privacy_identity", self.privacy_identity)
        _require_identity("cost_identity", self.cost_identity)
        for name in (
            "always_win_parallel",
            "manager_prompt_specific",
            "content_free",
            "customer_secret_free",
        ):
            _require_flag(name, getattr(self, name))

    def payload(self) -> Mapping[str, object]:
        """Return only the content-free, hashable card representation."""

        return {
            "schema_version": WORKLOAD_CARD_SCHEMA,
            "workload_id": self.workload_id,
            "fixture_revision": self.fixture_revision,
            "acceptance_identity": self.acceptance_identity,
            "evaluator_identity": self.evaluator_identity,
            "privacy_identity": self.privacy_identity,
            "cost_identity": self.cost_identity,
            "always_win_parallel": self.always_win_parallel,
            "manager_prompt_specific": self.manager_prompt_specific,
            "content_free": self.content_free,
            "customer_secret_free": self.customer_secret_free,
        }


@dataclass(frozen=True, slots=True)
class WorkloadPreflightAssessment:
    """A preflight result, not a workload selection or approval."""

    workload_id: str
    preflight_passed: bool
    exclusion_reasons: tuple[str, ...]

    def payload(self) -> Mapping[str, object]:
        return {
            "workload_id": self.workload_id,
            "preflight_passed": self.preflight_passed,
            "exclusion_reasons": self.exclusion_reasons,
        }


@dataclass(frozen=True, slots=True)
class ReleaseWorkloadPreflight:
    """Read-only D07 preparation output with approval explicitly deferred."""

    schema_version: str
    candidate_cards: tuple[CandidateWorkloadCard, ...]
    assessments: tuple[WorkloadPreflightAssessment, ...]
    read_only: bool
    content_free: bool
    customer_secret_free: bool
    local_status: str
    approval_status: str
    manifest_hash: str

    def manifest_payload(self) -> Mapping[str, object]:
        return {
            "schema_version": self.schema_version,
            "candidate_cards": tuple(card.payload() for card in self.candidate_cards),
            "assessments": tuple(
                assessment.payload() for assessment in self.assessments
            ),
            "read_only": self.read_only,
            "content_free": self.content_free,
            "customer_secret_free": self.customer_secret_free,
            "local_status": self.local_status,
            "approval_status": self.approval_status,
        }


def _primitive(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _primitive(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_primitive(item) for item in value]
    return value


def _digest(value: object) -> str:
    encoded = json.dumps(
        _primitive(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _assessment(card: CandidateWorkloadCard) -> WorkloadPreflightAssessment:
    reasons: list[str] = []
    if card.always_win_parallel:
        reasons.append(EXCLUSION_ALWAYS_WIN_PARALLEL)
    if card.manager_prompt_specific:
        reasons.append(EXCLUSION_MANAGER_PROMPT_SPECIFIC)
    if not card.content_free:
        reasons.append(EXCLUSION_CONTENT_NOT_FREE)
    if not card.customer_secret_free:
        reasons.append(EXCLUSION_CUSTOMER_SECRET_POSTURE_NOT_DECLARED)
    return WorkloadPreflightAssessment(
        workload_id=card.workload_id,
        preflight_passed=not reasons,
        exclusion_reasons=tuple(reasons),
    )


def preflight_workload_cards(
    cards: Iterable[CandidateWorkloadCard],
) -> ReleaseWorkloadPreflight:
    """Hash and inspect cards without selecting, approving, or executing them."""

    candidate_cards = tuple(cards)
    if any(not isinstance(card, CandidateWorkloadCard) for card in candidate_cards):
        raise ReleaseWorkloadPreflightError(
            "preflight accepts only CandidateWorkloadCard values"
        )
    workload_ids = tuple(card.workload_id for card in candidate_cards)
    if len(set(workload_ids)) != len(workload_ids):
        raise ReleaseWorkloadPreflightError("workload_id values must be unique")

    assessments = tuple(_assessment(card) for card in candidate_cards)
    base = ReleaseWorkloadPreflight(
        schema_version=PREFLIGHT_SCHEMA,
        candidate_cards=candidate_cards,
        assessments=assessments,
        read_only=True,
        content_free=True,
        customer_secret_free=True,
        local_status=LOCAL_PREPARATION_COMPLETE,
        approval_status=H4_WORKLOAD_APPROVAL_DEFERRED,
        manifest_hash="",
    )
    return ReleaseWorkloadPreflight(
        schema_version=base.schema_version,
        candidate_cards=base.candidate_cards,
        assessments=base.assessments,
        read_only=base.read_only,
        content_free=base.content_free,
        customer_secret_free=base.customer_secret_free,
        local_status=base.local_status,
        approval_status=base.approval_status,
        manifest_hash=_digest(base.manifest_payload()),
    )


def workload_manifest_hash(
    cards: Iterable[CandidateWorkloadCard],
) -> str:
    """Return the deterministic hash of a read-only candidate preflight."""

    return preflight_workload_cards(cards).manifest_hash


# Descriptive aliases keep the D07 contract easy to discover.
run_release_workload_preflight = preflight_workload_cards
hash_workload_manifest = workload_manifest_hash
