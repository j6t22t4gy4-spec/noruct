"""Conservative, read-only review queue for the user-owned Knowledge space.

This is deliberately not a semantic summarizer. Exact repeat leads require
the *same normalized candidate statement* from at least two distinct Jobs;
separate lexical leads use a deliberately high local-token threshold. A human
still opens and accepts/rejects each source candidate; this projection never
writes a record or a page.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from dynamic_firm.runtime.knowledge_retrieval import _terms

from .page_lint import KnowledgePageLinter
from .store import KnowledgeStore


_REVIEW_LINT_CODES = frozenset(
    {"STALE_PAGE", "CONTESTED_PAGE", "DECLARED_CONTRADICTION"}
)


@dataclass(frozen=True, slots=True)
class KnowledgeSynthesisReview:
    """One repeat-only synthesis lead; it contains no candidate body."""

    fingerprint: str
    kind: str
    candidate_ids: tuple[str, ...]
    job_ids: tuple[str, ...]
    evidence_pack_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class KnowledgeLexicalSimilarityReview:
    """A high-threshold lexical lead, never a semantic or truth claim."""

    fingerprint: str
    kind: str
    candidate_ids: tuple[str, str]
    job_ids: tuple[str, str]
    similarity_basis_points: int
    shared_term_count: int


@dataclass(frozen=True, slots=True)
class KnowledgePageReviewIssue:
    folder_id: str
    code: str
    relative_path: str
    reference: str = ""


@dataclass(frozen=True, slots=True)
class KnowledgeReviewSurface:
    """Bounded review-only state shared by terminal and future GUI surfaces."""

    syntheses: tuple[KnowledgeSynthesisReview, ...]
    lexical_near_duplicates: tuple[KnowledgeLexicalSimilarityReview, ...]
    page_issues: tuple[KnowledgePageReviewIssue, ...]
    truncated: bool


def _normalized_statement(value: str) -> str:
    return " ".join(value.casefold().split())


def _fingerprint(kind: str, statement: str) -> str:
    payload = f"{kind.upper()}\0{_normalized_statement(statement)}".encode("utf-8")
    return "synthesis-" + hashlib.sha256(payload).hexdigest()[:24]


def _lexical_fingerprint(kind: str, candidate_ids: tuple[str, str]) -> str:
    payload = f"{kind.upper()}\0{candidate_ids[0]}\0{candidate_ids[1]}".encode("utf-8")
    return "lexical-" + hashlib.sha256(payload).hexdigest()[:24]


def _lexical_near_duplicates(candidates: list[object], *, limit: int) -> tuple[tuple[KnowledgeLexicalSimilarityReview, ...], bool]:
    """Return only unusually close lexical pairs from separate Jobs.

    This is deliberately a high-precision convenience lead.  It does not use
    embeddings, a model, synonym table, or Korean morphology beyond the same
    bounded local lexical variants already used for Knowledge retrieval.
    """

    maximum_candidates = 256
    truncated = len(candidates) > maximum_candidates
    bounded = candidates[:maximum_candidates]
    token_sets = {item.candidate_id: frozenset(_terms(item.statement)) for item in bounded}
    leads: list[KnowledgeLexicalSimilarityReview] = []
    for index, first in enumerate(bounded):
        first_tokens = token_sets[first.candidate_id]
        if len(first_tokens) < 6:
            continue
        for second in bounded[index + 1 :]:
            if first.kind != second.kind or first.job_id == second.job_id:
                continue
            if _normalized_statement(first.statement) == _normalized_statement(second.statement):
                continue
            second_tokens = token_sets[second.candidate_id]
            if len(second_tokens) < 6:
                continue
            shared = first_tokens & second_tokens
            union = first_tokens | second_tokens
            if len(shared) < 6 or not union:
                continue
            basis_points = len(shared) * 10_000 // len(union)
            if basis_points < 9_000:
                continue
            candidate_ids = tuple(sorted((first.candidate_id, second.candidate_id)))
            job_ids = tuple(sorted((first.job_id, second.job_id)))
            leads.append(
                KnowledgeLexicalSimilarityReview(
                    fingerprint=_lexical_fingerprint(first.kind, candidate_ids),
                    kind=first.kind,
                    candidate_ids=candidate_ids,
                    job_ids=job_ids,
                    similarity_basis_points=basis_points,
                    shared_term_count=len(shared),
                )
            )
    leads.sort(key=lambda item: (-item.similarity_basis_points, item.kind, item.fingerprint))
    return tuple(leads[:limit]), truncated or len(leads) > limit


def _published_page_drift(store: KnowledgeStore, folder_id: str, root: Path) -> tuple[KnowledgePageReviewIssue, ...]:
    """Compare the immutable publication receipt with the current local page.

    This detects user-visible drift without treating it as an error or replacing
    the page.  Missing or unsafe paths are review items as well.
    """

    issues: list[KnowledgePageReviewIssue] = []
    for publication in store.list_page_publications(folder_id=folder_id, limit=1000):
        relative = PurePosixPath(publication.relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            issues.append(KnowledgePageReviewIssue(folder_id, "PUBLISHED_PAGE_PATH_UNSAFE", publication.relative_path))
            continue
        # Publication receipts are relative to the Folder root (for example
        # ``pages/topic.md``), whereas lint paths are relative to ``pages/``.
        target = root.joinpath(*relative.parts)
        try:
            if target.is_symlink() or not target.is_file():
                issues.append(KnowledgePageReviewIssue(folder_id, "PUBLISHED_PAGE_MISSING", publication.relative_path))
                continue
            payload = target.read_bytes()
        except OSError:
            issues.append(KnowledgePageReviewIssue(folder_id, "PUBLISHED_PAGE_UNREADABLE", publication.relative_path))
            continue
        if hashlib.sha256(payload).hexdigest() != publication.content_sha256:
            issues.append(KnowledgePageReviewIssue(folder_id, "PUBLISHED_PAGE_DRIFT", publication.relative_path))
    return tuple(issues)


def build_knowledge_review_surface(store: KnowledgeStore, *, limit: int = 32) -> KnowledgeReviewSurface:
    """Project repeat candidates plus stale/conflicting/drifted pages.

    The result is deliberately conservative: exact normalized text and two
    separate Job identities are prerequisites for a synthesis lead, and lexical
    leads have an independently high local-token threshold. No model, network
    request, Candidate transition, Folder scan, or file write occurs.
    """

    if not 1 <= limit <= 100:
        raise ValueError("Knowledge review limit must be between 1 and 100")
    pending_candidates = list(store.list_write_candidates(status="PENDING", limit=1000))
    grouped: dict[tuple[str, str], list[object]] = {}
    for candidate in pending_candidates:
        normalized = _normalized_statement(candidate.statement)
        if normalized:
            grouped.setdefault((candidate.kind, normalized), []).append(candidate)
    syntheses: list[KnowledgeSynthesisReview] = []
    for (kind, statement), grouped_candidates in grouped.items():
        job_ids = tuple(sorted({item.job_id for item in grouped_candidates}))
        if len(job_ids) < 2:
            continue
        syntheses.append(
            KnowledgeSynthesisReview(
                fingerprint=_fingerprint(kind, statement),
                kind=kind,
                candidate_ids=tuple(sorted(item.candidate_id for item in grouped_candidates)),
                job_ids=job_ids,
                evidence_pack_ids=tuple(sorted({item.evidence_pack_id for item in grouped_candidates if item.evidence_pack_id})),
            )
        )
    syntheses.sort(key=lambda item: (item.kind, item.fingerprint))
    lexical_near_duplicates, lexical_truncated = _lexical_near_duplicates(
        pending_candidates, limit=limit
    )

    page_issues: list[KnowledgePageReviewIssue] = []
    truncated = len(syntheses) > limit or lexical_truncated
    for folder in store.list_knowledge_folders(limit=100):
        try:
            report = KnowledgePageLinter(store).lint(folder_id=folder.folder_id, max_pages=limit * 10)
        except (OSError, ValueError):
            page_issues.append(KnowledgePageReviewIssue(folder.folder_id, "PAGE_REVIEW_UNAVAILABLE", ""))
            continue
        truncated = truncated or report.truncated
        page_issues.extend(
            KnowledgePageReviewIssue(folder.folder_id, issue.code, issue.relative_path, issue.reference)
            for issue in report.issues
            if issue.code in _REVIEW_LINT_CODES
        )
        try:
            root = Path(folder.root_path).expanduser().resolve()
        except OSError:
            page_issues.append(KnowledgePageReviewIssue(folder.folder_id, "PUBLISHED_PAGE_ROOT_UNAVAILABLE", ""))
        else:
            page_issues.extend(_published_page_drift(store, folder.folder_id, root))
    page_issues.sort(key=lambda item: (item.folder_id, item.code, item.relative_path, item.reference))
    return KnowledgeReviewSurface(
        tuple(syntheses[:limit]),
        lexical_near_duplicates,
        tuple(page_issues[:limit]),
        truncated or len(page_issues) > limit,
    )


__all__ = [
    "KnowledgePageReviewIssue",
    "KnowledgeLexicalSimilarityReview",
    "KnowledgeReviewSurface",
    "KnowledgeSynthesisReview",
    "build_knowledge_review_surface",
]
