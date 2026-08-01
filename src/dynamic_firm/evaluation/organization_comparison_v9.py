"""Provider-free sealed contract for the D06 organization comparison.

This module describes the experiment only.  It does not build prompts, call a
provider, execute a slot, or score an episode.  A v9 manifest is deliberately
separate from earlier campaign manifests; earlier results cannot be migrated
into this contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from math import isfinite
from typing import Mapping


V9_SCHEMA = "noruct.organization-comparison.v9"
V9_REPORT_SCHEMA = "noruct.organization-comparison-report.v9"
PROVIDER_FREE_REHEARSAL = "provider-free-rehearsal"
SLOT_COUNT_PER_ARM = 4

ORGANIZATION_ARMS = (
    "strong-solo",
    "homogeneous-replica",
    "heterogeneous-graph",
    "manager-led-graph",
)
LEGACY_V7_V8_SCHEMAS = frozenset(
    {
        "noruct.organization-comparison.v7",
        "noruct.organization-comparison.v8",
        "noruct.organization-comparison-campaign.v7",
        "noruct.organization-comparison-campaign.v8",
    }
)


class V9ManifestError(ValueError):
    """Base class for fail-closed v9 contract errors."""


class LegacyCampaignRejected(V9ManifestError):
    """An earlier campaign was supplied where a fresh v9 manifest is needed."""


class V9IntegrityError(V9ManifestError):
    """A sealed object no longer matches the bytes that were sealed."""


@dataclass(frozen=True, slots=True)
class EvaluatorIndependence:
    """Evaluator identity and independence constraints shared by every arm."""

    identity: str
    profile: str
    network_isolated: bool
    credential_inheritance: bool
    independent_of_arm: bool
    independent_of_results: bool

    def __post_init__(self) -> None:
        if not self.identity.strip() or not self.profile.strip():
            raise V9ManifestError("v9 evaluator identity and profile are required")
        if not self.network_isolated:
            raise V9ManifestError("v9 evaluator must be network isolated")
        if self.credential_inheritance:
            raise V9ManifestError("v9 evaluator may not inherit execution credentials")
        if not self.independent_of_arm or not self.independent_of_results:
            raise V9ManifestError("v9 evaluator independence is required")


@dataclass(frozen=True, slots=True)
class SealedSlot:
    """One provider-free, non-reusable slot envelope; it contains no prompt."""

    slot_id: str
    arm: str
    ordinal: int
    task_revision: str
    source_revision: str
    authority_revision: str
    budget_model_calls: int
    budget_wall_time_ms: int
    acceptance_revision: str
    evaluator_identity: str
    provider_kind: str
    sealed: bool
    seal_hash: str

    def payload(self) -> Mapping[str, object]:
        return {
            "slot_id": self.slot_id,
            "arm": self.arm,
            "ordinal": self.ordinal,
            "task_revision": self.task_revision,
            "source_revision": self.source_revision,
            "authority_revision": self.authority_revision,
            "budget_model_calls": self.budget_model_calls,
            "budget_wall_time_ms": self.budget_wall_time_ms,
            "acceptance_revision": self.acceptance_revision,
            "evaluator_identity": self.evaluator_identity,
            "provider_kind": self.provider_kind,
            "sealed": self.sealed,
        }


@dataclass(frozen=True, slots=True)
class OrganizationComparisonV9Manifest:
    """The single immutable identity shared by the four comparison arms."""

    schema_version: str
    benchmark_id: str
    task_revision: str
    source_revision: str
    authority_revision: str
    budget_model_calls: int
    budget_wall_time_ms: int
    acceptance_revision: str
    evaluator: EvaluatorIndependence
    arms: tuple[tuple[str, tuple[SealedSlot, ...]], ...]
    sealed: bool
    content_hash: str

    def content_payload(self) -> Mapping[str, object]:
        return {
            "schema_version": self.schema_version,
            "benchmark_id": self.benchmark_id,
            "task_revision": self.task_revision,
            "source_revision": self.source_revision,
            "authority_revision": self.authority_revision,
            "budget_model_calls": self.budget_model_calls,
            "budget_wall_time_ms": self.budget_wall_time_ms,
            "acceptance_revision": self.acceptance_revision,
            "evaluator": self.evaluator,
            "arms": self.arms,
            "sealed": self.sealed,
        }

    def slots(self) -> tuple[SealedSlot, ...]:
        return tuple(slot for _, slots in self.arms for slot in slots)

    def arm_slots(self, arm: str) -> tuple[SealedSlot, ...]:
        for name, slots in self.arms:
            if name == arm:
                return slots
        raise KeyError(arm)


@dataclass(frozen=True, slots=True)
class LowerDecileQuality:
    observed_slot_count: int
    lower_decile_quality: float | None


@dataclass(frozen=True, slots=True)
class CompleteSafetyFailure:
    slot_count: int
    complete_failure_count: int
    safety_failure_count: int


@dataclass(frozen=True, slots=True)
class CostTime:
    model_call_count: int
    review_call_count: int
    total_call_count: int
    model_time_ms: int
    review_time_ms: int
    total_time_ms: int


@dataclass(frozen=True, slots=True)
class ReviewRework:
    review_wait_ms: int | float | str
    reopened_evidence_count: int | float | str
    unused_subartifact_rate: int | float | str
    rework_count: int | float | str
    approval_friction_count: int | float | str
    unverified_item_discovery: str
    summary_comprehension_status: str


@dataclass(frozen=True, slots=True)
class NegativeTransfer:
    observed_slot_count: int
    negative_transfer_count: int
    fallback_to_strong_solo_count: int


@dataclass(frozen=True, slots=True)
class OrganizationComparisonV9ArmReport:
    """Content-free observations for one arm, kept in separate metric axes."""

    arm: str
    lower_decile_quality: LowerDecileQuality
    complete_safety_failure: CompleteSafetyFailure
    cost_time: CostTime
    review_rework: ReviewRework
    negative_transfer: NegativeTransfer


@dataclass(frozen=True, slots=True)
class OrganizationComparisonV9Report:
    """Report envelope; it stores aggregate facts, never prompts or content."""

    schema_version: str
    benchmark_id: str
    manifest_content_hash: str
    arms: tuple[OrganizationComparisonV9ArmReport, ...]


@dataclass(frozen=True, slots=True)
class ProviderFreeRehearsal:
    schema_version: str
    manifest_content_hash: str
    arms_checked: int
    slots_checked: int
    provider_calls: int
    passed: bool


def _primitive(value: object) -> object:
    if hasattr(value, "__dataclass_fields__"):
        return {
            name: _primitive(getattr(value, name))
            for name in value.__dataclass_fields__  # type: ignore[attr-defined]
        }
    if isinstance(value, tuple):
        return [_primitive(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _primitive(item) for key, item in value.items()}
    return value


def _digest(value: object) -> str:
    encoded = json.dumps(
        _primitive(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _legacy_schema(value: object) -> str | None:
    if isinstance(value, Mapping):
        schema = value.get("schema_version")
    else:
        schema = getattr(value, "schema_version", None)
    return schema if isinstance(schema, str) else None


def reject_legacy_campaign(value: object) -> None:
    """Reject v7/v8 input explicitly; no field-by-field migration exists."""

    schema = _legacy_schema(value)
    if schema in LEGACY_V7_V8_SCHEMAS or (
        schema is not None
        and (schema.endswith(".v7") or schema.endswith(".v8"))
    ):
        raise LegacyCampaignRejected(
            f"v9 refuses legacy campaign input ({schema}); create a fresh manifest"
        )


def _require_revision(name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise V9ManifestError(f"v9 {name} must be a non-empty exact revision")
    return value


def _require_budget(model_calls: int, wall_time_ms: int) -> None:
    if type(model_calls) is not int or model_calls <= 0:
        raise V9ManifestError("v9 model-call budget must be a positive integer")
    if type(wall_time_ms) is not int or wall_time_ms <= 0:
        raise V9ManifestError("v9 wall-time budget must be a positive integer")


def _make_slot(
    *,
    arm: str,
    ordinal: int,
    task_revision: str,
    source_revision: str,
    authority_revision: str,
    budget_model_calls: int,
    budget_wall_time_ms: int,
    acceptance_revision: str,
    evaluator_identity: str,
) -> SealedSlot:
    payload = {
        "slot_id": f"v9:{arm}:{ordinal}",
        "arm": arm,
        "ordinal": ordinal,
        "task_revision": task_revision,
        "source_revision": source_revision,
        "authority_revision": authority_revision,
        "budget_model_calls": budget_model_calls,
        "budget_wall_time_ms": budget_wall_time_ms,
        "acceptance_revision": acceptance_revision,
        "evaluator_identity": evaluator_identity,
        "provider_kind": PROVIDER_FREE_REHEARSAL,
        "sealed": True,
    }
    return SealedSlot(**payload, seal_hash=_digest(payload))


def create_v9_manifest(
    *,
    task_revision: str,
    source_revision: str,
    authority_revision: str,
    budget_model_calls: int,
    budget_wall_time_ms: int,
    acceptance_revision: str,
    evaluator: EvaluatorIndependence,
    benchmark_id: str = "organization-comparison-v9",
    legacy_input: object | None = None,
) -> OrganizationComparisonV9Manifest:
    """Create and seal the fresh four-arm v9 manifest."""

    if legacy_input is not None:
        reject_legacy_campaign(legacy_input)
    if not isinstance(evaluator, EvaluatorIndependence):
        raise V9ManifestError("v9 evaluator must use the typed independence contract")
    if not isinstance(benchmark_id, str) or not benchmark_id.strip():
        raise V9ManifestError("v9 benchmark id is required")
    task_revision = _require_revision("task revision", task_revision)
    source_revision = _require_revision("source revision", source_revision)
    authority_revision = _require_revision("authority revision", authority_revision)
    acceptance_revision = _require_revision("acceptance revision", acceptance_revision)
    _require_budget(budget_model_calls, budget_wall_time_ms)

    arms = tuple(
        (
            arm,
            tuple(
                _make_slot(
                    arm=arm,
                    ordinal=ordinal,
                    task_revision=task_revision,
                    source_revision=source_revision,
                    authority_revision=authority_revision,
                    budget_model_calls=budget_model_calls,
                    budget_wall_time_ms=budget_wall_time_ms,
                    acceptance_revision=acceptance_revision,
                    evaluator_identity=evaluator.identity,
                )
                for ordinal in range(1, SLOT_COUNT_PER_ARM + 1)
            ),
        )
        for arm in ORGANIZATION_ARMS
    )
    base = OrganizationComparisonV9Manifest(
        schema_version=V9_SCHEMA,
        benchmark_id=benchmark_id,
        task_revision=task_revision,
        source_revision=source_revision,
        authority_revision=authority_revision,
        budget_model_calls=budget_model_calls,
        budget_wall_time_ms=budget_wall_time_ms,
        acceptance_revision=acceptance_revision,
        evaluator=evaluator,
        arms=arms,
        sealed=True,
        content_hash="",
    )
    manifest = OrganizationComparisonV9Manifest(
        schema_version=base.schema_version,
        benchmark_id=base.benchmark_id,
        task_revision=base.task_revision,
        source_revision=base.source_revision,
        authority_revision=base.authority_revision,
        budget_model_calls=base.budget_model_calls,
        budget_wall_time_ms=base.budget_wall_time_ms,
        acceptance_revision=base.acceptance_revision,
        evaluator=base.evaluator,
        arms=base.arms,
        sealed=base.sealed,
        content_hash=_digest(base.content_payload()),
    )
    validate_v9_manifest(manifest)
    return manifest


def validate_v9_manifest(manifest: OrganizationComparisonV9Manifest) -> None:
    """Fail closed on tampering, mismatching identity, or slot reuse."""

    reject_legacy_campaign(manifest)
    if not isinstance(manifest, OrganizationComparisonV9Manifest):
        raise V9ManifestError("expected an organization comparison v9 manifest")
    if manifest.schema_version != V9_SCHEMA or not manifest.sealed:
        raise V9IntegrityError("v9 manifest is not sealed with the expected schema")
    if _digest(manifest.content_payload()) != manifest.content_hash:
        raise V9IntegrityError("v9 manifest content hash does not match sealed content")
    if len(manifest.arms) != len(ORGANIZATION_ARMS):
        raise V9ManifestError("v9 requires exactly four named organization arms")
    if tuple(name for name, _ in manifest.arms) != ORGANIZATION_ARMS:
        raise V9ManifestError("v9 organization arms are not the required named arms")
    _require_revision("task revision", manifest.task_revision)
    _require_revision("source revision", manifest.source_revision)
    _require_revision("authority revision", manifest.authority_revision)
    _require_revision("acceptance revision", manifest.acceptance_revision)
    _require_budget(manifest.budget_model_calls, manifest.budget_wall_time_ms)
    if not isinstance(manifest.evaluator, EvaluatorIndependence):
        raise V9ManifestError("v9 evaluator contract is missing")

    seen: set[str] = set()
    for arm, slots in manifest.arms:
        if len(slots) != SLOT_COUNT_PER_ARM:
            raise V9ManifestError("each v9 arm must contain four sealed slots")
        for expected_ordinal, slot in enumerate(slots, start=1):
            if not isinstance(slot, SealedSlot) or not slot.sealed:
                raise V9IntegrityError("v9 contains an unsealed slot")
            if slot.slot_id in seen:
                raise V9ManifestError("v9 slot was duplicated or reused")
            seen.add(slot.slot_id)
            if slot.arm != arm or slot.ordinal != expected_ordinal:
                raise V9ManifestError("v9 slot identity does not match its arm envelope")
            if slot.provider_kind != PROVIDER_FREE_REHEARSAL:
                raise V9ManifestError("v9 rehearsal slot cannot select a provider")
            if (
                slot.task_revision != manifest.task_revision
                or slot.source_revision != manifest.source_revision
                or slot.authority_revision != manifest.authority_revision
                or slot.budget_model_calls != manifest.budget_model_calls
                or slot.budget_wall_time_ms != manifest.budget_wall_time_ms
                or slot.acceptance_revision != manifest.acceptance_revision
                or slot.evaluator_identity != manifest.evaluator.identity
            ):
                raise V9ManifestError("v9 slot has an unmatched shared comparison field")
            if _digest(slot.payload()) != slot.seal_hash:
                raise V9IntegrityError("v9 slot seal hash does not match sealed content")
    if len(seen) != len(ORGANIZATION_ARMS) * SLOT_COUNT_PER_ARM:
        raise V9ManifestError("v9 slots are not globally unique")


def rehearse_provider_free(
    manifest: OrganizationComparisonV9Manifest,
) -> ProviderFreeRehearsal:
    """Validate all sealed envelopes without constructing or executing a slot."""

    validate_v9_manifest(manifest)
    return ProviderFreeRehearsal(
        schema_version=V9_SCHEMA,
        manifest_content_hash=manifest.content_hash,
        arms_checked=len(manifest.arms),
        slots_checked=len(manifest.slots()),
        provider_calls=0,
        passed=True,
    )


# Descriptive aliases keep the contract easy to discover without adding a
# second implementation.
seal_v9_manifest = create_v9_manifest
run_provider_free_rehearsal = rehearse_provider_free
