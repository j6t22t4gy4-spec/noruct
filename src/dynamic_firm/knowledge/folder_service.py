"""Bounded local scanner for user-owned raw Knowledge Folders."""

from __future__ import annotations

import os
import threading
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import Callable

from .folder_models import (
    KnowledgeFolder,
    KnowledgeFolderEntryStatus,
    KnowledgeFolderOpenResult,
    KnowledgeFolderScanProgress,
    KnowledgeFolderScanPreview,
    KnowledgeFolderPreviewEntry,
    KnowledgeFolderScanReport,
    KnowledgeFolderStatus,
    ScannedKnowledgeFile,
)
from .intake import LocalDocumentExtractor, PlainTextExtractor, detect_media_type
from .preview import KnowledgePreview, safe_folder_preview
from .store import KnowledgeStore
from .vault import MAX_ASSET_BYTES, KnowledgeVault, sha256_file


DEFAULT_MAX_FOLDER_FILES = 2_000
DEFAULT_MAX_FOLDER_DEPTH = 20
DEFAULT_MAX_FOLDER_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_INDEX_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_INDEX_TOTAL_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_DOCUMENT_FILES = 32
DEFAULT_DOCUMENT_TIMEOUT_SECONDS = 20.0
DEFAULT_MAX_DOCUMENT_SOURCE_BYTES = 16 * 1024 * 1024
_IGNORED_NAMES = frozenset({".DS_Store", ".git", ".noruct", "__pycache__"})
_SECRET_FILE_NAMES = frozenset({"credentials", "credentials.json", "secrets", "secrets.json", "id_rsa", "id_ed25519"})
_SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".kdbx")
_MAX_IGNORE_GLOBS = 32


class KnowledgeFolderScanControl:
    """Thread-safe stop request for one explicit local Folder scan.

    Cancellation is intentionally observed at file boundaries.  The scan
    commits only the entries it already observed and marks the reconciliation
    incomplete, so unseen raw files are never inferred to be deleted.
    """

    def __init__(self) -> None:
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()


