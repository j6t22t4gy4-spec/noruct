from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Mapping

from dynamic_firm.runtime.knowledge_retrieval import BoundedKnowledgeRetriever
from dynamic_firm.runtime.models import VersionedContent

from .intake import KnowledgeIntakeService
from .epistemic import ContentTrustClass, EpistemicStatus
from .models import EvidenceItem, EvidencePack, IntakeResult, KnowledgeAsset
from .folder_service import KnowledgeFolderService
from .remote_fetch import (
    RemoteKnowledgeDownload,
    RemoteKnowledgeNotModified,
    conditional_download_public_https_asset,
    download_public_https_asset,
    normalize_expected_sha256,
    validate_public_https_url,
    verify_download_sha256,
)
from .store import KnowledgeStore
from .vault import KnowledgeVault, VaultObject


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _bounded_utf8(value: str, maximum: int) -> str:
    payload = value.encode("utf-8")
    if len(payload) <= maximum:
        return value
    marker = "…".encode("utf-8")
    if maximum < len(marker):
        return ""
    clipped = payload[: maximum - len(marker)]
    while clipped:
        try:
            return clipped.decode("utf-8").rstrip() + marker.decode("utf-8")
        except UnicodeDecodeError:
            clipped = clipped[:-1]
    return ""


class UserKnowledgeService:
    """Small public service over the Knowledge DB, Vault, and bounded bridge."""

    def __init__(self, store: KnowledgeStore, vault: KnowledgeVault) -> None:
        self.store = store
        self.vault = vault
        self.last_recovery = self.recover_pending_mutations()
        self.intake = KnowledgeIntakeService(store, vault)
        self.folders = KnowledgeFolderService(store, vault)
        self.retriever = BoundedKnowledgeRetriever()

    def _recover_pending_mutations_unlocked(self) -> str | None:
        journal = self.vault.pending_delete()
        if journal is None:
            return None
        referenced_paths = self.store.referenced_vault_paths(
            tuple(entry.relative_path for entry in journal.entries)
        )
        return self.vault.recover_pending_delete(
            asset_present=self.store.asset(journal.asset_id) is not None,
            referenced_paths=referenced_paths,
        )

    def recover_pending_mutations(self) -> str | None:
        """Resolve a crash journal from DB authority without replaying user work."""

        with self.vault.mutation_lock():
            return self._recover_pending_mutations_unlocked()

    def ingest(self, path: str | Path, **options: object) -> IntakeResult:
        with self.vault.mutation_lock():
            self._recover_pending_mutations_unlocked()
            return self.intake.ingest(path, **options)

    def process(self, asset_id: str, **options: object) -> IntakeResult:
        with self.vault.mutation_lock():
            self._recover_pending_mutations_unlocked()
            return self.intake.process(asset_id, **options)

    def ingest_public_https(
        self,
        source_url: str,
        *,
        title: str = "",
        access_scope: str = "private",
        labels: tuple[str, ...] = (),
        processor: str = "auto",
        timeout_seconds: float = 20.0,
        expected_sha256: str | None = None,
    ) -> tuple[IntakeResult, RemoteKnowledgeDownload]:
        """Explicitly copy one public HTTPS response into the local vault.

        The temporary transport file exists only while the normal local
        intake copies it.  The remote URL is provenance text for this explicit
        Asset, never a sync target, credential source, or raw-folder owner.
        """

        expected_sha256 = normalize_expected_sha256(expected_sha256)
        download = download_public_https_asset(
            source_url, timeout_seconds=timeout_seconds
        )
        try:
            verify_download_sha256(download, expected_sha256)
            with self.vault.mutation_lock():
                self._recover_pending_mutations_unlocked()
                result = self.intake.ingest(
                    download.temporary_path,
                    title=title,
                    origin=download.source_url,
                    access_scope=access_scope,
                    labels=labels,
                    processor=processor,
                    timeout_seconds=timeout_seconds,
                )
                self.store.record_remote_asset_source(
                    result.asset.asset_id,
                    source_url=download.source_url,
                    etag=download.etag,
                    last_modified=download.last_modified,
                    content_fetched=True,
                )
        finally:
            download.temporary_path.unlink(missing_ok=True)
        return result, download

    def refresh_public_https(
        self,
        asset_id: str,
        *,
        processor: str = "auto",
        timeout_seconds: float = 20.0,
        expected_sha256: str | None = None,
    ) -> Mapping[str, object]:
        """Explicitly recheck one previously imported public source.

        This remains a foreground, user-confirmed pull: no schedule, OAuth,
        cookie, redirect, source write, or remote authority is introduced.
        A changed response becomes a parent-linked immutable Asset; a 304
        leaves the prior Asset and its evidence untouched.
        """

        expected_sha256 = normalize_expected_sha256(expected_sha256)
        asset = self.store.asset(asset_id)
        if asset is None:
            raise ValueError(f"Knowledge Asset was not found: {asset_id}")
        source = self.store.remote_asset_source(asset_id)
        if source is None:
            # Old explicit remote imports predate the local validator table.
            # Their immutable origin is enough to opt into one manual check;
            # ordinary local Assets never acquire a source merely by listing.
            source_url = validate_public_https_url(asset.origin)
            etag = None
            last_modified = None
        else:
            source_url = str(source["source_url"])
            etag = source.get("etag")
            last_modified = source.get("last_modified")
        downloaded = conditional_download_public_https_asset(
            source_url,
            timeout_seconds=timeout_seconds,
            if_none_match=str(etag) if etag else None,
            if_modified_since=str(last_modified) if last_modified else None,
        )
        if isinstance(downloaded, RemoteKnowledgeNotModified):
            with self.vault.mutation_lock():
                self._recover_pending_mutations_unlocked()
                remote = self.store.record_remote_asset_source(
                    asset.asset_id,
                    source_url=downloaded.source_url,
                    etag=downloaded.etag,
                    last_modified=downloaded.last_modified,
                    content_fetched=False,
                )
            return {
                "status": "NOT_MODIFIED",
                "previous_asset_id": asset.asset_id,
                "asset": asset,
                "remote": remote,
                "mode": "EXPLICIT_CONDITIONAL_LOCAL_REFRESH",
            }
        try:
            verify_download_sha256(downloaded, expected_sha256)
            with self.vault.mutation_lock():
                self._recover_pending_mutations_unlocked()
                result = self.intake.ingest(
                    downloaded.temporary_path,
                    title=asset.title,
                    origin=downloaded.source_url,
                    access_scope=asset.access_scope,
                    labels=asset.labels,
                    parent_asset_id=asset.asset_id,
                    processor=processor,
                    timeout_seconds=timeout_seconds,
                )
                remote = self.store.record_remote_asset_source(
                    result.asset.asset_id,
                    source_url=downloaded.source_url,
                    etag=downloaded.etag,
                    last_modified=downloaded.last_modified,
                    content_fetched=True,
                )
        finally:
            downloaded.temporary_path.unlink(missing_ok=True)
        return {
            "status": "UNCHANGED_CONTENT" if result.duplicate else "UPDATED",
            "previous_asset_id": asset.asset_id,
            "result": result,
            "remote": remote,
            "mode": "EXPLICIT_CONDITIONAL_LOCAL_REFRESH",
        }

    def build_evidence_pack(
        self,
        query: str,
        *,
        limit: int = 5,
        max_bytes: int = 12_000,
        max_excerpt_bytes: int = 2400,
        access_scope: str = "private",
        persist: bool = True,
    ) -> EvidencePack:
        normalized = query.strip()
        if not normalized:
            raise ValueError("Knowledge query must be non-empty")
        if limit < 1 or limit > 20:
            raise ValueError("Evidence Pack limit must be between 1 and 20")
        if max_bytes < 256 or max_bytes > 64_000:
            raise ValueError("Evidence Pack byte bound must be between 256 and 64000")
        if max_excerpt_bytes < 128 or max_excerpt_bytes > max_bytes:
            raise ValueError("Evidence excerpt bound is invalid")
        scope = access_scope.strip() or "private"
        # The Store's default is an operator-facing listing bound.  Evidence
        # selection must instead inspect the complete documented local corpus
        # ceiling; otherwise older files can silently disappear before lexical
        # ranking has a chance to select them.  Qualified Latin-token Folder
        # queries replace only the raw-folder candidate set with its derived
        # FTS5 projection; all other sources keep the full documented bound.
        indexed_folder_rows = self.store.indexed_folder_retrieval_rows(
            access_scope=scope,
            query=normalized,
        )
        indexed_representation_rows = self.store.indexed_representation_retrieval_rows(
            access_scope=scope,
            query=normalized,
        )
        reported_candidate_count: int | None = None
        if indexed_folder_rows is None and indexed_representation_rows is None:
            rows = self.store.retrieval_rows(access_scope=scope, limit=10_000)
        else:
            base_rows = self.store.retrieval_rows(
                access_scope=scope,
                limit=10_000,
                include_representation_chunks=indexed_representation_rows is None,
                include_folder_entries=indexed_folder_rows is None,
            )
            snapshot_asset_ids = self.store.current_folder_snapshot_asset_ids(
                access_scope=scope
            )
            indexed_representation_rows = tuple(
                row
                for row in (indexed_representation_rows or ())
                if str(row.get("asset_id") or "") not in snapshot_asset_ids
            )
            rows = (
                *base_rows,
                *indexed_representation_rows,
                *(indexed_folder_rows or ()),
            )
            reported_candidate_count = self.store.retrieval_candidate_count(
                access_scope=scope,
                limit=10_000,
            )
        mapping: dict[str, Mapping[str, object]] = {}
        retrieval_metadata: dict[str, Mapping[str, object]] = {}
        candidates: list[VersionedContent] = []
        for row in rows:
            source_id = str(row["source_id"])
            content_id = f"{row['source_type']}:{source_id}"
            content = str(row["content"])
            mapping[content_id] = row
            retrieval_metadata[content_id] = {
                "title": str(row.get("title") or ""),
                "path": str(row.get("relative_path") or row.get("title") or ""),
                "source_type": str(row.get("source_type") or ""),
                "freshness_expires_at": row.get("freshness_expires_at"),
                "trust_class": row.get("trust_class"),
                "conflict_refs": row.get("conflict_refs", ()),
            }
            candidates.append(
                VersionedContent(
                    content_id=content_id,
                    revision=str(row.get("representation_id") or source_id),
                    content=content,
                    content_hash=str(row.get("content_hash") or ""),
                )
            )
        selection = self.retriever.select(
            candidates,
            query=normalized,
            limit=limit,
            max_bytes=max_bytes,
            fallback_count=0,
            # Evidence Pack is the one caller that has a separately cited
            # excerpt boundary. Selecting a large relevant chunk here is safe:
            # the source hash/location stay intact and the loop below clips it
            # before any model-facing payload is created.
            allow_partial=True,
            metadata=retrieval_metadata,
        )
        items: list[EvidenceItem] = []
        selected_bytes = 0
        for selected in selection.items:
            row = mapping[selected.content_id]
            explanation = selection.explanation_for(selected.content_id)
            remaining = max_bytes - selected_bytes
            excerpt = _bounded_utf8(
                selected.content,
                min(max_excerpt_bytes, remaining),
            )
            size = len(excerpt.encode("utf-8"))
            if not excerpt or size > remaining:
                continue
            selected_bytes += size
            content_hash = str(row.get("content_hash") or "")
            if not content_hash:
                content_hash = hashlib.sha256(selected.content.encode("utf-8")).hexdigest()
            asset_id = str(row["asset_id"]) if row.get("asset_id") else None
            representation_id = (
                str(row["representation_id"])
                if row.get("representation_id")
                else None
            )
            if str(row["source_type"]) == "folder_file":
                try:
                    snapshot = self.folders.snapshot_entry(str(row["source_id"]))
                except (OSError, ValueError):
                    # The raw folder is current-state authority. A changed or
                    # vanished file must be rescanned instead of citing stale
                    # indexed text as immutable evidence.
                    selected_bytes -= size
                    continue
                asset_id = snapshot.asset_id
                latest = self.store.latest_representation(snapshot.asset_id)
                representation_id = (
                    latest.representation_id if latest is not None else None
                )
            items.append(
                EvidenceItem(
                    evidence_id=f"evidence-{uuid.uuid4()}",
                    source_type=str(row["source_type"]),
                    source_id=str(row["source_id"]),
                    asset_id=asset_id,
                    representation_id=representation_id,
                    title=str(row["title"]),
                    excerpt=excerpt,
                    content_hash=content_hash,
                    excerpt_hash=hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
                    source_revision=str(row.get("source_revision") or "1"),
                    source_created_at=str(row.get("source_created_at") or ""),
                    location=dict(row.get("location", {})),
                    confidence=float(row.get("confidence", 1.0)),
                    epistemic_status=EpistemicStatus(
                        str(row.get("epistemic_status", EpistemicStatus.UNKNOWN.value))
                    ),
                    trust_class=ContentTrustClass(
                        str(row.get("trust_class", ContentTrustClass.UNSPECIFIED.value))
                    ),
                    freshness_expires_at=(
                        str(row["freshness_expires_at"])
                        if row.get("freshness_expires_at")
                        else None
                    ),
                    conflict_refs=tuple(
                        str(value) for value in row.get("conflict_refs", ())
                    ),
                    unknown_refs=tuple(
                        str(value) for value in row.get("unknown_refs", ())
                    ),
                    retrieval_basis=(explanation.basis if explanation is not None else ()),
                )
            )
        provisional = EvidencePack(
            pack_id=f"pack-{uuid.uuid4()}",
            query=normalized,
            items=tuple(items),
            selected_bytes=selected_bytes,
            candidate_count=(
                selection.candidate_count
                if reported_candidate_count is None
                else reported_candidate_count
            ),
            created_at=_now(),
            access_scope=scope,
            digest="",
            schema_version="noruct.evidence-pack.v3",
        )
        pack = EvidencePack(
            pack_id=provisional.pack_id,
            query=provisional.query,
            items=provisional.items,
            selected_bytes=provisional.selected_bytes,
            candidate_count=provisional.candidate_count,
            created_at=provisional.created_at,
            access_scope=provisional.access_scope,
            digest=provisional.computed_digest(),
            schema_version=provisional.schema_version,
        )
        pack.verify(maximum_bytes=max_bytes, maximum_items=limit)
        if persist:
            self.store.save_evidence_pack(pack)
        return pack

    def delete_asset(self, asset_id: str) -> KnowledgeAsset:
        with self.vault.mutation_lock():
            self._recover_pending_mutations_unlocked()
            asset = self.store.asset(asset_id)
            if asset is None:
                raise ValueError(f"Knowledge Asset was not found: {asset_id}")
            assets = self.store.asset_deletion_closure(asset_id)
            representations = tuple(
                representation
                for item in assets
                for representation in self.store.list_representations(item.asset_id)
            )
            objects = (
                *(
                    VaultObject(
                        item.content_hash,
                        item.byte_size,
                        item.vault_relative_path,
                    )
                    for item in assets
                ),
                *(
                    VaultObject(
                        item.content_hash,
                        item.byte_size,
                        item.vault_relative_path,
                    )
                    for item in representations
                ),
            )
            try:
                journal = self.vault.begin_delete(
                    asset_id=asset_id,
                    expected_asset_ids=tuple(item.asset_id for item in assets),
                    expected_representation_ids=tuple(
                        item.representation_id for item in representations
                    ),
                    objects=objects,
                )
                journal = self.vault.stage_journal_delete(journal)
            except BaseException:
                pending = self.vault.pending_delete()
                if pending is not None:
                    self.vault.recover_pending_delete(
                        asset_present=self.store.asset(pending.asset_id) is not None,
                        referenced_paths=self.store.referenced_vault_paths(
                            tuple(entry.relative_path for entry in pending.entries)
                        ),
                    )
                raise
            try:
                deleted = self.store.delete_asset(
                    asset_id,
                    expected_asset_ids=set(journal.expected_asset_ids),
                    expected_representation_ids=set(
                        journal.expected_representation_ids
                    ),
                )
                journal = self.vault.mark_delete_committed(journal)
            except BaseException:
                self.vault.recover_pending_delete(
                    asset_present=self.store.asset(asset_id) is not None,
                    referenced_paths=self.store.referenced_vault_paths(
                        tuple(entry.relative_path for entry in journal.entries)
                    ),
                )
                raise
            self.vault.recover_pending_delete(
                asset_present=False,
                referenced_paths=self.store.referenced_vault_paths(
                    tuple(entry.relative_path for entry in journal.entries)
                ),
            )
            return deleted
