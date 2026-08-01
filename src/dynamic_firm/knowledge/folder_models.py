"""Folder-first user Knowledge state contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class KnowledgeFolderStatus(StrEnum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"


class KnowledgeFolderEntryStatus(StrEnum):
    READY = "READY"
    METADATA_ONLY = "METADATA_ONLY"
    ERROR = "ERROR"
    DELETED = "DELETED"


@dataclass(frozen=True, slots=True)
class KnowledgeFolder:
    folder_id: str
    root_path: str
    display_name: str
    access_scope: str
    ignore_globs: tuple[str, ...]
    status: KnowledgeFolderStatus
    scan_generation: int
    last_scan_status: str
    last_scan_at: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class KnowledgeFolderEntry:
    entry_id: str
    folder_id: str
    relative_path: str
    content_hash: str
    byte_size: int
    modified_ns: int
    media_type: str
    index_status: KnowledgeFolderEntryStatus
    index_text: str
    index_error: str
    indexer_revision: str
    snapshot_asset_id: str | None
    revision: int
    last_seen_generation: int
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class ScannedKnowledgeFile:
    relative_path: str
    content_hash: str
    byte_size: int
    modified_ns: int
    media_type: str
    index_status: KnowledgeFolderEntryStatus
    index_text: str = ""
    index_error: str = ""
    indexer_revision: str = ""


@dataclass(frozen=True, slots=True)
class KnowledgeFolderScanReport:
    folder: KnowledgeFolder
    scanned_files: int
    ready_files: int
    metadata_only_files: int
    document_files: int
    error_files: int
    created_entries: int
    updated_entries: int
    renamed_entries: int
    unchanged_entries: int
    deleted_entries: int
    skipped_symlinks: int
    skipped_oversized: int
    skipped_secret_like: int
    skipped_user_ignored: int
    scanned_bytes: int
    truncated: bool
    cancelled: bool = False
    messages: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class KnowledgeFolderPreviewEntry:
    """One content-free pre-scan classification, retained nowhere."""

    relative_path: str
    classification: str


@dataclass(frozen=True, slots=True)
class KnowledgeFolderScanPreview:
    """Bounded local preflight; it neither reads file bodies nor writes state."""

    root_path: str
    candidate_files: int
    ignored_system: int
    ignored_secret_like: int
    ignored_user_patterns: int
    skipped_symlinks: int
    depth_limited: int
    file_limited: bool
    samples: tuple[KnowledgeFolderPreviewEntry, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class KnowledgeFolderScanProgress:
    """Ephemeral local scan telemetry; never a second Folder authority."""

    folder_id: str
    phase: str
    scanned_files: int
    ready_files: int
    error_files: int
    scanned_bytes: int
    max_files: int
    cancelled: bool = False


@dataclass(frozen=True, slots=True)
class KnowledgeFolderOpenResult:
    entry: KnowledgeFolderEntry
    content: str
    selected_bytes: int
    truncated: bool
    snapshot_asset_id: str
