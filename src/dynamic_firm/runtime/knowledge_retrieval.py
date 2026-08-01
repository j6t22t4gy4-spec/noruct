from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Mapping
from typing import Sequence

from .models import VersionedContent
from dynamic_firm.korean_lexical import korean_retrieval_variants


_TERM = re.compile(r"[\w.-]{2,}", re.UNICODE)


def _is_cjk(character: str) -> bool:
    codepoint = ord(character)
    return (
        0x3400 <= codepoint <= 0x9FFF  # CJK unified ideographs
        or 0xAC00 <= codepoint <= 0xD7A3  # Hangul syllables
        or 0x3040 <= codepoint <= 0x30FF  # Hiragana / Katakana
    )


def _terms(value: str) -> tuple[str, ...]:
    """Return lexical terms plus CJK bigrams without a language dependency."""

    normalized = {
        item.strip("._-").casefold()
        for item in _TERM.findall(value)
        if len(item.strip("._-")) >= 2
    }
    run: list[str] = []
    for character in value.casefold():
        if _is_cjk(character):
            run.append(character)
            continue
        if len(run) >= 2:
            normalized.update("".join(run[index : index + 2]) for index in range(len(run) - 1))
        run.clear()
    if len(run) >= 2:
        normalized.update("".join(run[index : index + 2]) for index in range(len(run) - 1))
    # Keep original Hangul surface terms while adding bounded transparent
    # postposition/compound-connector variants. This is only lexical recall
    # expansion; it does not infer a lemma or overwrite the query retained in
    # an Evidence Pack.
    for variant in korean_retrieval_variants(value):
        if len(variant) >= 2:
            normalized.add(variant.casefold())
        if len(variant) >= 2:
            normalized.update(
                variant[index : index + 2].casefold()
                for index in range(len(variant) - 1)
            )
    return tuple(sorted(normalized))


def _bm25_score(
    query_terms: tuple[str, ...],
    document_terms: tuple[str, ...],
    *,
    document_frequency: dict[str, int],
    document_count: int,
    average_length: float,
) -> float:
    if not document_terms:
        return 0.0
    term_frequency: dict[str, int] = {}
    for term in document_terms:
        term_frequency[term] = term_frequency.get(term, 0) + 1
    score = 0.0
    length = len(document_terms)
    for term in query_terms:
        frequency = term_frequency.get(term, 0)
        if not frequency:
            continue
        document_frequency_value = document_frequency.get(term, 0)
        inverse_frequency = math.log(
            1 + (document_count - document_frequency_value + 0.5)
            / (document_frequency_value + 0.5)
        )
        score += inverse_frequency * (
            frequency * 2.5
            / (frequency + 1.5 * (1 - 0.75 + 0.75 * length / max(average_length, 1.0)))
        )
    return score


@dataclass(frozen=True, slots=True)
class KnowledgeSelection:
    items: tuple[VersionedContent, ...]
    candidate_count: int
    selected_bytes: int
    explanations: tuple["KnowledgeRetrievalExplanation", ...] = ()

    def explanation_for(self, content_id: str) -> "KnowledgeRetrievalExplanation | None":
        return next(
            (item for item in self.explanations if item.content_id == content_id),
            None,
        )


@dataclass(frozen=True, slots=True)
class KnowledgeRetrievalExplanation:
    """Bounded, non-authoritative reasons for one lexical retrieval choice.

    This is deliberately a selection disclosure, not a statement of truth.  The
    source's epistemic metadata remains visible to the caller and is never
    converted into Company or Employee authority.
    """

    content_id: str
    filename_path_score: float
    phrase_score: float
    body_score: float
    freshness_score: float
    trust_score: float
    conflict_penalty: float
    total_score: float
    basis: tuple[str, ...]


def _metadata_text(metadata: Mapping[str, object], *keys: str) -> str:
    return " ".join(str(metadata.get(key) or "") for key in keys).strip()


def _phrase_score(query: str, title_path: str, content: str) -> tuple[float, tuple[str, ...]]:
    normalized = " ".join(query.casefold().split())
    if len(normalized) < 2:
        return 0.0, ()
    basis: list[str] = []
    score = 0.0
    if normalized in " ".join(title_path.casefold().split()):
        score += 4.0
        basis.append("exact_phrase:title_or_path")
    if normalized in " ".join(content.casefold().split()):
        score += 1.0
        basis.append("exact_phrase:body")
    return score, tuple(basis)


def _freshness_score(value: object, *, now: datetime) -> tuple[float, tuple[str, ...]]:
    if not value:
        return 0.0, ("freshness:unspecified",)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        current = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    except ValueError:
        return -0.05, ("freshness:invalid",)
    if parsed < current:
        return -0.35, ("freshness:expired",)
    return 0.10, ("freshness:current",)


def _trust_score(value: object) -> tuple[float, tuple[str, ...]]:
    trust = str(value or "UNSPECIFIED")
    scores = {
        "TRUSTED_SOURCE": 0.25,
        "USER_ASSERTED": 0.12,
        "DERIVED": 0.04,
        "UNTRUSTED_EXTERNAL": 0.0,
        "MODEL_GENERATED": -0.05,
        "UNSPECIFIED": 0.0,
    }
    return scores.get(trust, 0.0), (f"trust:{trust}",)


def _conflict_penalty(value: object) -> tuple[float, tuple[str, ...]]:
    try:
        count = len(tuple(value or ()))
    except TypeError:
        count = 1
    if not count:
        return 0.0, ()
    return -min(0.30, count * 0.10), (f"conflicts:{count}",)


