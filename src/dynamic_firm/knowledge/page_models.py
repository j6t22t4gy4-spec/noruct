"""Human-readable Knowledge page preview and publication receipts."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class KnowledgePagePreview:
    candidate_id: str
    accepted_record_id: str
    folder_id: str
    relative_path: str
    title: str
    markdown: str
    content_sha256: str
    byte_size: int
    target_state: str
    publishable: bool


@dataclass(frozen=True, slots=True)
class KnowledgePagePublication:
    publication_id: str
    candidate_id: str
    accepted_record_id: str
    folder_id: str
    relative_path: str
    title: str
    content_sha256: str
    byte_size: int
    published_at: str


@dataclass(frozen=True, slots=True)
class KnowledgePageIndexPreview:
    """Deterministic navigation map for a user-owned Knowledge Folder."""

    folder_id: str
    relative_path: str
    markdown: str
    content_sha256: str
    byte_size: int
    indexed_page_count: int
    topic_count: int
    target_state: str
    publishable: bool


@dataclass(frozen=True, slots=True)
class KnowledgePageIndexPublication:
    """Exact index bytes created without a second file authority or overwrite."""

    folder_id: str
    relative_path: str
    content_sha256: str
    byte_size: int
    indexed_page_count: int
    topic_count: int
    target_state: str


@dataclass(frozen=True, slots=True)
class KnowledgePageBundlePreview:
    """One explicit multi-candidate page preview; candidate state is unchanged."""

    candidate_ids: tuple[str, ...]
    accepted_record_ids: tuple[str, ...]
    folder_id: str
    relative_path: str
    title: str
    markdown: str
    content_sha256: str
    byte_size: int
    target_state: str
    publishable: bool


@dataclass(frozen=True, slots=True)
class KnowledgePageBundlePublication:
    candidate_ids: tuple[str, ...]
    accepted_record_ids: tuple[str, ...]
    folder_id: str
    relative_path: str
    title: str
    content_sha256: str
    byte_size: int
    target_state: str
