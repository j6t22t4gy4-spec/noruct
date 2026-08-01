from __future__ import annotations

from dataclasses import dataclass

from dynamic_firm.kernel.models import ExecutionOriginBinding
from dynamic_firm.runtime.models import TaskEvidencePack

from .models import (
    EvidencePack,
    IntentRecord,
    IntentStatus,
    KnowledgeExecutionBinding,
    KnowledgeExecutionOutcome,
    KnowledgeWriteCandidate,
)
from .epistemic import (
    DecisionContextSnapshot,
    OracleContract,
    OracleValidatorType,
    OutcomeObservation,
    ValidatorIndependence,
)
from .service import UserKnowledgeService
from .delivery import runtime_delivery_from_evidence_pack


@dataclass(frozen=True, slots=True)
class PreparedKnowledgeExecution:
    """Frozen, verified input for exactly one Intent-originated Firm Job."""

    intent: IntentRecord
    evidence_pack: EvidencePack
    task_evidence: TaskEvidencePack
    execution_origin: ExecutionOriginBinding
    binding: KnowledgeExecutionBinding
    decision_context: DecisionContextSnapshot
    oracle_contract: OracleContract


@dataclass(frozen=True, slots=True)
class CompletedKnowledgeExecution:
    binding: KnowledgeExecutionBinding
    candidate: KnowledgeWriteCandidate | None
    outcome: OutcomeObservation


def _bounded_result(value: str, maximum: int = 64_000) -> str:
    payload = value.strip().encode("utf-8")
    if len(payload) <= maximum:
        return payload.decode("utf-8")
    marker = "\n\n[Result truncated at the Knowledge Write Candidate boundary.]".encode("utf-8")
    clipped = payload[: maximum - len(marker)]
    while clipped:
        try:
            return clipped.decode("utf-8").rstrip() + marker.decode("utf-8")
        except UnicodeDecodeError:
            clipped = clipped[:-1]
    return ""


