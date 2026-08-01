"""Explicit multi-candidate Markdown publication without an automatic merge.

Candidate acceptance remains a per-candidate Knowledge-store lifecycle.  This
service only lets a human select already accepted candidates and create one
new user-owned Markdown page after checking deterministic bytes.  It never
changes candidate state, records a synthetic merged record, or overwrites a
page after the user has taken control of it.
"""

from __future__ import annotations

import json
from pathlib import PurePosixPath
from typing import Sequence

from .folder_models import KnowledgeFolderStatus
from .folder_service import KnowledgeFolderService
from .page_contracts import (
    normalize_page_title,
    normalize_relative_page_path,
    page_payload_identity,
)
from .page_models import KnowledgePageBundlePreview, KnowledgePageBundlePublication
from .pages import _existing_state, _exclusive_write
from .store import KnowledgeStore


_MAX_CANDIDATES = 16


class KnowledgePageBundleService:
    def __init__(self, store: KnowledgeStore) -> None:
        self.store = store

    def preview(
        self,
        *,
        candidate_ids: Sequence[str],
        folder_id: str,
        relative_path: str,
        title: str,
    ) -> KnowledgePageBundlePreview:
        identifiers = _candidate_ids(candidate_ids)
        candidates = []
        records = []
        scope: str | None = None
        for candidate_id in identifiers:
            candidate = self.store.write_candidate(candidate_id)
            if (
                candidate is None
                or candidate.status != "ACCEPTED"
                or candidate.accepted_record_id is None
            ):
                raise ValueError("Knowledge page bundle requires explicitly accepted candidates")
            record = self.store.record(candidate.accepted_record_id)
            if record is None or record.source_candidate_id != candidate.candidate_id:
                raise ValueError("Accepted Knowledge candidate record is unavailable")
            if scope is None:
                scope = record.access_scope
            elif scope != record.access_scope:
                raise ValueError("Knowledge page bundle candidates must share one access scope")
            candidates.append(candidate)
            records.append(record)
        folder = self.store.knowledge_folder(folder_id)
        if folder is None or folder.status is not KnowledgeFolderStatus.ACTIVE:
            raise ValueError("Knowledge page bundle target Folder must be active")
        if folder.access_scope != scope:
            raise ValueError("Knowledge page bundle target Folder must match candidate scope")
        root = KnowledgeFolderService.validate_root(folder.root_path)
        path = normalize_relative_page_path(relative_path)
        normalized_title = normalize_page_title(title)
        markdown = _render_bundle(
            title=normalized_title,
            candidates=tuple(candidates),
            accepted_record_ids=tuple(record.record_id for record in records),
            access_scope=scope or "",
        )
        payload, digest, byte_size = page_payload_identity(markdown)
        target = root.joinpath(*path.parts)
        target_state = _existing_state(target, payload)
        return KnowledgePageBundlePreview(
            candidate_ids=identifiers,
            accepted_record_ids=tuple(record.record_id for record in records),
            folder_id=folder.folder_id,
            relative_path=path.as_posix(),
            title=normalized_title,
            markdown=markdown,
            content_sha256=digest,
            byte_size=byte_size,
            target_state=target_state,
            publishable=target_state in {"NEW", "EXACT_MATCH_RECOVERABLE"},
        )

    def publish(
        self,
        *,
        candidate_ids: Sequence[str],
        folder_id: str,
        relative_path: str,
        title: str,
        expected_content_sha256: str,
        confirm: bool,
    ) -> KnowledgePageBundlePublication:
        if not confirm:
            raise ValueError("Knowledge page bundle publication requires explicit confirmation")
        preview = self.preview(
            candidate_ids=candidate_ids,
            folder_id=folder_id,
            relative_path=relative_path,
            title=title,
        )
        if preview.content_sha256 != expected_content_sha256:
            raise ValueError("Knowledge page bundle preview digest changed before publication")
        if not preview.publishable:
            raise ValueError(f"Knowledge page bundle target is not publishable: {preview.target_state}")
        if preview.target_state == "NEW":
            folder = self.store.knowledge_folder(folder_id)
            assert folder is not None
            root = KnowledgeFolderService.validate_root(folder.root_path)
            payload = preview.markdown.encode("utf-8")
            try:
                _exclusive_write(root, PurePosixPath(preview.relative_path), payload)
            except FileExistsError:
                if _existing_state(root.joinpath(*PurePosixPath(preview.relative_path).parts), payload) != "EXACT_MATCH_RECOVERABLE":
                    raise ValueError("Knowledge page bundle target changed during publication") from None
        return KnowledgePageBundlePublication(
            candidate_ids=preview.candidate_ids,
            accepted_record_ids=preview.accepted_record_ids,
            folder_id=preview.folder_id,
            relative_path=preview.relative_path,
            title=preview.title,
            content_sha256=preview.content_sha256,
            byte_size=preview.byte_size,
            target_state=preview.target_state,
        )


def _candidate_ids(candidate_ids: Sequence[str]) -> tuple[str, ...]:
    if isinstance(candidate_ids, (str, bytes)):
        raise ValueError("Knowledge page bundle candidate identifiers must be a list")
    normalized = tuple(str(item).strip() for item in candidate_ids)
    if not 2 <= len(normalized) <= _MAX_CANDIDATES or any(not item for item in normalized):
        raise ValueError("Knowledge page bundle requires between 2 and 16 candidate identifiers")
    if len(set(normalized)) != len(normalized):
        raise ValueError("Knowledge page bundle candidate identifiers must be unique")
    return tuple(sorted(normalized))


def _render_bundle(*, title: str, candidates: tuple[object, ...], accepted_record_ids: tuple[str, ...], access_scope: str) -> str:
    lines = [
        "---",
        "schema: noruct.knowledge-page-bundle.v1",
        f"title: {json.dumps(title, ensure_ascii=False)}",
        "type: synthesis-bundle",
        f"access_scope: {json.dumps(access_scope, ensure_ascii=False)}",
        "source_candidate_ids: " + json.dumps([item.candidate_id for item in candidates]),
        "source_record_ids: " + json.dumps(list(accepted_record_ids)),
        "---",
        "",
        f"# {title}",
        "",
        "This page was explicitly assembled from accepted candidates. It does not merge or replace their Knowledge records.",
        "",
    ]
    for position, candidate in enumerate(candidates, start=1):
        lines.extend((f"## Candidate {position}", "", candidate.statement.rstrip(), "", "### Provenance", "", f"- Accepted candidate: `{candidate.candidate_id}`", f"- Source Job: `{candidate.job_id}`", ""))
    return "\n".join(lines).rstrip() + "\n"
