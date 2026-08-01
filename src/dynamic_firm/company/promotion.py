from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Callable, Mapping

from dynamic_firm.runtime.models import to_primitive, utc_now

from .models import (
    OrganizationEpisode,
    WorkflowPatchCandidate,
    WorkflowPatchEvent,
    WorkflowPatchEventType,
    WorkflowPatchStatus,
    WorkflowPattern,
    WorkflowTaskTemplate,
    content_digest,
    workflow_task_from_dict,
)
from .store import CompanyStateStore


WORKFLOW_PATCH_PROMOTION_EVIDENCE_SCHEMA = (
    "noruct.workflow-patch-promotion-evidence.v1"
)
WORKFLOW_PATCH_PROMOTION_ENVELOPE_SCHEMA = (
    "noruct.workflow-patch-promotion-envelope.v1"
)
WORKFLOW_PATCH_PROMOTION_PREVIEW_SCHEMA = (
    "noruct.workflow-patch-promotion-preview.v1"
)
_DIGEST = re.compile(r"[0-9a-f]{64}")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,191}")


class WorkflowPatchPromotionFailureCode(StrEnum):
    INVALID_EVIDENCE = "INVALID_EVIDENCE"
    IMMUTABLE_PARENT_TARGET = "IMMUTABLE_PARENT_TARGET"
    PARENT_COMPANY_DRIFT = "PARENT_COMPANY_DRIFT"
    COMPANY_STATE_DRIFT = "COMPANY_STATE_DRIFT"
    SOURCE_PATTERN_DRIFT = "SOURCE_PATTERN_DRIFT"
    TARGET_PATTERN_CONFLICT = "TARGET_PATTERN_CONFLICT"
    INVALID_ENVELOPE = "INVALID_ENVELOPE"