class KnowledgeFirmBridge:
    """One-way, bounded bridge; Knowledge DB remains outside Firm/employee state."""

    def __init__(self, service: UserKnowledgeService) -> None:
        self.service = service

    def prepare(
        self,
        intent_id: str,
        *,
        request_id: str,
        job_id: str,
        access_scope: str = "private",
        evidence_limit: int = 6,
        evidence_max_bytes: int = 16_000,
        owner_ref: str = "user",
        authority_ref: str = "company-authority:local",
        assumptions: tuple[str, ...] = (),
        unknown_refs: tuple[str, ...] = (),
        excluded_alternatives: tuple[str, ...] = (),
        failure_criteria: tuple[str, ...] = (),
        observable_signals: tuple[str, ...] = (),
        observation_channel: str = "",
        validator_type: OracleValidatorType = OracleValidatorType.UNVERIFIABLE,
        independence_class: ValidatorIndependence = ValidatorIndependence.NONE,
        feedback_due_at: str | None = None,
        reversibility_class: str = "UNKNOWN",
        risk_class: str = "UNKNOWN",
        proxy_metric: str | None = None,
        proxy_failure_modes: tuple[str, ...] = (),
    ) -> PreparedKnowledgeExecution:
        verified_intent = self.service.store.verified_intent(intent_id)
        if verified_intent is None:
            raise ValueError(f"Intent was not found: {intent_id}")
        intent, intent_hash = verified_intent
        if intent.status != IntentStatus.ACTIVE:
            raise ValueError("Only an ACTIVE Intent can start a Firm Job")
        query = intent.knowledge_query.strip() or intent.goal
        pack = self.service.build_evidence_pack(
            query,
            limit=evidence_limit,
            max_bytes=evidence_max_bytes,
            max_excerpt_bytes=min(2400, evidence_max_bytes),
            access_scope=access_scope,
            persist=True,
        )
        pack.verify(maximum_items=evidence_limit, maximum_bytes=evidence_max_bytes)
        task_evidence = runtime_delivery_from_evidence_pack(pack)
        task_evidence.verify(max_items=evidence_limit, max_bytes=evidence_max_bytes)
        binding = self.service.store.prepare_execution_binding(
            request_id=request_id,
            job_id=job_id,
            intent_id=intent.intent_id,
            intent_revision=intent.revision,
            intent_hash=intent_hash,
            pack_id=pack.pack_id,
            pack_revision=pack.revision,
            pack_digest=pack.digest,
            delivery_digest=task_evidence.delivery_digest,
            item_count=len(task_evidence.items),
            selected_bytes=task_evidence.selected_bytes,
            access_scope=pack.access_scope,
        )
        known_refs = tuple(
            dict.fromkeys(
                f"{item.source_type}:{item.source_id}@{item.source_revision}"
                for item in pack.items
            )
        )
        pack_unknowns = tuple(
            dict.fromkeys(
                value
                for item in pack.items
                for value in item.unknown_refs
            )
        )
        decision_context, oracle_contract = self.service.store.ensure_epistemic_admission(
            binding_id=binding.binding_id,
            known_refs=known_refs,
            unknown_refs=tuple(dict.fromkeys((*unknown_refs, *pack_unknowns))),
            assumptions=assumptions,
            constraints=intent.constraints,
            excluded_alternatives=excluded_alternatives,
            owner_ref=owner_ref,
            authority_ref=authority_ref,
            acceptance_criteria=intent.acceptance_criteria,
            failure_criteria=failure_criteria,
            observable_signals=observable_signals,
            observation_channel=observation_channel,
            validator_type=validator_type,
            independence_class=independence_class,
            feedback_due_at=feedback_due_at,
            reversibility_class=reversibility_class,
            risk_class=risk_class,
            proxy_metric=proxy_metric,
            proxy_failure_modes=proxy_failure_modes,
            max_evidence_items=evidence_limit,
        )
        origin = ExecutionOriginBinding(
            binding_id=binding.binding_id,
            intent_id=binding.intent_id,
            intent_revision=binding.intent_revision,
            intent_hash=binding.intent_hash,
            pack_id=binding.pack_id,
            pack_revision=binding.pack_revision,
            pack_digest=binding.pack_digest,
            delivery_digest=binding.delivery_digest,
            item_count=binding.item_count,
            selected_bytes=binding.selected_bytes,
            access_scope=binding.access_scope,
            decision_context_id=decision_context.snapshot_id,
            decision_context_digest=decision_context.content_digest,
            oracle_contract_id=oracle_contract.oracle_contract_id,
            oracle_contract_digest=oracle_contract.content_digest,
        )
        return PreparedKnowledgeExecution(
            intent=intent,
            evidence_pack=pack,
            task_evidence=task_evidence,
            execution_origin=origin,
            binding=binding,
            decision_context=decision_context,
            oracle_contract=oracle_contract,
        )

    def complete(
        self,
        prepared: PreparedKnowledgeExecution,
        outcome: KnowledgeExecutionOutcome,
    ) -> CompletedKnowledgeExecution:
        if not isinstance(outcome, KnowledgeExecutionOutcome):
            raise TypeError(
                "Knowledge bridge completion requires a KnowledgeExecutionOutcome; "
                "full Firm Job or graph state must not cross this boundary"
            )
        if outcome.job_id != prepared.binding.job_id:
            raise ValueError("Firm Job result does not match the Knowledge execution binding")
        statement = _bounded_result(outcome.summary)
        binding, candidate = self.service.store.finalize_execution(
            prepared.binding.binding_id,
            job_status=outcome.status,
            candidate_statement=(
                statement if outcome.status == "SUCCEEDED" else ""
            ),
        )
        observed_outcome = self.service.store.ensure_pending_outcome(
            binding_id=binding.binding_id,
            job_status=outcome.status,
            result_summary=statement,
        )
        return CompletedKnowledgeExecution(
            binding=binding,
            candidate=candidate,
            outcome=observed_outcome,
        )

    def interrupt(self, prepared: PreparedKnowledgeExecution) -> KnowledgeExecutionBinding:
        """Make crash recovery explicit; it never auto-runs a prepared Intent."""

        binding = self.service.store.complete_execution_binding(
            prepared.binding.binding_id,
            job_status="INTERRUPTED",
        )
        self.service.store.ensure_pending_outcome(
            binding_id=binding.binding_id,
            job_status="INTERRUPTED",
            result_summary="",
        )
        return binding