class KnowledgeFolderService:
    """Index raw files without making the database their source of truth."""

    def __init__(self, store: KnowledgeStore, vault: KnowledgeVault) -> None:
        self.store = store
        self.vault = vault
        self._text = PlainTextExtractor()
        self._documents = LocalDocumentExtractor()

    @staticmethod
    def _clip_utf8(value: str, maximum: int) -> tuple[str, int, bool]:
        payload = value.encode("utf-8")
        if len(payload) <= maximum:
            return value, len(payload), False
        clipped = payload[:maximum]
        while clipped:
            try:
                return clipped.decode("utf-8"), len(clipped), True
            except UnicodeDecodeError:
                clipped = clipped[:-1]
        return "", 0, True

    @staticmethod
    def validate_root(root: str | Path) -> Path:
        requested = Path(root).expanduser()
        if requested.is_symlink():
            raise ValueError("Knowledge Folder root must not be a symbolic link")
        resolved = requested.resolve()
        if not resolved.is_dir():
            raise ValueError("Knowledge Folder root must be an existing directory")
        return resolved

    def register(
        self,
        root: str | Path,
        *,
        display_name: str = "",
        access_scope: str = "private",
        ignore_globs: tuple[str, ...] = (),
    ) -> tuple[KnowledgeFolder, bool]:
        resolved = self.validate_root(root)
        return self.store.register_knowledge_folder(
            root_path=str(resolved),
            display_name=display_name.strip() or resolved.name or str(resolved),
            access_scope=access_scope.strip() or "private",
            ignore_globs=self.normalize_ignore_globs(ignore_globs),
        )

    @staticmethod
    def normalize_ignore_globs(patterns: tuple[str, ...] | list[str]) -> tuple[str, ...]:
        """Validate portable, relative glob exclusions without interpreting ignore files.

        A slash-free pattern matches any basename. A pattern containing a slash
        matches the normalized root-relative POSIX path. Rules cannot override
        Noruct's hard system or secret-like exclusions.
        """

        if len(patterns) > _MAX_IGNORE_GLOBS:
            raise ValueError(f"Knowledge Folder supports at most {_MAX_IGNORE_GLOBS} ignore rules")
        normalized: list[str] = []
        for value in patterns:
            if not isinstance(value, str):
                raise ValueError("Knowledge Folder ignore rule must be text")
            pattern = value.strip()
            if (
                not pattern
                or len(pattern.encode("utf-8")) > 256
                or "\\" in pattern
                or "\x00" in pattern
                or pattern.startswith("/")
                or any(part == ".." for part in PurePosixPath(pattern).parts)
            ):
                raise ValueError("Knowledge Folder ignore rule must be a bounded relative POSIX glob")
            if pattern not in normalized:
                normalized.append(pattern)
        return tuple(normalized)

    @staticmethod
    def _matches_user_ignore(relative_path: str, patterns: tuple[str, ...]) -> bool:
        name = PurePosixPath(relative_path).name
        return any(
            fnmatchcase(relative_path, pattern)
            if "/" in pattern
            else fnmatchcase(name, pattern)
            for pattern in patterns
        )

    @staticmethod
    def _secret_like_name(name: str) -> bool:
        normalized = name.casefold()
        return (
            normalized == ".env"
            or normalized.startswith(".env.")
            or normalized in _SECRET_FILE_NAMES
            or normalized.endswith(_SECRET_SUFFIXES)
        )

    @classmethod
    def preview_root(
        cls,
        root: str | Path,
        *,
        max_files: int = DEFAULT_MAX_FOLDER_FILES,
        max_depth: int = DEFAULT_MAX_FOLDER_DEPTH,
        sample_limit: int = 100,
        ignore_globs: tuple[str, ...] = (),
    ) -> KnowledgeFolderScanPreview:
        """Classify the local tree without reading bodies, hashing, or saving state.

        This is intentionally not `.gitignore` emulation: an ignore file is
        content and may encode project-specific policy.  The preview reports
        only Noruct's deterministic system and secret-like exclusions, which
        are the exact same exclusions the scanner applies.
        """

        if max_files < 1 or max_files > 10_000:
            raise ValueError("Knowledge Folder max_files must be between 1 and 10000")
        if max_depth < 0 or max_depth > 64:
            raise ValueError("Knowledge Folder max_depth must be between 0 and 64")
        if sample_limit < 1 or sample_limit > 500:
            raise ValueError("Knowledge Folder preview sample_limit must be between 1 and 500")
        patterns = cls.normalize_ignore_globs(ignore_globs)
        resolved = cls.validate_root(root)
        candidate_files = ignored_system = ignored_secret_like = ignored_user_patterns = skipped_symlinks = depth_limited = 0
        file_limited = False
        samples: list[KnowledgeFolderPreviewEntry] = []

        def note(path: Path, classification: str) -> None:
            if len(samples) < sample_limit:
                samples.append(
                    KnowledgeFolderPreviewEntry(
                        relative_path=PurePosixPath(*path.relative_to(resolved).parts).as_posix(),
                        classification=classification,
                    )
                )

        stack: list[tuple[Path, int]] = [(resolved, 0)]
        while stack and not file_limited:
            directory, depth = stack.pop()
            try:
                with os.scandir(directory) as iterator:
                    children = sorted(iterator, key=lambda item: item.name.casefold())
            except OSError:
                continue
            directories: list[Path] = []
            for child in children:
                path = Path(child.path)
                if child.name in _IGNORED_NAMES:
                    ignored_system += 1
                    note(path, "IGNORED_SYSTEM")
                    continue
                if cls._secret_like_name(child.name):
                    ignored_secret_like += 1
                    note(path, "IGNORED_SECRET_LIKE")
                    continue
                relative = PurePosixPath(*path.relative_to(resolved).parts).as_posix()
                if cls._matches_user_ignore(relative, patterns):
                    ignored_user_patterns += 1
                    note(path, "IGNORED_USER_PATTERN")
                    continue
                try:
                    if child.is_symlink():
                        skipped_symlinks += 1
                        note(path, "SKIPPED_SYMLINK")
                    elif child.is_dir(follow_symlinks=False):
                        if depth < max_depth:
                            directories.append(path)
                        else:
                            depth_limited += 1
                            note(path, "SKIPPED_DEPTH_LIMIT")
                    elif child.is_file(follow_symlinks=False):
                        candidate_files += 1
                        note(path, "CANDIDATE")
                        if candidate_files >= max_files:
                            file_limited = True
                            break
                except OSError:
                    continue
            for nested in reversed(directories):
                stack.append((nested, depth + 1))
        return KnowledgeFolderScanPreview(
            root_path=str(resolved),
            candidate_files=candidate_files,
            ignored_system=ignored_system,
            ignored_secret_like=ignored_secret_like,
            ignored_user_patterns=ignored_user_patterns,
            skipped_symlinks=skipped_symlinks,
            depth_limited=depth_limited,
            file_limited=file_limited,
            samples=tuple(samples),
        )

    def pause(self, folder_id: str) -> KnowledgeFolder:
        return self.store.set_knowledge_folder_status(
            folder_id, status=KnowledgeFolderStatus.PAUSED
        )

    def resume(self, folder_id: str) -> KnowledgeFolder:
        return self.store.set_knowledge_folder_status(
            folder_id, status=KnowledgeFolderStatus.ACTIVE
        )

    def relink(
        self,
        folder_id: str,
        root: str | Path,
        *,
        display_name: str | None = None,
    ) -> KnowledgeFolder:
        resolved = self.validate_root(root)
        return self.store.relink_knowledge_folder(
            folder_id,
            root_path=str(resolved),
            display_name=display_name,
        )

    def remove(self, folder_id: str) -> bool:
        return self.store.remove_knowledge_folder(folder_id)

    def set_ignore_globs(
        self, folder_id: str, *, ignore_globs: tuple[str, ...] | list[str]
    ) -> KnowledgeFolder:
        return self.store.set_knowledge_folder_ignore_globs(
            folder_id,
            ignore_globs=self.normalize_ignore_globs(ignore_globs),
        )

    def _indexer_revision(
        self,
        *,
        max_index_bytes: int,
        max_index_total_bytes: int,
        extract_documents: bool,
        max_document_files: int,
        document_timeout_seconds: float,
    ) -> str:
        """Bind reusable folder text to the exact local indexing contract."""

        return "|".join(
            (
                "folder-index-v2",
                f"plain={self._text.name}@{self._text.version}",
                f"document={self._documents.name}@{self._documents.version}",
                f"max-index={max_index_bytes}",
                f"max-total={max_index_total_bytes}",
                f"extract-documents={int(extract_documents)}",
                f"max-documents={max_document_files}",
                f"document-timeout={document_timeout_seconds:.3f}",
            )
        )

    def scan(
        self,
        folder_id: str,
        *,
        max_files: int = DEFAULT_MAX_FOLDER_FILES,
        max_depth: int = DEFAULT_MAX_FOLDER_DEPTH,
        max_total_bytes: int = DEFAULT_MAX_FOLDER_BYTES,
        max_index_bytes: int = DEFAULT_MAX_INDEX_BYTES,
        max_index_total_bytes: int = DEFAULT_MAX_INDEX_TOTAL_BYTES,
        extract_documents: bool = False,
        max_document_files: int = DEFAULT_MAX_DOCUMENT_FILES,
        document_timeout_seconds: float = DEFAULT_DOCUMENT_TIMEOUT_SECONDS,
        control: KnowledgeFolderScanControl | None = None,
        progress: Callable[[KnowledgeFolderScanProgress], None] | None = None,
    ) -> KnowledgeFolderScanReport:
        if max_files < 1 or max_files > 10_000:
            raise ValueError("Knowledge Folder max_files must be between 1 and 10000")
        if max_depth < 0 or max_depth > 64:
            raise ValueError("Knowledge Folder max_depth must be between 0 and 64")
        if max_total_bytes < 1 or max_total_bytes > 8 * 1024 * 1024 * 1024:
            raise ValueError("Knowledge Folder byte ceiling is invalid")
        if max_index_bytes < 1 or max_index_bytes > 16 * 1024 * 1024:
            raise ValueError("Knowledge Folder index byte ceiling is invalid")
        if max_index_total_bytes < 1 or max_index_total_bytes > 512 * 1024 * 1024:
            raise ValueError("Knowledge Folder total index byte ceiling is invalid")
        if max_document_files < 0 or max_document_files > 256:
            raise ValueError("Knowledge Folder document file ceiling is invalid")
        if document_timeout_seconds <= 0 or document_timeout_seconds > 120:
            raise ValueError("Knowledge Folder document timeout is invalid")
        scan_control = control or KnowledgeFolderScanControl()
        folder = self.store.knowledge_folder(folder_id)
        if folder is None:
            raise ValueError(f"Knowledge Folder was not found: {folder_id}")
        root = self.validate_root(folder.root_path)
        indexer_revision = self._indexer_revision(
            max_index_bytes=max_index_bytes,
            max_index_total_bytes=max_index_total_bytes,
            extract_documents=extract_documents,
            max_document_files=max_document_files,
            document_timeout_seconds=document_timeout_seconds,
        )
        existing_by_path = {
            item.relative_path: item
            for item in self.store.list_knowledge_folder_entries(
                folder_id,
                include_deleted=True,
                limit=10_000,
            )
            if item.index_status != KnowledgeFolderEntryStatus.DELETED
        }
        files: list[ScannedKnowledgeFile] = []
        messages: list[str] = []
        skipped_symlinks = 0
        skipped_oversized = 0
        skipped_secret_like = 0
        skipped_user_ignored = 0
        error_files = 0
        scanned_bytes = 0
        indexed_text_bytes = 0
        document_files = 0
        truncated = False
        incomplete = False
        cancelled = False

        def emit(phase: str, *, force: bool = False) -> None:
            if progress is None or (not force and len(files) % 25 != 0):
                return
            try:
                progress(
                    KnowledgeFolderScanProgress(
                        folder_id=folder_id,
                        phase=phase,
                        scanned_files=len(files),
                        ready_files=sum(
                            item.index_status == KnowledgeFolderEntryStatus.READY
                            for item in files
                        ),
                        error_files=error_files,
                        scanned_bytes=scanned_bytes,
                        max_files=max_files,
                        cancelled=scan_control.cancelled,
                    )
                )
            except Exception:
                # UI/observer code is not allowed to turn a bounded local
                # reconciliation into a failed or authority-changing scan.
                return

        emit("STARTED", force=True)
        stack: list[tuple[Path, int]] = [(root, 0)]
        while stack and not truncated:
            if scan_control.cancelled:
                cancelled = True
                incomplete = True
                break
            directory, depth = stack.pop()
            try:
                with os.scandir(directory) as iterator:
                    children = sorted(iterator, key=lambda item: item.name.casefold())
            except OSError as exc:
                error_files += 1
                incomplete = True
                messages.append(f"Unreadable directory skipped: {directory.name}: {exc}")
                continue
            directories: list[Path] = []
            for child in children:
                if scan_control.cancelled:
                    cancelled = True
                    incomplete = True
                    break
                if child.name in _IGNORED_NAMES:
                    continue
                try:
                    if self._secret_like_name(child.name):
                        skipped_secret_like += 1
                        continue
                    if child.is_symlink():
                        skipped_symlinks += 1
                        continue
                    if child.is_dir(follow_symlinks=False):
                        relative = PurePosixPath(*Path(child.path).relative_to(root).parts).as_posix()
                        if self._matches_user_ignore(relative, folder.ignore_globs):
                            skipped_user_ignored += 1
                            continue
                        if depth < max_depth:
                            directories.append(Path(child.path))
                        else:
                            incomplete = True
                        continue
                    if not child.is_file(follow_symlinks=False):
                        continue
                    if len(files) >= max_files:
                        truncated = True
                        break
                    stat = child.stat(follow_symlinks=False)
                    if scanned_bytes + stat.st_size > max_total_bytes:
                        truncated = True
                        break
                    scanned_bytes += stat.st_size
                    path = Path(child.path)
                    relative = PurePosixPath(*path.relative_to(root).parts).as_posix()
                    if self._matches_user_ignore(relative, folder.ignore_globs):
                        skipped_user_ignored += 1
                        continue
                    if len(relative.encode("utf-8")) > 2048:
                        error_files += 1
                        messages.append("One path exceeded the 2048-byte index bound.")
                        continue
                    media_type = detect_media_type(path)
                    existing = existing_by_path.get(relative)
                    if (
                        existing is not None
                        and existing.byte_size == stat.st_size
                        and existing.modified_ns == stat.st_mtime_ns
                        and existing.media_type == media_type
                        and existing.indexer_revision == indexer_revision
                    ):
                        files.append(
                            ScannedKnowledgeFile(
                                relative_path=relative,
                                content_hash=existing.content_hash,
                                byte_size=existing.byte_size,
                                modified_ns=existing.modified_ns,
                                media_type=existing.media_type,
                                index_status=existing.index_status,
                                index_text=existing.index_text,
                                index_error=existing.index_error,
                                indexer_revision=indexer_revision,
                            )
                        )
                        emit("SCANNING")
                        continue
                    if stat.st_size > MAX_ASSET_BYTES:
                        skipped_oversized += 1
                        files.append(
                            ScannedKnowledgeFile(
                                relative_path=relative,
                                content_hash="",
                                byte_size=stat.st_size,
                                modified_ns=stat.st_mtime_ns,
                                media_type=media_type,
                                index_status=KnowledgeFolderEntryStatus.METADATA_ONLY,
                                index_error="File exceeds the Knowledge snapshot size bound.",
                                indexer_revision=indexer_revision,
                            )
                        )
                        emit("SCANNING")
                        continue
                    digest, observed_size = sha256_file(path, max_bytes=MAX_ASSET_BYTES)
                    after = path.stat()
                    if (
                        observed_size != stat.st_size
                        or after.st_size != stat.st_size
                        or after.st_mtime_ns != stat.st_mtime_ns
                    ):
                        raise ValueError("File changed during Knowledge Folder scan")
                    status = KnowledgeFolderEntryStatus.METADATA_ONLY
                    text = ""
                    error = "No bounded local indexer supports this file type."
                    extractor = None
                    timeout = 1.0
                    plain_text = self._text.supports(path, media_type)
                    local_document = self._documents.supports(path, media_type)
                    if plain_text and stat.st_size <= max_index_bytes:
                        extractor = self._text
                    elif plain_text:
                        error = "Plain-text file exceeds the per-file index byte ceiling."
                    elif local_document and not extract_documents:
                        error = "Local document extraction was not requested for this scan."
                    elif local_document and stat.st_size > DEFAULT_MAX_DOCUMENT_SOURCE_BYTES:
                        error = "Document exceeds the folder extraction source-size ceiling."
                    elif local_document and document_files >= max_document_files:
                        error = "Folder document extraction count budget was exhausted."
                    elif local_document:
                        extractor = self._documents
                        timeout = document_timeout_seconds

                    remaining_index_bytes = max_index_total_bytes - indexed_text_bytes
                    if extractor is not None and remaining_index_bytes <= 0:
                        extractor = None
                        error = "Folder text index budget was exhausted."
                    if extractor is not None:
                        try:
                            extracted = extractor.extract(
                                path,
                                media_type=media_type,
                                timeout_seconds=timeout,
                            )
                            text, selected_bytes, clipped = self._clip_utf8(
                                extracted.markdown,
                                min(max_index_bytes, remaining_index_bytes),
                            )
                            if not text:
                                raise ValueError("Local extractor returned no bounded searchable text")
                            status = KnowledgeFolderEntryStatus.READY
                            error = (
                                "Extracted text was clipped to the folder index byte ceiling."
                                if clipped
                                else ""
                            )
                            indexed_text_bytes += selected_bytes
                            if extractor is self._documents:
                                document_files += 1
                        except (OSError, UnicodeError, ValueError) as exc:
                            error_files += 1
                            error = f"Local extraction failed: {exc}"
                            messages.append(f"Metadata-only file: {child.name}: {exc}")
                    files.append(
                        ScannedKnowledgeFile(
                            relative_path=relative,
                            content_hash=digest,
                            byte_size=stat.st_size,
                            modified_ns=stat.st_mtime_ns,
                            media_type=media_type,
                            index_status=status,
                            index_text=text,
                            index_error=error,
                            indexer_revision=indexer_revision,
                        )
                    )
                    emit("SCANNING")
                except (OSError, UnicodeError, ValueError) as exc:
                    error_files += 1
                    incomplete = True
                    messages.append(f"File skipped: {child.name}: {exc}")
            if cancelled:
                break
            for nested in reversed(directories):
                stack.append((nested, depth + 1))

        files.sort(key=lambda item: item.relative_path)
        updated_folder, changes = self.store.reconcile_knowledge_folder(
            folder_id,
            files,
            truncated=(truncated or incomplete),
        )
        ready = sum(item.index_status == KnowledgeFolderEntryStatus.READY for item in files)
        metadata_only = sum(
            item.index_status == KnowledgeFolderEntryStatus.METADATA_ONLY for item in files
        )
        if truncated or incomplete:
            messages.append(
                "Scan was incomplete or reached a ceiling; unseen entries were not marked deleted."
            )
        if cancelled:
            messages.append("Scan was cancelled; indexed entries observed before cancellation were retained.")
        emit("CANCELLED" if cancelled else "COMPLETED", force=True)
        return KnowledgeFolderScanReport(
            folder=updated_folder,
            scanned_files=len(files),
            ready_files=ready,
            metadata_only_files=metadata_only,
            document_files=document_files,
            error_files=error_files,
            created_entries=changes["created"],
            updated_entries=changes["updated"],
            renamed_entries=changes["renamed"],
            unchanged_entries=changes["unchanged"],
            deleted_entries=changes["deleted"],
            skipped_symlinks=skipped_symlinks,
            skipped_oversized=skipped_oversized,
            skipped_secret_like=skipped_secret_like,
            skipped_user_ignored=skipped_user_ignored,
            scanned_bytes=scanned_bytes,
            truncated=(truncated or incomplete),
            cancelled=cancelled,
            messages=tuple(messages[:50]),
        )

    def snapshot_entry(self, entry_id: str):
        entry = self.store.folder_entry(entry_id)
        if entry is None or entry.index_status == KnowledgeFolderEntryStatus.DELETED:
            raise ValueError(f"Current Knowledge Folder entry was not found: {entry_id}")
        folder = self.store.knowledge_folder(entry.folder_id)
        if folder is None:
            raise ValueError(f"Knowledge Folder was not found: {entry.folder_id}")
        root = self.validate_root(folder.root_path)
        source = (root / Path(*PurePosixPath(entry.relative_path).parts)).resolve()
        try:
            source.relative_to(root)
        except ValueError as exc:
            raise ValueError("Knowledge Folder entry escaped its registered root") from exc
        if source.is_symlink() or not source.is_file():
            raise ValueError("Knowledge Folder entry is no longer a regular file; rescan required")
        digest, size = sha256_file(source, max_bytes=MAX_ASSET_BYTES)
        if digest != entry.content_hash or size != entry.byte_size:
            raise ValueError("Knowledge Folder entry changed; rescan before use")
        if entry.snapshot_asset_id:
            asset = self.store.asset(entry.snapshot_asset_id)
            if asset is not None and asset.content_hash == digest:
                return asset
        result = self.store_and_process_snapshot(source, entry)
        self.store.bind_folder_entry_snapshot(entry.entry_id, result.asset.asset_id)
        return result.asset

    def store_and_process_snapshot(self, source: Path, entry):
        from .intake import KnowledgeIntakeService

        folder = self.store.knowledge_folder(entry.folder_id)
        if folder is None:
            raise ValueError(f"Knowledge Folder was not found: {entry.folder_id}")
        return KnowledgeIntakeService(self.store, self.vault).ingest(
            source,
            title=Path(entry.relative_path).stem,
            origin=f"knowledge-folder:{entry.folder_id}:{entry.relative_path}",
            access_scope=folder.access_scope,
        )

    def open_entry(
        self,
        entry_id: str,
        *,
        max_bytes: int = 16_000,
    ) -> KnowledgeFolderOpenResult:
        if max_bytes < 128 or max_bytes > 64_000:
            raise ValueError("Knowledge Folder open bound must be between 128 and 64000")
        entry = self.store.folder_entry(entry_id)
        if entry is None or entry.index_status != KnowledgeFolderEntryStatus.READY:
            raise ValueError("Knowledge Folder entry has no bounded searchable text")
        asset = self.snapshot_entry(entry_id)
        payload = entry.index_text.encode("utf-8")
        clipped = payload[:max_bytes]
        while clipped:
            try:
                content = clipped.decode("utf-8")
                break
            except UnicodeDecodeError:
                clipped = clipped[:-1]
        else:
            content = ""
        current = self.store.folder_entry(entry_id)
        assert current is not None
        return KnowledgeFolderOpenResult(
            entry=current,
            content=content,
            selected_bytes=len(clipped),
            truncated=len(payload) > len(clipped),
            snapshot_asset_id=asset.asset_id,
        )

    def preview_entry(self, entry_id: str, *, max_bytes: int = 16_000) -> KnowledgePreview:
        """Display current indexed text safely without creating a snapshot Asset."""
        if max_bytes < 128 or max_bytes > 64_000:
            raise ValueError("Knowledge Folder preview bound must be between 128 and 64000")
        entry = self.store.folder_entry(entry_id)
        if entry is None or entry.index_status != KnowledgeFolderEntryStatus.READY:
            raise ValueError("Knowledge Folder entry has no bounded searchable text")
        content, selected_bytes, truncated = self._clip_utf8(entry.index_text, max_bytes)
        return safe_folder_preview(entry, content, selected_bytes=selected_bytes, truncated=truncated)
