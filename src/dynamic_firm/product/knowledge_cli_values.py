"""Bounded typed-value helpers shared by Knowledge command surfaces."""

from __future__ import annotations

from dynamic_firm.knowledge import KnowledgeStore


def knowledge_limit(value: int, *, label: str = "limit") -> int:
    if value < 1 or value > 500:
        raise ValueError(f"Knowledge {label} must be between 1 and 500")
    return value


def show_knowledge_value(store: KnowledgeStore, identifier: str) -> object:
    """Resolve one approved typed Knowledge identifier, never an arbitrary table."""

    value: object | None
    if identifier.startswith("asset-"):
        value = store.asset(identifier)
    elif identifier.startswith("record-"):
        value = store.record(identifier)
    elif identifier.startswith("pack-"):
        value = store.evidence_pack(identifier)
    elif identifier.startswith("candidate-"):
        value = store.write_candidate(identifier)
    elif identifier.startswith("intent-"):
        verified = store.verified_intent(identifier)
        intent = verified[0] if verified is not None else None
        value = None if intent is None else {"intent": intent, "history": store.intent_history(identifier)}
    elif identifier.startswith("decision-"):
        verified = store.verified_decision(identifier)
        decision = verified[0] if verified is not None else None
        value = None if decision is None else {"decision": decision, "history": store.decision_history(identifier)}
    elif identifier.startswith("binding-"):
        value = store.execution_binding(identifier)
    elif identifier.startswith("outcome-"):
        value = store.outcome(identifier)
    elif identifier.startswith("question-"):
        verified = store.verified_question(identifier)
        question = verified[0] if verified is not None else None
        value = None if question is None else {"question": question, "history": store.question_history(identifier)}
    elif identifier.startswith("research-"):
        request = store.research_request(identifier)
        value = None if request is None else {"research_request": request, "history": store.research_history(identifier)}
    else:
        raise ValueError("Knowledge identifier prefix is not recognized")
    if value is None:
        raise ValueError(f"Knowledge object was not found: {identifier}")
    return value
