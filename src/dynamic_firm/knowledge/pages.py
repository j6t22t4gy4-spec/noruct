"""Explicit publication of accepted Knowledge candidates as Markdown pages.

The user-owned Knowledge Folder remains authoritative.  This service never
updates an existing file: it previews deterministic bytes, requires an exact
digest confirmation, creates a new page exclusively, and then appends a local
publication receipt.  A user may edit or delete the page afterwards without
the database silently restoring it.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

from .folder_models import KnowledgeFolderStatus
from .folder_service import KnowledgeFolderService
from .page_contracts import (
    normalize_page_title,
    normalize_relative_page_path,
    page_payload_identity,
    render_candidate_markdown,
)
from .page_models import KnowledgePagePreview, KnowledgePagePublication
from .store import KnowledgeStore

def _existing_state(target: Path, payload: bytes) -> str:
    if target.is_symlink():
        return "CONFLICT_SYMLINK"
    if not target.exists():
        return "NEW"
    if not target.is_file():
        return "CONFLICT_NON_FILE"
    try:
        observed = target.read_bytes()
    except OSError:
        return "CONFLICT_UNREADABLE"
    return "EXACT_MATCH_RECOVERABLE" if observed == payload else "CONFLICT_CONTENT"


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("Knowledge page write made no progress")
        view = view[written:]


def _exclusive_write(root: Path, relative: PurePosixPath, payload: bytes) -> None:
    """Create one file without following a final symlink.

    POSIX uses directory descriptors for every path component.  The portable
    fallback revalidates each parent and still uses O_EXCL for the final file.
    """

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    if os.open in os.supports_dir_fd and os.mkdir in os.supports_dir_fd:
        root_fd = os.open(root, os.O_RDONLY | directory | nofollow)
        parent_fd = root_fd
        opened: list[int] = []
        try:
            for part in relative.parts[:-1]:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=parent_fd)
                except FileExistsError:
                    pass
                child_fd = os.open(
                    part,
                    os.O_RDONLY | directory | nofollow,
                    dir_fd=parent_fd,
                )
                opened.append(child_fd)
                parent_fd = child_fd
            descriptor = os.open(
                relative.parts[-1],
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow,
                0o600,
                dir_fd=parent_fd,
            )
            try:
                _write_all(descriptor, payload)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.fsync(parent_fd)
        finally:
            for descriptor in reversed(opened):
                os.close(descriptor)
            os.close(root_fd)
        return

    target = root.joinpath(*relative.parts)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        if current.is_symlink() or not current.is_dir():
            raise ValueError("Knowledge page parent must be a non-symlink directory")
    if root not in target.parent.resolve().parents and target.parent.resolve() != root:
        raise ValueError("Knowledge page escaped its registered Folder")
    descriptor = os.open(
        target,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow,
        0o600,
    )
    try:
        _write_all(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class KnowledgePageService:
    def __init__(self, store: KnowledgeStore) -> None:
        self.store = store

    def preview_candidate_page(
        self,
        *,
        candidate_id: str,
        folder_id: str,
        relative_path: str,
        title: str,
    ) -> KnowledgePagePreview:
        candidate = self.store.write_candidate(candidate_id)
        if (
            candidate is None
            or candidate.status != "ACCEPTED"
            or candidate.accepted_record_id is None
        ):
            raise ValueError(
                "Knowledge page preview requires an explicitly accepted candidate"
            )
        record = self.store.record(candidate.accepted_record_id)
        if record is None or record.source_candidate_id != candidate.candidate_id:
            raise ValueError("Accepted Knowledge candidate record is unavailable")
        folder = self.store.knowledge_folder(folder_id)
        if folder is None or folder.status != KnowledgeFolderStatus.ACTIVE:
            raise ValueError("Knowledge page target Folder must be active")
        if folder.access_scope != record.access_scope:
            raise ValueError(
                "Knowledge page target Folder must match the accepted record scope"
            )
        root = KnowledgeFolderService.validate_root(folder.root_path)
        page_path = normalize_relative_page_path(relative_path)
        page_title = normalize_page_title(title)
        markdown = render_candidate_markdown(
            title=page_title,
            accepted_date=str(candidate.resolved_at or candidate.created_at),
            knowledge_kind=candidate.kind,
            candidate_id=candidate.candidate_id,
            record_id=record.record_id,
            job_id=candidate.job_id,
            evidence_pack_id=candidate.evidence_pack_id,
            access_scope=record.access_scope,
            statement=candidate.statement,
        )
        payload, content_sha256, byte_size = page_payload_identity(markdown)
        receipt = self.store.page_publication(candidate_id)
        target = root.joinpath(*page_path.parts)
        if receipt is not None:
            exact_identity = (
                receipt.folder_id == folder_id
                and receipt.relative_path == page_path.as_posix()
                and receipt.title == page_title
                and receipt.content_sha256 == content_sha256
            )
            target_state = (
                "PUBLISHED"
                if exact_identity
                and _existing_state(target, payload) == "EXACT_MATCH_RECOVERABLE"
                else "PUBLISHED_USER_CONTROLLED"
                if exact_identity
                else "CONFLICT_RECEIPT"
            )
            publishable = False
        else:
            target_state = _existing_state(target, payload)
            publishable = target_state in {"NEW", "EXACT_MATCH_RECOVERABLE"}
        return KnowledgePagePreview(
            candidate_id=candidate.candidate_id,
            accepted_record_id=record.record_id,
            folder_id=folder.folder_id,
            relative_path=page_path.as_posix(),
            title=page_title,
            markdown=markdown,
            content_sha256=content_sha256,
            byte_size=byte_size,
            target_state=target_state,
            publishable=publishable,
        )

    def publish_candidate_page(
        self,
        *,
        candidate_id: str,
        folder_id: str,
        relative_path: str,
        title: str,
        expected_content_sha256: str,
        confirm: bool,
    ) -> KnowledgePagePublication:
        if not confirm:
            raise ValueError("Knowledge page publication requires explicit confirmation")
        preview = self.preview_candidate_page(
            candidate_id=candidate_id,
            folder_id=folder_id,
            relative_path=relative_path,
            title=title,
        )
        if preview.content_sha256 != expected_content_sha256:
            raise ValueError("Knowledge page preview digest changed before publication")
        existing = self.store.page_publication(candidate_id)
        if existing is not None:
            if preview.target_state in {"PUBLISHED", "PUBLISHED_USER_CONTROLLED"}:
                return existing
            raise ValueError("Knowledge page publication identity conflicts with its receipt")
        if not preview.publishable:
            raise ValueError(
                f"Knowledge page target is not publishable: {preview.target_state}"
            )
        folder = self.store.knowledge_folder(folder_id)
        assert folder is not None
        root = KnowledgeFolderService.validate_root(folder.root_path)
        payload = preview.markdown.encode("utf-8")
        if preview.target_state == "NEW":
            try:
                _exclusive_write(
                    root, normalize_relative_page_path(relative_path), payload
                )
            except FileExistsError:
                target = root.joinpath(
                    *normalize_relative_page_path(relative_path).parts
                )
                if _existing_state(target, payload) != "EXACT_MATCH_RECOVERABLE":
                    raise ValueError(
                        "Knowledge page target changed during publication"
                    ) from None
        return self.store.record_page_publication(
            candidate_id=preview.candidate_id,
            accepted_record_id=preview.accepted_record_id,
            folder_id=preview.folder_id,
            relative_path=preview.relative_path,
            title=preview.title,
            content_sha256=preview.content_sha256,
            byte_size=preview.byte_size,
        )
