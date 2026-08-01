"""Bounded, content-free Knowledge lineage projection for operator surfaces."""

from __future__ import annotations

from typing import Any, Mapping


KNOWLEDGE_LINEAGE_SCHEMA = "noruct.knowledge-lineage.v1"


def build_knowledge_lineage(
    store: Any,
    *,
    job_id: str | None = None,
    intent_id: str | None = None,
    limit: int = 100,
) -> Mapping[str, object]:
    """Project durable provenance joins without reading or writing content."""

    if limit < 1 or limit > 500:
        raise ValueError("Knowledge lineage limit must be between 1 and 500")
    selected_job = _identifier(job_id, "job_id") if job_id is not None else None
    selected_intent = _identifier(intent_id, "intent_id") if intent_id is not None else None
    bindings = store.list_execution_bindings(limit=limit)
    if selected_job is not None:
        bindings = tuple(item for item in bindings if item.job_id == selected_job)
    if selected_intent is not None:
        bindings = tuple(item for item in bindings if item.intent_id == selected_intent)
    jobs = {item.job_id for item in bindings}
    if selected_job is not None:
        jobs.add(selected_job)
    intents = {item.intent_id for item in bindings}
    if selected_intent is not None:
        intents.add(selected_intent)
    candidates = tuple(item for item in store.list_write_candidates(limit=limit) if item.job_id in jobs)
    candidate_ids = {item.candidate_id for item in candidates}
    records = tuple(
        item for item in store.list_records(limit=limit, include_superseded=True)
        if item.source_job_id in jobs or item.source_candidate_id in candidate_ids
    )
    outcomes = tuple(item for item in store.list_outcomes(limit=limit) if item.job_id in jobs)
    packs = {item.pack_id for item in bindings}
    decisions = tuple(
        item for item in store.list_decisions(limit=limit)
        if item.intent_id in intents or item.evidence_pack_id in packs
    )
    node_map: dict[str, Mapping[str, object]] = {}
    edges: set[tuple[str, str, str]] = set()

    def node(identifier: str, kind: str, **facts: object) -> None:
        node_map.setdefault(identifier, {"id": identifier, "kind": kind, **facts})

    for binding in bindings:
        binding_node = f"binding:{binding.binding_id}"
        intent_node = f"intent:{binding.intent_id}"
        job_node = f"job:{binding.job_id}"
        pack_node = f"evidence-pack:{binding.pack_id}"
        node(binding_node, "EXECUTION_BINDING", status=binding.status, job_status=binding.job_status)
        node(intent_node, "INTENT", revision=binding.intent_revision)
        node(job_node, "JOB")
        node(pack_node, "EVIDENCE_PACK", revision=binding.pack_revision, digest=binding.pack_digest)
        edges.update({(intent_node, binding_node, "EXECUTES"), (pack_node, binding_node, "CONTEXT_FOR"), (binding_node, job_node, "BINDS")})
        if binding.candidate_id is not None:
            edges.add((binding_node, f"candidate:{binding.candidate_id}", "PRODUCES"))
    for candidate in candidates:
        candidate_node = f"candidate:{candidate.candidate_id}"
        node(candidate_node, "WRITE_CANDIDATE", status=candidate.status, kind_name=candidate.kind)
        node(f"job:{candidate.job_id}", "JOB")
        edges.add((f"job:{candidate.job_id}", candidate_node, "PRODUCES"))
        if candidate.evidence_pack_id is not None:
            node(f"evidence-pack:{candidate.evidence_pack_id}", "EVIDENCE_PACK")
            edges.add((f"evidence-pack:{candidate.evidence_pack_id}", candidate_node, "SUPPORTS"))
        if candidate.accepted_record_id is not None:
            edges.add((candidate_node, f"record:{candidate.accepted_record_id}", "ACCEPTED_AS"))
    for record in records:
        record_node = f"record:{record.record_id}"
        node(record_node, "KNOWLEDGE_RECORD", status=record.status, revision=record.revision, kind_name=record.kind)
        if record.source_candidate_id is not None:
            edges.add((f"candidate:{record.source_candidate_id}", record_node, "ACCEPTED_AS"))
        if record.source_job_id is not None:
            node(f"job:{record.source_job_id}", "JOB")
            edges.add((f"job:{record.source_job_id}", record_node, "ACCEPTED_RESULT"))
        if record.evidence_pack_id is not None:
            node(f"evidence-pack:{record.evidence_pack_id}", "EVIDENCE_PACK")
            edges.add((f"evidence-pack:{record.evidence_pack_id}", record_node, "SUPPORTS"))
    for outcome in outcomes:
        outcome_node = f"outcome:{outcome.outcome_id}"
        node(outcome_node, "OUTCOME", verdict=outcome.verdict.value, attribution=outcome.attribution_status.value)
        node(f"job:{outcome.job_id}", "JOB")
        edges.update({(f"job:{outcome.job_id}", outcome_node, "OBSERVED_AS"), (f"binding:{outcome.binding_id}", outcome_node, "EVALUATED_BY")})
    for decision in decisions:
        decision_node = f"decision:{decision.decision_id}"
        node(decision_node, "DECISION", status=decision.status.value, revision=decision.revision)
        if decision.intent_id is not None:
            node(f"intent:{decision.intent_id}", "INTENT")
            edges.add((f"intent:{decision.intent_id}", decision_node, "INFORMS"))
        if decision.evidence_pack_id is not None:
            node(f"evidence-pack:{decision.evidence_pack_id}", "EVIDENCE_PACK")
            edges.add((f"evidence-pack:{decision.evidence_pack_id}", decision_node, "SUPPORTS"))
        if decision.supersedes_decision_id is not None:
            edges.add((f"decision:{decision.supersedes_decision_id}", decision_node, "SUPERSEDED_BY"))
    return {
        "schema": KNOWLEDGE_LINEAGE_SCHEMA,
        "job_id": selected_job,
        "intent_id": selected_intent,
        "nodes": tuple(node_map[key] for key in sorted(node_map)),
        "edges": tuple(
            {"from": source, "to": target, "relation": relation}
            for source, target, relation in sorted(edges)
            if source in node_map and target in node_map
        ),
        "truncated": any(len(items) >= limit for items in (bindings, candidates, records, outcomes, decisions)),
        "network_request_performed": False,
    }


def _identifier(value: str, label: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 256:
        raise ValueError(f"Knowledge lineage {label} is invalid")
    return normalized
