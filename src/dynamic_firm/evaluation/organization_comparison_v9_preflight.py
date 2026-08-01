"""Provider-free H4 preparation for one v9 live-comparison slot.

This module prepares an admission record only.  It does not reserve a slot,
call a provider, execute a workload, or write a ledger.  The result template
is intentionally append-only: one immutable terminal record can be produced
for the exact slot, and a terminal record cannot be appended again.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
import json
from typing import Literal, Mapping

from .organization_comparison_v9 import (
    OrganizationComparisonV9Manifest,
    SealedSlot,
    validate_v9_manifest,
)


PREFLIGHT_SCHEMA = "noruct.organization-comparison.v9-live-preflight.v1"
RESULT_SCHEMA = "noruct.organization-comparison.v9-live-result.v1"
PREFLIGHT_STATUS = "H4_EXECUTION_DEFERRED"
EXECUTION_STATUS = "NOT_STARTED"

OUTCOME_SUCCESS = "SUCCESS"
OUTCOME_FAILED = "FAILED"
OUTCOME_INTERRUPTED = "INTERRUPTED"
OUTCOME_NEGATIVE = "NEGATIVE_OUTCOME"
OUTCOMES = frozenset(
    {OUTCOME_SUCCESS, OUTCOME_FAILED, OUTCOME_INTERRUPTED, OUTCOME_NEGATIVE}
)


class OrganizationComparisonV9PreflightError(ValueError):
    """A required H4 admission field or sealed-slot identity is invalid."""


class OrganizationComparisonV9SlotReuseError(
    OrganizationComparisonV9PreflightError
):
    """The requested slot is already represented by a prior result."""


def _identity(name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OrganizationComparisonV9PreflightError(f"{name} is required")
    return value


def _boolean(name: str, value: bool) -> bool:
    if type(value) is not bool:
        raise OrganizationComparisonV9PreflightError(f"{name} must be a boolean")
    return value


def _positive_integer(name: str, value: int) -> int:
    if type(value) is not int or value <= 0:
        raise OrganizationComparisonV9PreflightError(
            f"{name} must be a positive integer"
        )
    return value


def _nonnegative_integer(name: str, value: int) -> int:
    if type(value) is not int or value < 0:
        raise OrganizationComparisonV9PreflightError(
            f"{name} must be a non-negative integer"
        )
    return value


def _primitive(value: object) -> object:
    if hasattr(value, "__dataclass_fields__"):
        return {
            name: _primitive(getattr(value, name))
            for name in value.__dataclass_fields__  # type: ignore[attr-defined]
        }
    if isinstance(value, Mapping):
        return {str(key): _primitive(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, frozenset)):
        return [_primitive(item) for item in value]
    return value


def _digest(value: object) -> str:
    encoded = json.dumps(
        _primitive(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class OrganizationComparisonV9ResultRecord:
    """One terminal, append-only observation for one sealed slot."""

    schema_version: str
    manifest_content_hash: str
    slot_id: str
    slot_seal_hash: str
    record_id: str
    outcome: Literal["SUCCESS", "FAILED", "INTERRUPTED", "NEGATIVE_OUTCOME"]
    outcome_reason: str
    evidence_digest: str
    append_only: bool

    def __post_init__(self) -> None:
        if self.schema_version != RESULT_SCHEMA:
            raise OrganizationComparisonV9PreflightError(
                "result schema is incompatible"
            )
        _identity("manifest_content_hash", self.manifest_content_hash)
        _identity("slot_id", self.slot_id)
        _identity("slot_seal_hash", self.slot_seal_hash)
        _identity("record_id", self.record_id)
        if self.outcome not in OUTCOMES:
            raise OrganizationComparisonV9PreflightError("unknown terminal outcome")
        _identity("outcome_reason", self.outcome_reason)
        _identity("evidence_digest", self.evidence_digest)
        if not _boolean("append_only", self.append_only):
            raise OrganizationComparisonV9PreflightError(
                "result records must remain append-only"
            )

    def payload(self) -> Mapping[str, object]:
        return {
            "schema_version": self.schema_version,
            "manifest_content_hash": self.manifest_content_hash,
            "slot_id": self.slot_id,
            "slot_seal_hash": self.slot_seal_hash,
            "record_id": self.record_id,
            "outcome": self.outcome,
            "outcome_reason": self.outcome_reason,
            "evidence_digest": self.evidence_digest,
            "append_only": self.append_only,
        }

    def append_result(self, *args: object, **kwargs: object) -> "OrganizationComparisonV9ResultRecord":
        """Reject mutation/reuse once a terminal result exists."""

        del args, kwargs
        raise OrganizationComparisonV9SlotReuseError(
            "a terminal v9 slot result cannot be appended twice"
        )


@dataclass(frozen=True, slots=True)
class OrganizationComparisonV9ResultTemplate:
    """An empty result envelope whose only legal transition is one terminal append."""

    schema_version: str
    manifest_content_hash: str
    slot_id: str
    slot_seal_hash: str
    result_fields: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.schema_version != RESULT_SCHEMA:
            raise OrganizationComparisonV9PreflightError(
                "result template schema is incompatible"
            )
        _identity("manifest_content_hash", self.manifest_content_hash)
        _identity("slot_id", self.slot_id)
        _identity("slot_seal_hash", self.slot_seal_hash)
        if self.result_fields != (
            "record_id",
            "outcome",
            "outcome_reason",
            "evidence_digest",
        ):
            raise OrganizationComparisonV9PreflightError(
                "result template fields are not append-only terminal fields"
            )

    def append(
        self,
        *,
        record_id: str,
        outcome: str,
        outcome_reason: str,
        evidence_digest: str,
    ) -> OrganizationComparisonV9ResultRecord:
        if outcome not in OUTCOMES:
            raise OrganizationComparisonV9PreflightError("unknown terminal outcome")
        return OrganizationComparisonV9ResultRecord(
            schema_version=RESULT_SCHEMA,
            manifest_content_hash=self.manifest_content_hash,
            slot_id=self.slot_id,
            slot_seal_hash=self.slot_seal_hash,
            record_id=record_id,
            outcome=outcome,  # type: ignore[arg-type]
            outcome_reason=outcome_reason,
            evidence_digest=evidence_digest,
            append_only=True,
        )

    append_result = append


@dataclass(frozen=True, slots=True)
class OrganizationComparisonV9LivePreflight:
    """Immutable H4 gate; it has no execution, reservation, or provider effect."""

    schema_version: str
    manifest_content_hash: str
    slot_id: str
    slot_seal_hash: str
    quota_identity: str
    quota_available_model_calls: int
    quota_requested_model_calls: int
    quota_available_wall_time_ms: int
    quota_requested_wall_time_ms: int
    provider_kind: str
    model_pin: str
    consent_id: str
    consent_scope: str
    consent_confirmed: bool
    stop_threshold_id: str
    stop_threshold: str
    stop_threshold_confirmed: bool
    evaluator_identity: str
    evaluator_risk_id: str
    evaluator_risk_summary: str
    evaluator_risk_accepted: bool
    one_slot_confirmation_id: str
    confirmed_slot_id: str
    confirmed_slot_count: int
    one_slot_confirmed: bool
    execution_status: str
    execution_started: bool
    provider_calls: int
    slot_reserved: bool
    result_template: OrganizationComparisonV9ResultTemplate
    preflight_hash: str

    def payload(self) -> Mapping[str, object]:
        return {
            "schema_version": self.schema_version,
            "manifest_content_hash": self.manifest_content_hash,
            "slot_id": self.slot_id,
            "slot_seal_hash": self.slot_seal_hash,
            "quota_identity": self.quota_identity,
            "quota_available_model_calls": self.quota_available_model_calls,
            "quota_requested_model_calls": self.quota_requested_model_calls,
            "quota_available_wall_time_ms": self.quota_available_wall_time_ms,
            "quota_requested_wall_time_ms": self.quota_requested_wall_time_ms,
            "provider_kind": self.provider_kind,
            "model_pin": self.model_pin,
            "consent_id": self.consent_id,
            "consent_scope": self.consent_scope,
            "consent_confirmed": self.consent_confirmed,
            "stop_threshold_id": self.stop_threshold_id,
            "stop_threshold": self.stop_threshold,
            "stop_threshold_confirmed": self.stop_threshold_confirmed,
            "evaluator_identity": self.evaluator_identity,
            "evaluator_risk_id": self.evaluator_risk_id,
            "evaluator_risk_summary": self.evaluator_risk_summary,
            "evaluator_risk_accepted": self.evaluator_risk_accepted,
            "one_slot_confirmation_id": self.one_slot_confirmation_id,
            "confirmed_slot_id": self.confirmed_slot_id,
            "confirmed_slot_count": self.confirmed_slot_count,
            "one_slot_confirmed": self.one_slot_confirmed,
            "execution_status": self.execution_status,
            "execution_started": self.execution_started,
            "provider_calls": self.provider_calls,
            "slot_reserved": self.slot_reserved,
            "result_template": self.result_template,
        }

    @property
    def ready(self) -> bool:
        return True


def _find_slot(
    manifest: OrganizationComparisonV9Manifest, slot_id: str
) -> SealedSlot:
    for slot in manifest.slots():
        if slot.slot_id == slot_id:
            return slot
    raise OrganizationComparisonV9PreflightError("slot_id is not in the sealed v9 manifest")


def prepare_v9_slot_preflight(
    *,
    manifest: OrganizationComparisonV9Manifest,
    slot_id: str,
    quota_identity: str,
    quota_available_model_calls: int,
    quota_available_wall_time_ms: int,
    provider_kind: str,
    model_pin: str,
    consent_id: str,
    consent_scope: str,
    consent_confirmed: bool,
    stop_threshold_id: str,
    stop_threshold: str,
    stop_threshold_confirmed: bool,
    evaluator_identity: str,
    evaluator_risk_id: str,
    evaluator_risk_summary: str,
    evaluator_risk_accepted: bool,
    one_slot_confirmation_id: str,
    confirmed_slot_id: str,
    confirmed_slot_count: int,
    one_slot_confirmed: bool,
    prior_result_slot_ids: tuple[str, ...] = (),
) -> OrganizationComparisonV9LivePreflight:
    """Validate H4 inputs without starting, reserving, or calling anything."""

    validate_v9_manifest(manifest)
    slot_id = _identity("slot_id", slot_id)
    slot = _find_slot(manifest, slot_id)
    prior_result_slot_ids = tuple(prior_result_slot_ids)
    if slot_id in prior_result_slot_ids:
        raise OrganizationComparisonV9SlotReuseError(
            "a slot represented by a prior result cannot be reused"
        )
    if len(set(prior_result_slot_ids)) != len(prior_result_slot_ids):
        raise OrganizationComparisonV9SlotReuseError(
            "prior result slot identities must be unique"
        )

    quota_identity = _identity("quota_identity", quota_identity)
    available_calls = _positive_integer(
        "quota_available_model_calls", quota_available_model_calls
    )
    available_wall = _positive_integer(
        "quota_available_wall_time_ms", quota_available_wall_time_ms
    )
    if available_calls < slot.budget_model_calls:
        raise OrganizationComparisonV9PreflightError("quota is below the sealed slot budget")
    if available_wall < slot.budget_wall_time_ms:
        raise OrganizationComparisonV9PreflightError(
            "wall-time quota is below the sealed slot budget"
        )

    provider_kind = _identity("provider_kind", provider_kind)
    model_pin = _identity("model_pin", model_pin)
    consent_id = _identity("consent_id", consent_id)
    consent_scope = _identity("consent_scope", consent_scope)
    if not _boolean("consent_confirmed", consent_confirmed):
        raise OrganizationComparisonV9PreflightError("H4 consent is required")
    stop_threshold_id = _identity("stop_threshold_id", stop_threshold_id)
    stop_threshold = _identity("stop_threshold", stop_threshold)
    if not _boolean("stop_threshold_confirmed", stop_threshold_confirmed):
        raise OrganizationComparisonV9PreflightError("H4 stop threshold is required")
    evaluator_identity = _identity("evaluator_identity", evaluator_identity)
    if evaluator_identity != manifest.evaluator.identity:
        raise OrganizationComparisonV9PreflightError(
            "evaluator identity does not match the sealed manifest"
        )
    evaluator_risk_id = _identity("evaluator_risk_id", evaluator_risk_id)
    evaluator_risk_summary = _identity(
        "evaluator_risk_summary", evaluator_risk_summary
    )
    if not _boolean("evaluator_risk_accepted", evaluator_risk_accepted):
        raise OrganizationComparisonV9PreflightError("evaluator risk is not accepted")

    one_slot_confirmation_id = _identity(
        "one_slot_confirmation_id", one_slot_confirmation_id
    )
    confirmed_slot_id = _identity("confirmed_slot_id", confirmed_slot_id)
    if confirmed_slot_id != slot_id:
        raise OrganizationComparisonV9PreflightError(
            "confirmation must identify the exact selected slot"
        )
    confirmed_slot_count = _positive_integer(
        "confirmed_slot_count", confirmed_slot_count
    )
    if confirmed_slot_count != 1:
        raise OrganizationComparisonV9PreflightError(
            "confirmation must cover exactly one slot"
        )
    if not _boolean("one_slot_confirmed", one_slot_confirmed):
        raise OrganizationComparisonV9PreflightError(
            "exactly-one-slot confirmation is required"
        )

    template = OrganizationComparisonV9ResultTemplate(
        schema_version=RESULT_SCHEMA,
        manifest_content_hash=manifest.content_hash,
        slot_id=slot.slot_id,
        slot_seal_hash=slot.seal_hash,
        result_fields=(
            "record_id",
            "outcome",
            "outcome_reason",
            "evidence_digest",
        ),
    )
    base = OrganizationComparisonV9LivePreflight(
        schema_version=PREFLIGHT_SCHEMA,
        manifest_content_hash=manifest.content_hash,
        slot_id=slot.slot_id,
        slot_seal_hash=slot.seal_hash,
        quota_identity=quota_identity,
        quota_available_model_calls=available_calls,
        quota_requested_model_calls=slot.budget_model_calls,
        quota_available_wall_time_ms=available_wall,
        quota_requested_wall_time_ms=slot.budget_wall_time_ms,
        provider_kind=provider_kind,
        model_pin=model_pin,
        consent_id=consent_id,
        consent_scope=consent_scope,
        consent_confirmed=consent_confirmed,
        stop_threshold_id=stop_threshold_id,
        stop_threshold=stop_threshold,
        stop_threshold_confirmed=stop_threshold_confirmed,
        evaluator_identity=evaluator_identity,
        evaluator_risk_id=evaluator_risk_id,
        evaluator_risk_summary=evaluator_risk_summary,
        evaluator_risk_accepted=evaluator_risk_accepted,
        one_slot_confirmation_id=one_slot_confirmation_id,
        confirmed_slot_id=confirmed_slot_id,
        confirmed_slot_count=confirmed_slot_count,
        one_slot_confirmed=one_slot_confirmed,
        execution_status=EXECUTION_STATUS,
        execution_started=False,
        provider_calls=0,
        slot_reserved=False,
        result_template=template,
        preflight_hash="",
    )
    return replace(base, preflight_hash=_digest(base.payload()))


create_v9_live_preflight = prepare_v9_slot_preflight
build_v9_live_preflight = prepare_v9_slot_preflight