def _source_authority_score(value: object) -> tuple[float, tuple[str, ...]]:
    """Prefer the current raw-folder citation over its derived duplicate.

    This only affects selection; a folder entry is still untrusted evidence and
    must be snapshotted before it crosses the Firm bridge.
    """

    if str(value) == "folder_file":
        return 0.25, ("source:live_folder",)
    return 0.0, ()


class BoundedKnowledgeRetriever:
    """Deterministic, namespace-filtered retrieval for data-only knowledge.

    This follows the registered foundation's preprocess-before-injection shape while preserving
    Noruct's stronger rule: retrieval can select immutable content, but it can
    neither write memory nor expand employee authority.
    """

    revision = "bounded-hybrid-cjk-v4"

    def select(
        self,
        candidates: Sequence[VersionedContent],
        *,
        query: str,
        limit: int = 3,
        max_bytes: int = 12_000,
        allowed_prefixes: tuple[str, ...] = (),
        fallback_count: int = 1,
        allow_partial: bool = False,
        metadata: Mapping[str, Mapping[str, object]] | None = None,
        now: datetime | None = None,
    ) -> KnowledgeSelection:
        if limit < 1 or max_bytes < 1 or fallback_count < 0:
            raise ValueError("Knowledge retrieval bounds must be positive")
        if not isinstance(allow_partial, bool):
            raise ValueError("Knowledge retrieval partial-selection mode must be boolean")
        eligible = tuple(
            item
            for item in candidates
            if not allowed_prefixes
            or any(item.content_id.startswith(prefix) for prefix in allowed_prefixes)
        )
        query_terms = _terms(query)
        candidate_metadata = metadata or {}
        observed_now = now or datetime.now(UTC)
        documents = tuple(
            (
                item,
                candidate_metadata.get(item.content_id, {}),
                _terms(
                    re.sub(
                        r"[./\\\\]+",
                        " ",
                        " ".join(
                        (
                            item.content_id.replace(":", " "),
                            _metadata_text(candidate_metadata.get(item.content_id, {}), "title", "path"),
                        )
                        ),
                    )
                ),
                _terms(item.content),
            )
            for item in eligible
        )
        document_frequency: dict[str, int] = {}
        document_lengths: list[int] = []
        for _, _, identity_terms, content_terms in documents:
            combined = (*identity_terms, *content_terms)
            document_lengths.append(len(combined))
            for term in set(combined):
                document_frequency[term] = document_frequency.get(term, 0) + 1
        average_length = sum(document_lengths) / max(len(document_lengths), 1)
        ranked: list[tuple[float, str, str, VersionedContent, KnowledgeRetrievalExplanation]] = []
        for item, item_metadata, identity_terms, content_terms in documents:
            filename_path_score = 3 * _bm25_score(
                    query_terms,
                    identity_terms,
                    document_frequency=document_frequency,
                    document_count=len(documents),
                    average_length=average_length,
                )
            body_score = _bm25_score(
                    query_terms,
                    content_terms,
                    document_frequency=document_frequency,
                    document_count=len(documents),
                    average_length=average_length,
                )
            title_path = _metadata_text(item_metadata, "title", "path")
            phrase_score, phrase_basis = _phrase_score(query, title_path, item.content)
            freshness_score, freshness_basis = _freshness_score(
                item_metadata.get("freshness_expires_at"), now=observed_now
            )
            trust_score, trust_basis = _trust_score(item_metadata.get("trust_class"))
            conflict_penalty, conflict_basis = _conflict_penalty(
                item_metadata.get("conflict_refs")
            )
            source_score, source_basis = _source_authority_score(
                item_metadata.get("source_type")
            )
            score = (
                filename_path_score
                + phrase_score
                + body_score
                + freshness_score
                + trust_score
                + conflict_penalty
                + source_score
            )
            basis = tuple(
                item
                for item in (
                    *("filename_or_path" for _ in [0] if filename_path_score > 0),
                    *phrase_basis,
                    *("body" for _ in [0] if body_score > 0),
                    *freshness_basis,
                    *trust_basis,
                    *conflict_basis,
                    *source_basis,
                )
            )
            explanation = KnowledgeRetrievalExplanation(
                content_id=item.content_id,
                filename_path_score=round(filename_path_score, 6),
                phrase_score=round(phrase_score, 6),
                body_score=round(body_score, 6),
                freshness_score=round(freshness_score, 6),
                trust_score=round(trust_score, 6),
                conflict_penalty=round(conflict_penalty, 6),
                total_score=round(score, 6),
                basis=basis,
            )
            ranked.append((-score, item.content_id, item.revision, item, explanation))
        ranked.sort(key=lambda row: row[:3])

        selected: list[VersionedContent] = []
        explanations: list[KnowledgeRetrievalExplanation] = []
        selected_bytes = 0
        for negative_score, _, _, item, explanation in ranked:
            if len(selected) >= limit:
                break
            if negative_score == 0 and len(selected) >= fallback_count:
                continue
            size = len(item.content.encode("utf-8"))
            remaining = max_bytes - selected_bytes
            if size > remaining and not allow_partial:
                continue
            if remaining <= 0:
                break
            selected.append(item)
            explanations.append(explanation)
            selected_bytes += min(size, remaining)
        return KnowledgeSelection(
            tuple(selected), len(eligible), selected_bytes, tuple(explanations)
        )