class WorkflowPatchPromotionError(ValueError):
    def __init__(self, code: WorkflowPatchPromotionFailureCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code


@dataclass(frozen=True, slots=True)
class WorkflowPatchPromotionCheck:
    name: str
    passed: bool
    evidence: str


@dataclass(frozen=True, slots=True)
class WorkflowPatchPromotionEvidence:
    """Bounded first-party projection of one fully verified evaluation lineage."""

    schema_version: str
    content_hash: str
    pair_id: str
    manifest_content_hash: str
    comparison_content_hash: str
    comparison_file_sha256: str
    binding_id: str
    binding_content_hash: str
    preparation_id: str
    preparation_content_hash: str
    source_revision: str
    distribution_sha256: str
    model_id: str
    authority_profile: str
    company_revision: int
    roster_revision: int
    playbook_revision: int
    parent_extension_id: str
    parent_pattern_id: str
    parent_semantic_anchor: str
    parent_company_state_sha256: str
    production_context_fingerprint: str
    bound_pattern_id: str
    workload_hash: str
    run_ids: tuple[str, ...]
    live_evidence_ids: tuple[str, ...]
    live_evidence_content_hashes: tuple[str, ...]
    control_quality: float
    candidate_quality: float
    quality_gain: float
    model_call_delta: int
    repair_delta: int
    token_delta: int
    runtime_compatibility_digest: str
    pair_gate_passed: bool
    proposal_recommended: bool
    automatic_approval: bool
    eligible_for_apply: bool
    external_model_calls: int
    quota_consumed: bool

    def content_payload(self) -> Mapping[str, object]:
        payload = to_primitive(self)
        assert isinstance(payload, dict)
        payload.pop("content_hash", None)
        return payload


@dataclass(frozen=True, slots=True)
class WorkflowPatchPromotionEnvelope:
    schema_version: str
    promotion_id: str
    content_hash: str
    source_patch_id: str
    source_pattern_id: str
    source_context_fingerprint: str
    source_plan_digest: str
    source_episode_ids: tuple[str, ...]
    source_episode_content_hashes: tuple[str, ...]
    binding_id: str
    binding_content_hash: str
    preparation_id: str
    preparation_content_hash: str
    pair_id: str
    manifest_content_hash: str
    comparison_content_hash: str
    comparison_file_sha256: str
    live_run_ids: tuple[str, ...]
    live_evidence_ids: tuple[str, ...]
    live_evidence_content_hashes: tuple[str, ...]
    source_revision: str
    distribution_sha256: str
    model_id: str
    authority_profile: str
    workload_hash: str
    target_context_fingerprint: str
    target_pattern_id: str
    task_family: str
    execution_profile: str
    tasks: tuple[WorkflowTaskTemplate, ...]
    maximum_parallelism: int
    writer_count: int
    evidence_count: int
    quality_gain: float
    model_call_delta: int
    repair_delta: int
    token_delta: int
    base_company_revision: int
    base_roster_revision: int
    base_playbook_revision: int
    base_company_state_digest: str
    runtime_compatibility_digest: str
    automatic_approval: bool
    automatic_apply: bool

    def content_payload(self) -> Mapping[str, object]:
        payload = to_primitive(self)
        assert isinstance(payload, dict)
        payload.pop("promotion_id", None)
        payload.pop("content_hash", None)
        return payload

    @classmethod
    def create(cls, **values: object) -> WorkflowPatchPromotionEnvelope:
        provisional = cls(
            schema_version=WORKFLOW_PATCH_PROMOTION_ENVELOPE_SCHEMA,
            promotion_id="pending",
            content_hash="pending",
            **values,
        )
        digest = content_digest(provisional.content_payload())
        return cls(
            **{
                **values,
                "schema_version": WORKFLOW_PATCH_PROMOTION_ENVELOPE_SCHEMA,
                "promotion_id": f"workflow-promotion-{digest[:24]}",
                "content_hash": digest,
            }
        )

    @classmethod
    def from_mapping(
        cls, value: Mapping[str, object]
    ) -> WorkflowPatchPromotionEnvelope:
        expected = set(cls.__dataclass_fields__)
        if set(value) != expected:
            raise WorkflowPatchPromotionError(
                WorkflowPatchPromotionFailureCode.INVALID_ENVELOPE,
                "promotion envelope fields differ",
            )
        raw_tasks = value.get("tasks")
        if not isinstance(raw_tasks, list):
            raise WorkflowPatchPromotionError(
                WorkflowPatchPromotionFailureCode.INVALID_ENVELOPE,
                "promotion tasks must be a list",
            )
        try:
            envelope = cls(
                **{
                    **value,
                    "source_episode_ids": tuple(value["source_episode_ids"]),
                    "source_episode_content_hashes": tuple(
                        value["source_episode_content_hashes"]
                    ),
                    "live_run_ids": tuple(value["live_run_ids"]),
                    "live_evidence_ids": tuple(value["live_evidence_ids"]),
                    "live_evidence_content_hashes": tuple(
                        value["live_evidence_content_hashes"]
                    ),
                    "tasks": tuple(workflow_task_from_dict(item) for item in raw_tasks),
                }
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkflowPatchPromotionError(
                WorkflowPatchPromotionFailureCode.INVALID_ENVELOPE,
                "promotion envelope cannot be decoded",
            ) from exc
        envelope.validate()
        return envelope

    def validate(self) -> None:
        identifiers = (
            self.promotion_id,
            self.source_patch_id,
            self.source_pattern_id,
            self.binding_id,
            self.preparation_id,
            self.pair_id,
            self.target_context_fingerprint,
            self.target_pattern_id,
            self.task_family,
            self.execution_profile,
            self.model_id,
            self.authority_profile,
        )
        digests = (
            self.content_hash,
            self.source_plan_digest,
            *self.source_episode_content_hashes,
            self.binding_content_hash,
            self.preparation_content_hash,
            self.manifest_content_hash,
            self.comparison_content_hash,
            self.comparison_file_sha256,
            *self.live_evidence_content_hashes,
            self.distribution_sha256,
            self.workload_hash,
            self.base_company_state_digest,
            self.runtime_compatibility_digest,
        )
        if (
            self.schema_version != WORKFLOW_PATCH_PROMOTION_ENVELOPE_SCHEMA
            or any(_IDENTIFIER.fullmatch(str(item)) is None for item in identifiers)
            or any(_DIGEST.fullmatch(str(item)) is None for item in digests)
            or self.promotion_id != f"workflow-promotion-{self.content_hash[:24]}"
            or self.content_hash != content_digest(self.content_payload())
            or len(self.source_episode_ids) < 2
            or len(self.source_episode_ids) != len(self.source_episode_content_hashes)
            or len(self.live_run_ids) != 2
            or len(self.live_evidence_ids) != 2
            or len(self.live_evidence_content_hashes) != 2
            or len(set(self.live_run_ids)) != 2
            or len(set(self.live_evidence_ids)) != 2
            or len(self.tasks) < 2
            or self.evidence_count != len(self.source_episode_ids) + 1
            or self.maximum_parallelism < 1
            or self.writer_count != 1
            or self.quality_gain < 0.2 - 1e-9
            or self.automatic_approval
            or self.automatic_apply
        ):
            raise WorkflowPatchPromotionError(
                WorkflowPatchPromotionFailureCode.INVALID_ENVELOPE,
                "promotion envelope invariant failed",
            )


@dataclass(frozen=True, slots=True)
class WorkflowPatchPromotionPreview:
    schema_version: str
    envelope: WorkflowPatchPromotionEnvelope
    candidate: WorkflowPatchCandidate
    proposal_exists: bool
    state_changed: bool
    automatic_approval: bool
    automatic_apply: bool
    external_model_calls: int
    quota_consumed: bool
    checks: tuple[WorkflowPatchPromotionCheck, ...]


@dataclass(frozen=True, slots=True)
class WorkflowPatchPromotionResult:
    preview: WorkflowPatchPromotionPreview
    patch: WorkflowPatchCandidate
    created: bool
    active_company_revision: int
    active_roster_revision: int
    active_playbook_revision: int


PromotionSourceLoader = Callable[
    [Path], tuple[WorkflowPatchPromotionEvidence, Path]
]


def _default_source_loader(
    directory: Path,
) -> tuple[WorkflowPatchPromotionEvidence, Path]:
    from dynamic_firm.evaluation.exact_context_live_pair import (
        load_exact_context_workflow_patch_promotion_source,
    )

    return load_exact_context_workflow_patch_promotion_source(directory)


def _workflow_state_digest(
    store: CompanyStateStore,
    *,
    ignored_pattern_id: str,
) -> str:
    patches = tuple(
        patch
        for patch in store.list_patches()
        if patch.pattern.pattern_id != ignored_pattern_id
    )
    patch_payloads: list[Mapping[str, object]] = []
    for patch in patches:
        try:
            contract: object = store.get_observation_contract(patch.patch_id)
            observations: object = store.list_observations(patch.patch_id)
            assessments: object = store.list_assessments(patch.patch_id)
        except KeyError:
            contract = None
            observations = ()
            assessments = ()
        patch_payloads.append(
            {
                "patch": patch,
                "events": store.list_patch_events(patch.patch_id),
                "observation_contract": contract,
                "observations": observations,
                "assessments": assessments,
            }
        )
    payload = {
        "schema": "noruct.workflow-patch-promotion-company-base.v1",
        "company": store.company(),
        "roster": store.roster(),
        "playbook": store.playbook(),
        "episodes": store.list_episodes(),
        "workflow_patches": tuple(patch_payloads),
        "verified_live_pairs": store.list_live_evidence_pairs(),
    }
    return content_digest(payload)


def _source_patch(
    store: CompanyStateStore,
    pattern_id: str,
) -> tuple[WorkflowPatchCandidate, tuple[OrganizationEpisode, ...]]:
    patch = store.find_applied_patch_for_pattern(pattern_id)
    active = tuple(
        item for item in store.playbook().patterns if item.pattern_id == pattern_id
    )
    if (
        patch is None
        or patch.status != WorkflowPatchStatus.APPLIED
        or len(active) != 1
        or active[0] != patch.pattern
    ):
        raise WorkflowPatchPromotionError(
            WorkflowPatchPromotionFailureCode.SOURCE_PATTERN_DRIFT,
            "source pattern is not the exact active applied patch",
        )
    episodes = tuple(store.get_episode(item) for item in patch.evidence_episode_ids)
    if (
        len(episodes) < 2
        or not all(item.production_eligible for item in episodes)
        or not all(item.safety_passed and item.effect_passed for item in episodes)
        or any(item.plan_template != patch.pattern.tasks for item in episodes)
        or any(item.plan_digest != patch.pattern.plan_digest for item in episodes)
    ):
        raise WorkflowPatchPromotionError(
            WorkflowPatchPromotionFailureCode.SOURCE_PATTERN_DRIFT,
            "source patch evidence no longer replays",
        )
    return patch, episodes


def _candidate_payload(
    *,
    base_playbook_revision: int,
    pattern: WorkflowPattern,
    evidence_episode_ids: tuple[str, ...],
    expected_quality_gain: float,
    expected_model_call_savings: int,
    confidence: float,
    envelope: WorkflowPatchPromotionEnvelope,
) -> Mapping[str, object]:
    return {
        "base_playbook_revision": base_playbook_revision,
        "pattern": pattern,
        "evidence_episode_ids": evidence_episode_ids,
        "expected_quality_gain": expected_quality_gain,
        "expected_model_call_savings": expected_model_call_savings,
        "confidence": confidence,
        "eligible_for_apply": True,
        "ineligibility_reasons": (),
        "promotion_id": envelope.promotion_id,
        "promotion_content_hash": envelope.content_hash,
    }


def _promoted_candidate(
    envelope: WorkflowPatchPromotionEnvelope,
) -> WorkflowPatchCandidate:
    pattern = WorkflowPattern(
        pattern_id=envelope.target_pattern_id,
        task_family=envelope.task_family,
        context_fingerprint=envelope.target_context_fingerprint,
        execution_profile=envelope.execution_profile,
        plan_digest=envelope.source_plan_digest,
        tasks=envelope.tasks,
        maximum_parallelism=envelope.maximum_parallelism,
        writer_count=envelope.writer_count,
        evidence_count=envelope.evidence_count,
        rationale=(
            "A verified source workflow and an exact production-context live pair "
            "support this advisory post-gap Compiler prior."
        ),
    )
    confidence = round(min(0.95, 0.6 + envelope.evidence_count * 0.1), 2)
    immutable = _candidate_payload(
        base_playbook_revision=envelope.base_playbook_revision,
        pattern=pattern,
        evidence_episode_ids=envelope.source_episode_ids,
        expected_quality_gain=envelope.quality_gain,
        expected_model_call_savings=-envelope.model_call_delta,
        confidence=confidence,
        envelope=envelope,
    )
    digest = content_digest(immutable)
    now = utc_now().isoformat()
    return WorkflowPatchCandidate(
        patch_id=f"workflow-patch-{digest[:24]}",
        status=WorkflowPatchStatus.PROPOSED,
        base_playbook_revision=envelope.base_playbook_revision,
        pattern=pattern,
        evidence_episode_ids=envelope.source_episode_ids,
        expected_quality_gain=envelope.quality_gain,
        expected_model_call_savings=-envelope.model_call_delta,
        confidence=confidence,
        eligible_for_apply=True,
        ineligibility_reasons=(),
        content_hash=digest,
        created_at=now,
        updated_at=now,
    )


def promotion_envelope_from_events(
    events: tuple[WorkflowPatchEvent, ...],
) -> WorkflowPatchPromotionEnvelope | None:
    proposed = tuple(
        event for event in events if event.event_type == WorkflowPatchEventType.PROPOSED
    )
    if len(proposed) != 1:
        return None
    raw = proposed[0].payload.get("promotion_envelope")
    if not isinstance(raw, Mapping):
        return None
    return WorkflowPatchPromotionEnvelope.from_mapping(raw)


def replay_promoted_candidate(
    candidate: WorkflowPatchCandidate,
    episodes: tuple[OrganizationEpisode, ...],
    envelope: WorkflowPatchPromotionEnvelope,
) -> bool:
    try:
        envelope.validate()
    except WorkflowPatchPromotionError:
        return False
    if (
        candidate.evidence_episode_ids != envelope.source_episode_ids
        or tuple(item.episode_id for item in episodes) != envelope.source_episode_ids
        or tuple(content_digest(item.content_payload()) for item in episodes)
        != envelope.source_episode_content_hashes
        or not all(item.safety_passed and item.effect_passed for item in episodes)
    ):
        return False
    replayed = _promoted_candidate(envelope)
    return (
        replayed.patch_id == candidate.patch_id
        and replayed.content_hash == candidate.content_hash
        and replayed.pattern == candidate.pattern
        and replayed.evidence_episode_ids == candidate.evidence_episode_ids
    )


class WorkflowPatchPromotionService:
    """Promote one exact-context evidence lineage to PROPOSED only."""

    def __init__(
        self,
        store: CompanyStateStore,
        *,
        source_loader: PromotionSourceLoader = _default_source_loader,
    ) -> None:
        self.store = store
        self.source_loader = source_loader

    @staticmethod
    def _validate_evidence(evidence: WorkflowPatchPromotionEvidence) -> None:
        payload = evidence.content_payload()
        if (
            evidence.schema_version != WORKFLOW_PATCH_PROMOTION_EVIDENCE_SCHEMA
            or evidence.content_hash != content_digest(payload)
            or not evidence.pair_gate_passed
            or not evidence.proposal_recommended
            or evidence.automatic_approval
            or evidence.eligible_for_apply
            or evidence.external_model_calls != 0
            or evidence.quota_consumed
            or len(evidence.run_ids) != 2
            or len(set(evidence.run_ids)) != 2
            or len(evidence.live_evidence_ids) != 2
            or len(set(evidence.live_evidence_ids)) != 2
            or len(evidence.live_evidence_content_hashes) != 2
            or any(_DIGEST.fullmatch(item) is None for item in (
                evidence.content_hash,
                evidence.manifest_content_hash,
                evidence.comparison_content_hash,
                evidence.comparison_file_sha256,
                evidence.binding_content_hash,
                evidence.preparation_content_hash,
                evidence.distribution_sha256,
                evidence.parent_semantic_anchor,
                evidence.parent_company_state_sha256,
                evidence.workload_hash,
                *evidence.live_evidence_content_hashes,
                evidence.runtime_compatibility_digest,
            ))
        ):
            raise WorkflowPatchPromotionError(
                WorkflowPatchPromotionFailureCode.INVALID_EVIDENCE,
                "frozen exact-context evidence projection is invalid",
            )

    @staticmethod
    def _validate_revisions(
        store: CompanyStateStore,
        evidence: WorkflowPatchPromotionEvidence,
    ) -> None:
        if (
            store.company().revision != evidence.company_revision
            or store.roster().revision != evidence.roster_revision
            or store.playbook().revision != evidence.playbook_revision
        ):
            raise WorkflowPatchPromotionError(
                WorkflowPatchPromotionFailureCode.COMPANY_STATE_DRIFT,
                "COMPANY, ROSTER, or PLAYBOOK revision changed",
            )

    def preview(self, directory: str | Path) -> WorkflowPatchPromotionPreview:
        root = Path(directory).expanduser().resolve()
        evidence, parent_database = self.source_loader(root)
        if self.store.path == parent_database.resolve():
            # The parent database is part of the sealed evidence lineage.  A
            # proposal is a legitimate Company mutation, but it must occur in
            # an operator-selected Company state (normally a copy or active
            # state that semantically matches the parent), never in the
            # artifact that later verifies the pair's immutable parent hash.
            raise WorkflowPatchPromotionError(
                WorkflowPatchPromotionFailureCode.IMMUTABLE_PARENT_TARGET,
                "promotion target must not be the immutable evidence parent database",
            )
        self._validate_evidence(evidence)
        self._validate_revisions(self.store, evidence)

        with CompanyStateStore(parent_database) as parent:
            self._validate_revisions(parent, evidence)
            parent_patch, parent_episodes = _source_patch(
                parent, evidence.parent_pattern_id
            )
            parent_digest = _workflow_state_digest(
                parent,
                ignored_pattern_id=evidence.bound_pattern_id,
            )

        source_patch, source_episodes = _source_patch(
            self.store, evidence.parent_pattern_id
        )
        current_digest = _workflow_state_digest(
            self.store,
            ignored_pattern_id=evidence.bound_pattern_id,
        )
        if (
            parent_digest != current_digest
            or parent_patch != source_patch
            or parent_episodes != source_episodes
        ):
            raise WorkflowPatchPromotionError(
                WorkflowPatchPromotionFailureCode.PARENT_COMPANY_DRIFT,
                "target Company is not the immutable parent semantic state",
            )
        if (
            source_patch.pattern.pattern_id != evidence.parent_pattern_id
            or source_patch.pattern.task_family == ""
            or source_patch.pattern.execution_profile != "READ_ONLY"
            or source_patch.pattern.plan_digest != content_digest(source_patch.pattern.tasks)
        ):
            raise WorkflowPatchPromotionError(
                WorkflowPatchPromotionFailureCode.SOURCE_PATTERN_DRIFT,
                "source pattern topology or profile changed",
            )
        if any(
            pattern.pattern_id == evidence.bound_pattern_id
            for pattern in self.store.playbook().patterns
        ):
            raise WorkflowPatchPromotionError(
                WorkflowPatchPromotionFailureCode.TARGET_PATTERN_CONFLICT,
                "target bound pattern is already active",
            )

        envelope = WorkflowPatchPromotionEnvelope.create(
            source_patch_id=source_patch.patch_id,
            source_pattern_id=source_patch.pattern.pattern_id,
            source_context_fingerprint=source_patch.pattern.context_fingerprint,
            source_plan_digest=source_patch.pattern.plan_digest,
            source_episode_ids=source_patch.evidence_episode_ids,
            source_episode_content_hashes=tuple(
                content_digest(item.content_payload()) for item in source_episodes
            ),
            binding_id=evidence.binding_id,
            binding_content_hash=evidence.binding_content_hash,
            preparation_id=evidence.preparation_id,
            preparation_content_hash=evidence.preparation_content_hash,
            pair_id=evidence.pair_id,
            manifest_content_hash=evidence.manifest_content_hash,
            comparison_content_hash=evidence.comparison_content_hash,
            comparison_file_sha256=evidence.comparison_file_sha256,
            live_run_ids=evidence.run_ids,
            live_evidence_ids=evidence.live_evidence_ids,
            live_evidence_content_hashes=evidence.live_evidence_content_hashes,
            source_revision=evidence.source_revision,
            distribution_sha256=evidence.distribution_sha256,
            model_id=evidence.model_id,
            authority_profile=evidence.authority_profile,
            workload_hash=evidence.workload_hash,
            target_context_fingerprint=evidence.production_context_fingerprint,
            target_pattern_id=evidence.bound_pattern_id,
            task_family=source_patch.pattern.task_family,
            execution_profile=source_patch.pattern.execution_profile,
            tasks=source_patch.pattern.tasks,
            maximum_parallelism=source_patch.pattern.maximum_parallelism,
            writer_count=source_patch.pattern.writer_count,
            evidence_count=len(source_patch.evidence_episode_ids) + 1,
            quality_gain=evidence.quality_gain,
            model_call_delta=evidence.model_call_delta,
            repair_delta=evidence.repair_delta,
            token_delta=evidence.token_delta,
            base_company_revision=evidence.company_revision,
            base_roster_revision=evidence.roster_revision,
            base_playbook_revision=evidence.playbook_revision,
            base_company_state_digest=current_digest,
            runtime_compatibility_digest=evidence.runtime_compatibility_digest,
            automatic_approval=False,
            automatic_apply=False,
        )
        envelope.validate()
        candidate = _promoted_candidate(envelope)
        matching = tuple(
            patch
            for patch in self.store.list_patches()
            if patch.pattern.pattern_id == evidence.bound_pattern_id
        )
        if len(matching) > 1:
            raise WorkflowPatchPromotionError(
                WorkflowPatchPromotionFailureCode.TARGET_PATTERN_CONFLICT,
                "target pattern has multiple historical proposals",
            )
        existing = matching[0] if matching else None
        if existing is not None and existing.status not in {
            WorkflowPatchStatus.PROPOSED,
            WorkflowPatchStatus.APPROVED,
        }:
            raise WorkflowPatchPromotionError(
                WorkflowPatchPromotionFailureCode.TARGET_PATTERN_CONFLICT,
                f"target pattern proposal is already {existing.status.value}",
            )
        if existing is not None and existing.content_hash != candidate.content_hash:
            raise WorkflowPatchPromotionError(
                WorkflowPatchPromotionFailureCode.TARGET_PATTERN_CONFLICT,
                "target pattern already has a different open proposal",
            )
        if existing is not None:
            try:
                stored_envelope = promotion_envelope_from_events(
                    self.store.list_patch_events(existing.patch_id)
                )
            except ValueError as exc:
                raise WorkflowPatchPromotionError(
                    WorkflowPatchPromotionFailureCode.TARGET_PATTERN_CONFLICT,
                    "existing proposal promotion envelope is invalid",
                ) from exc
            if (
                stored_envelope != envelope
                or not replay_promoted_candidate(
                    existing,
                    source_episodes,
                    envelope,
                )
            ):
                raise WorkflowPatchPromotionError(
                    WorkflowPatchPromotionFailureCode.TARGET_PATTERN_CONFLICT,
                    "existing proposal does not replay from the exact envelope",
                )
            candidate = existing
        checks = (
            WorkflowPatchPromotionCheck(
                "frozen-pair-lineage-and-gates",
                True,
                f"pair={evidence.pair_id}; comparison={evidence.comparison_content_hash}",
            ),
            WorkflowPatchPromotionCheck(
                "immutable-parent-and-current-company-base",
                True,
                f"company=r{evidence.company_revision}; playbook=r{evidence.playbook_revision}",
            ),
            WorkflowPatchPromotionCheck(
                "source-pattern-and-exact-bound-topology",
                True,
                f"{evidence.parent_pattern_id}->{evidence.bound_pattern_id}",
            ),
            WorkflowPatchPromotionCheck(
                "proposal-only-authority",
                True,
                "automatic approval=false; automatic apply=false",
            ),
        )
        return WorkflowPatchPromotionPreview(
            schema_version=WORKFLOW_PATCH_PROMOTION_PREVIEW_SCHEMA,
            envelope=envelope,
            candidate=candidate,
            proposal_exists=existing is not None,
            state_changed=False,
            automatic_approval=False,
            automatic_apply=False,
            external_model_calls=0,
            quota_consumed=False,
            checks=checks,
        )

    def promote(
        self,
        directory: str | Path,
        *,
        actor: str,
    ) -> WorkflowPatchPromotionResult:
        if not actor.strip():
            raise ValueError("Workflow Patch promotion actor must be explicit")
        preview = self.preview(directory)
        if preview.proposal_exists:
            patch = self.store.get_patch(preview.candidate.patch_id)
            created = False
        else:
            patch, created = self.store.create_candidate(
                preview.candidate,
                actor=actor,
                proposal_payload={
                    "base_company_revision": preview.envelope.base_company_revision,
                    "base_roster_revision": preview.envelope.base_roster_revision,
                    "base_playbook_revision": preview.envelope.base_playbook_revision,
                    "eligible_for_apply": True,
                    "evidence_episode_ids": preview.envelope.source_episode_ids,
                    "promotion_envelope": preview.envelope,
                },
                reject_open_pattern_conflict=True,
                expected_state_revisions=(
                    preview.envelope.base_company_revision,
                    preview.envelope.base_roster_revision,
                    preview.envelope.base_playbook_revision,
                ),
            )
        return WorkflowPatchPromotionResult(
            preview=preview,
            patch=patch,
            created=created,
            active_company_revision=self.store.company().revision,
            active_roster_revision=self.store.roster().revision,
            active_playbook_revision=self.store.playbook().revision,
        )
