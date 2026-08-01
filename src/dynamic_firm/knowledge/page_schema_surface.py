"""Archive-validation surface owned by the Knowledge page receipt schema."""

from __future__ import annotations

from typing import Mapping


PAGE_PUBLICATION_TABLE_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "knowledge_page_publications": (
        "publication_id",
        "candidate_id",
        "accepted_record_id",
        "folder_id",
        "relative_path",
        "title",
        "content_sha256",
        "byte_size",
        "published_at",
    )
}

PAGE_PUBLICATION_INDEX_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "knowledge_page_publications_folder_idx": ("folder_id", "published_at")
}

PAGE_PUBLICATION_FOREIGN_KEYS: Mapping[
    str, frozenset[tuple[str, str, str, str, str]]
] = {
    "knowledge_page_publications": frozenset(
        {
            (
                "candidate_id",
                "knowledge_write_candidates",
                "candidate_id",
                "NO ACTION",
                "CASCADE",
            ),
            (
                "accepted_record_id",
                "knowledge_records",
                "record_id",
                "NO ACTION",
                "CASCADE",
            ),
            (
                "folder_id",
                "knowledge_folders",
                "folder_id",
                "NO ACTION",
                "CASCADE",
            ),
        }
    )
}
