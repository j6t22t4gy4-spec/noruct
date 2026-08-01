from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .locking import KnowledgeStateLock


MAX_ASSET_BYTES = 256 * 1024 * 1024
MAX_REPRESENTATION_BYTES = 32 * 1024 * 1024
MAX_DELETE_JOURNAL_BYTES = 8 * 1024 * 1024
MAX_DELETE_ENTRIES = 100_000


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def sha256_file(path: str | Path, *, max_bytes: int | None = None) -> tuple[str, int]:
    """Hash a stable regular file without loading it into memory."""

    source = Path(path)
    if source.is_symlink() or not source.is_file():
        raise ValueError("File to hash must be a regular non-symlink file")
    expected_size = source.stat().st_size
    if max_bytes is not None and expected_size > max_bytes:
        raise ValueError(f"File exceeds the {max_bytes} byte hash limit")
    digest = hashlib.sha256()
    observed = 0
    with source.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            observed += len(chunk)
            if max_bytes is not None and observed > max_bytes:
                raise ValueError(f"File exceeds the {max_bytes} byte hash limit")
            digest.update(chunk)
    if observed != expected_size:
        raise ValueError("File changed while it was being hashed")
    return digest.hexdigest(), observed


@dataclass(frozen=True, slots=True)
class VaultObject:
    content_hash: str
    byte_size: int
    relative_path: str
    created: bool = False


@dataclass(frozen=True, slots=True)
class VaultDeleteEntry:
    relative_path: str
    trash_relative_path: str
    content_hash: str
    byte_size: int


@dataclass(frozen=True, slots=True)
class VaultDeleteJournal:
    transaction: str
    asset_id: str
    expected_asset_ids: tuple[str, ...]
    expected_representation_ids: tuple[str, ...]
    entries: tuple[VaultDeleteEntry, ...]
    phase: str


class KnowledgeVault:
    """Content-addressed, user-owned files beneath one resolved vault root."""

    def __init__(self, root: str | Path) -> None:
        requested = Path(root).expanduser()
        if requested.is_symlink():
            raise ValueError("Knowledge Vault root must not be a symbolic link")
        self.root = requested.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            self.root.chmod(0o700)
        except OSError:
            pass

    def resolve(self, relative_path: str) -> Path:
        value = Path(relative_path)
        if value.is_absolute() or not value.parts or ".." in value.parts:
            raise ValueError("Knowledge Vault path must be a safe relative path")
        candidate = self.root
        for part in value.parts:
            if part in ("", "."):
                raise ValueError("Knowledge Vault path must be a safe relative path")
            candidate = candidate / part
            if candidate.is_symlink():
                raise ValueError("Knowledge Vault path contains a symbolic link")
        target = candidate.resolve()
        if not target.is_relative_to(self.root):
            raise ValueError("Knowledge Vault path escaped its root")
        return target

    def mutation_lock(self) -> KnowledgeStateLock:
        """Serialize journaled Vault mutations across threads and processes."""

        return KnowledgeStateLock(
            self.root / ".asset-operations",
            mode="exclusive",
            create_parent=True,
        )

    @property
    def delete_journal_path(self) -> Path:
        return self.resolve(".asset-delete.json")

    def _write_delete_journal(self, journal: VaultDeleteJournal) -> None:
        payload = {
            "asset_id": journal.asset_id,
            "entries": [
                {
                    "byte_size": entry.byte_size,
                    "content_hash": entry.content_hash,
                    "relative_path": entry.relative_path,
                    "trash_relative_path": entry.trash_relative_path,
                }
                for entry in journal.entries
            ],
            "expected_asset_ids": list(journal.expected_asset_ids),
            "expected_representation_ids": list(journal.expected_representation_ids),
            "phase": journal.phase,
            "schema_version": "noruct.asset-delete.v1",
            "transaction": journal.transaction,
        }
        raw = _canonical_json(payload)
        if len(raw) > MAX_DELETE_JOURNAL_BYTES:
            raise ValueError("Knowledge Asset deletion journal is oversized")
        marker = self.delete_journal_path
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".asset-delete-", suffix=".tmp", dir=self.root
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, marker)
            marker.chmod(0o600)
            _fsync_directory(self.root)
        finally:
            temporary.unlink(missing_ok=True)

    def pending_delete(self) -> VaultDeleteJournal | None:
        marker = self.delete_journal_path
        if not marker.exists():
            return None
        if marker.is_symlink() or not marker.is_file():
            raise ValueError("Knowledge Asset deletion journal is unsafe")
        with marker.open("rb") as handle:
            raw = handle.read(MAX_DELETE_JOURNAL_BYTES + 1)
        if len(raw) > MAX_DELETE_JOURNAL_BYTES:
            raise ValueError("Knowledge Asset deletion journal is oversized")
        try:
            payload = json.loads(raw)
        except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
            raise ValueError("Knowledge Asset deletion journal is invalid") from exc
        expected_keys = {
            "asset_id",
            "entries",
            "expected_asset_ids",
            "expected_representation_ids",
            "phase",
            "schema_version",
            "transaction",
        }
        if (
            not isinstance(payload, dict)
            or set(payload) != expected_keys
            or _canonical_json(payload) != raw
            or payload.get("schema_version") != "noruct.asset-delete.v1"
            or payload.get("phase") not in {"prepared", "staged", "db_committed"}
        ):
            raise ValueError("Knowledge Asset deletion journal is invalid")
        transaction = payload.get("transaction")
        asset_id = payload.get("asset_id")
        asset_ids = payload.get("expected_asset_ids")
        representation_ids = payload.get("expected_representation_ids")
        raw_entries = payload.get("entries")
        if (
            not isinstance(transaction, str)
            or len(transaction) != 32
            or any(character not in "0123456789abcdef" for character in transaction)
            or not isinstance(asset_id, str)
            or not asset_id.startswith("asset-")
            or len(asset_id.encode("utf-8")) > 256
            or not isinstance(asset_ids, list)
            or asset_ids != sorted(set(asset_ids))
            or asset_id not in asset_ids
            or not isinstance(representation_ids, list)
            or representation_ids != sorted(set(representation_ids))
            or not isinstance(raw_entries, list)
            or len(raw_entries) > MAX_DELETE_ENTRIES
        ):
            raise ValueError("Knowledge Asset deletion journal is invalid")
        if any(
            not isinstance(value, str) or not value or len(value.encode("utf-8")) > 256
            for value in (*asset_ids, *representation_ids)
        ):
            raise ValueError("Knowledge Asset deletion journal identities are invalid")
        entries: list[VaultDeleteEntry] = []
        seen_originals: set[str] = set()
        seen_trash: set[str] = set()
        for value in raw_entries:
            if not isinstance(value, Mapping) or set(value) != {
                "byte_size",
                "content_hash",
                "relative_path",
                "trash_relative_path",
            }:
                raise ValueError("Knowledge Asset deletion journal entry is invalid")
            relative = value.get("relative_path")
            trash_relative = value.get("trash_relative_path")
            digest = value.get("content_hash")
            size = value.get("byte_size")
            if (
                not isinstance(relative, str)
                or not isinstance(trash_relative, str)
                or not trash_relative.startswith(f".trash/{transaction}-")
                or relative in seen_originals
                or trash_relative in seen_trash
                or not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
                or not isinstance(size, int)
                or isinstance(size, bool)
                or size < 0
                or size > MAX_ASSET_BYTES
            ):
                raise ValueError("Knowledge Asset deletion journal entry is invalid")
            self.resolve(relative)
            self.resolve(trash_relative)
            seen_originals.add(relative)
            seen_trash.add(trash_relative)
            entries.append(VaultDeleteEntry(relative, trash_relative, digest, size))
        return VaultDeleteJournal(
            transaction=transaction,
            asset_id=asset_id,
            expected_asset_ids=tuple(asset_ids),
            expected_representation_ids=tuple(representation_ids),
            entries=tuple(entries),
            phase=str(payload["phase"]),
        )

    def begin_delete(
        self,
        *,
        asset_id: str,
        expected_asset_ids: Sequence[str],
        expected_representation_ids: Sequence[str],
        objects: Sequence[VaultObject],
    ) -> VaultDeleteJournal:
        if self.pending_delete() is not None:
            raise ValueError("A Knowledge Asset deletion requires recovery before another mutation")
        if (
            not isinstance(asset_id, str)
            or any(
                not isinstance(value, str)
                for value in (*expected_asset_ids, *expected_representation_ids)
            )
        ):
            raise ValueError("Knowledge Asset deletion identities are invalid")
        normalized_asset_ids = tuple(sorted(set(expected_asset_ids)))
        normalized_representation_ids = tuple(sorted(set(expected_representation_ids)))
        if (
            not asset_id.startswith("asset-")
            or asset_id not in normalized_asset_ids
            or any(
                not isinstance(value, str)
                or not value
                or len(value.encode("utf-8")) > 256
                for value in (*normalized_asset_ids, *normalized_representation_ids)
            )
        ):
            raise ValueError("Knowledge Asset deletion identities are invalid")
        transaction = uuid.uuid4().hex
        normalized: dict[str, VaultObject] = {}
        for value in objects:
            if (
                not isinstance(value, VaultObject)
                or not isinstance(value.relative_path, str)
                or not value.relative_path
                or len(value.relative_path.encode("utf-8")) > 2048
                or not isinstance(value.content_hash, str)
                or len(value.content_hash) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in value.content_hash
                )
                or not isinstance(value.byte_size, int)
                or isinstance(value.byte_size, bool)
                or value.byte_size < 0
                or value.byte_size > MAX_ASSET_BYTES
            ):
                raise ValueError("Knowledge Vault deletion receipt is invalid")
            self.resolve(value.relative_path)
            previous = normalized.get(value.relative_path)
            if previous is not None and (
                previous.content_hash != value.content_hash
                or previous.byte_size != value.byte_size
            ):
                raise ValueError("Knowledge Vault path has conflicting deletion identities")
            normalized[value.relative_path] = value
        if len(normalized) > MAX_DELETE_ENTRIES:
            raise ValueError("Knowledge Asset deletion exceeds the bounded object limit")
        entries: list[VaultDeleteEntry] = []
        for ordinal, value in enumerate(normalized.values()):
            original = self.resolve(value.relative_path)
            if not original.exists():
                continue
            if original.is_symlink() or not original.is_file():
                raise ValueError("Refusing to delete an unsafe Knowledge Vault object")
            observed_hash, observed_size = sha256_file(original, max_bytes=MAX_ASSET_BYTES)
            if observed_hash != value.content_hash or observed_size != value.byte_size:
                raise ValueError("Refusing to delete a changed Knowledge Vault object")
            entries.append(
                VaultDeleteEntry(
                    relative_path=value.relative_path,
                    trash_relative_path=(
                        f".trash/{transaction}-{ordinal:06d}.pending-delete"
                    ),
                    content_hash=value.content_hash,
                    byte_size=value.byte_size,
                )
            )
        journal = VaultDeleteJournal(
            transaction=transaction,
            asset_id=asset_id,
            expected_asset_ids=normalized_asset_ids,
            expected_representation_ids=normalized_representation_ids,
            entries=tuple(entries),
            phase="prepared",
        )
        self._write_delete_journal(journal)
        return journal

    def stage_journal_delete(self, journal: VaultDeleteJournal) -> VaultDeleteJournal:
        current = self.pending_delete()
        if current != journal or journal.phase != "prepared":
            raise ValueError("Knowledge Asset deletion journal changed before staging")
        trash = self.resolve(".trash")
        trash.mkdir(parents=True, exist_ok=True)
        trash.chmod(0o700)
        touched: set[Path] = {trash}
        for entry in journal.entries:
            original = self.resolve(entry.relative_path)
            temporary = self.resolve(entry.trash_relative_path)
            if not original.is_file() or temporary.exists() or temporary.is_symlink():
                raise ValueError("Knowledge Asset deletion staging state is ambiguous")
            os.replace(original, temporary)
            touched.add(original.parent)
        for directory in touched:
            _fsync_directory(directory)
        staged = VaultDeleteJournal(
            transaction=journal.transaction,
            asset_id=journal.asset_id,
            expected_asset_ids=journal.expected_asset_ids,
            expected_representation_ids=journal.expected_representation_ids,
            entries=journal.entries,
            phase="staged",
        )
        self._write_delete_journal(staged)
        return staged

    def mark_delete_committed(self, journal: VaultDeleteJournal) -> VaultDeleteJournal:
        current = self.pending_delete()
        if current != journal or journal.phase != "staged":
            raise ValueError("Knowledge Asset deletion journal changed before DB commit")
        committed = VaultDeleteJournal(
            transaction=journal.transaction,
            asset_id=journal.asset_id,
            expected_asset_ids=journal.expected_asset_ids,
            expected_representation_ids=journal.expected_representation_ids,
            entries=journal.entries,
            phase="db_committed",
        )
        self._write_delete_journal(committed)
        return committed

    def recover_pending_delete(
        self,
        *,
        asset_present: bool,
        referenced_paths: set[str],
    ) -> str | None:
        journal = self.pending_delete()
        if journal is None:
            return None
        journal_paths = {entry.relative_path for entry in journal.entries}
        if not referenced_paths.issubset(journal_paths):
            raise ValueError("Knowledge Asset deletion recovery references are invalid")

        states: list[tuple[VaultDeleteEntry, Path, Path, bool, bool, bool]] = []
        for entry in reversed(journal.entries):
            original = self.resolve(entry.relative_path)
            temporary = self.resolve(entry.trash_relative_path)
            original_present = original.exists() or original.is_symlink()
            temporary_present = temporary.exists() or temporary.is_symlink()
            must_preserve = asset_present or entry.relative_path in referenced_paths
            present_targets = tuple(
                target
                for target, present in (
                    (original, original_present),
                    (temporary, temporary_present),
                )
                if present
            )
            for target in present_targets:
                if target.is_symlink() or not target.is_file():
                    raise ValueError("Knowledge Asset deletion recovery found an unsafe object")
                observed_hash, observed_size = sha256_file(
                    target, max_bytes=MAX_ASSET_BYTES
                )
                if observed_hash != entry.content_hash or observed_size != entry.byte_size:
                    raise ValueError("Knowledge Asset deletion recovery found changed content")
            if must_preserve and not present_targets:
                raise ValueError(
                    "Knowledge Asset deletion rollback is missing referenced content"
                )
            states.append(
                (
                    entry,
                    original,
                    temporary,
                    original_present,
                    temporary_present,
                    must_preserve,
                )
            )

        touched: set[Path] = {self.root}
        preserved_reuse = False
        for _, original, temporary, original_present, temporary_present, must_preserve in states:
            if must_preserve:
                preserved_reuse = preserved_reuse or not asset_present
                if not original_present:
                    original.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(temporary, original)
                    temporary_present = False
                    touched.update((temporary.parent, original.parent))
                if temporary_present:
                    temporary.unlink()
                    touched.add(temporary.parent)
            else:
                if original_present:
                    original.unlink()
                    touched.add(original.parent)
                if temporary_present:
                    temporary.unlink()
                    touched.add(temporary.parent)
        for directory in touched:
            _fsync_directory(directory)
        marker = self.delete_journal_path
        marker.unlink()
        _fsync_directory(self.root)
        if asset_present:
            return "RESTORED"
        return "FINALIZED_WITH_REUSED_OBJECTS" if preserved_reuse else "FINALIZED"

    @staticmethod
    def inspect_source(path: str | Path, *, max_bytes: int = MAX_ASSET_BYTES) -> tuple[Path, str, int]:
        source = Path(path).expanduser()
        if source.is_symlink():
            raise ValueError("Knowledge Asset source must not be a symbolic link")
        resolved = source.resolve()
        if not resolved.is_file():
            raise ValueError(f"Knowledge Asset source is not a regular file: {resolved}")
        try:
            digest, observed = sha256_file(resolved, max_bytes=max_bytes)
        except ValueError as exc:
            if "hash limit" in str(exc):
                raise ValueError(
                    f"Knowledge Asset exceeds the {max_bytes} byte intake limit"
                ) from exc
            raise
        return resolved, digest, observed

    def store_source(
        self,
        source: Path,
        *,
        content_hash: str,
        byte_size: int,
        access_scope: str,
    ) -> VaultObject:
        scope_hash = hashlib.sha256(access_scope.encode("utf-8")).hexdigest()[:16]
        relative = Path("objects") / scope_hash / content_hash[:2] / content_hash
        target = self.resolve(relative.as_posix())
        if target.exists():
            if target.is_symlink() or not target.is_file():
                raise ValueError("Knowledge Vault content-addressed target is unsafe")
            existing_hash, existing_size = sha256_file(target, max_bytes=MAX_ASSET_BYTES)
            if existing_size != byte_size or existing_hash != content_hash:
                raise ValueError("Knowledge Vault content-addressed target has an invalid size")
            return VaultObject(content_hash, byte_size, relative.as_posix(), False)
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".asset-", dir=target.parent)
        try:
            with os.fdopen(descriptor, "wb") as output, source.open("rb") as input_handle:
                shutil.copyfileobj(input_handle, output, length=1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            temporary_path = Path(temporary)
            if temporary_path.stat().st_size != byte_size:
                raise ValueError("Knowledge Asset changed while it was copied")
            copied_hash, copied_size = sha256_file(temporary_path, max_bytes=MAX_ASSET_BYTES)
            if copied_size != byte_size:
                raise ValueError("Knowledge Asset changed while it was copied")
            if copied_hash != content_hash:
                raise ValueError("Knowledge Asset hash changed while it was copied")
            temporary_path.chmod(0o600)
            os.replace(temporary_path, target)
            _fsync_directory(target.parent)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return VaultObject(content_hash, byte_size, relative.as_posix(), True)

    def write_representation(self, asset_id: str, content: str, *, suffix: str = ".md") -> VaultObject:
        if (
            not isinstance(asset_id, str)
            or not asset_id.startswith("asset-")
            or len(asset_id.encode("utf-8")) > 256
        ):
            raise ValueError("Knowledge representation Asset id is invalid")
        if (
            not isinstance(content, str)
            or not isinstance(suffix, str)
            or not suffix.startswith(".")
            or len(suffix) < 2
            or len(suffix) > 16
            or any(character not in ".-_abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" for character in suffix)
        ):
            raise ValueError("Knowledge representation content or suffix is invalid")
        payload = content.encode("utf-8")
        if not payload:
            raise ValueError("Derived representation must not be empty")
        if len(payload) > MAX_REPRESENTATION_BYTES:
            raise ValueError(
                f"Derived representation exceeds the {MAX_REPRESENTATION_BYTES} byte limit"
            )
        digest = hashlib.sha256(payload).hexdigest()
        safe_asset = asset_id.replace("/", "_").replace("\\", "_")
        relative = Path("derived") / safe_asset / f"{digest}{suffix}"
        target = self.resolve(relative.as_posix())
        if target.exists():
            if target.is_symlink() or not target.is_file():
                raise ValueError("Knowledge Vault derived target conflicts with existing content")
            existing_hash, existing_size = sha256_file(
                target, max_bytes=MAX_REPRESENTATION_BYTES
            )
            if existing_hash != digest or existing_size != len(payload):
                raise ValueError("Knowledge Vault derived target conflicts with existing content")
            return VaultObject(digest, len(payload), relative.as_posix(), False)
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".representation-", dir=target.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            temporary_path = Path(temporary)
            temporary_path.chmod(0o600)
            os.replace(temporary_path, target)
            _fsync_directory(target.parent)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return VaultObject(digest, len(payload), relative.as_posix(), True)

    def remove_if_matches(self, value: VaultObject) -> bool:
        """Remove a newly-created object only while its bytes still match its receipt."""

        target = self.resolve(value.relative_path)
        if not target.exists():
            return False
        digest, size = sha256_file(
            target,
            max_bytes=max(MAX_ASSET_BYTES, MAX_REPRESENTATION_BYTES),
        )
        if digest != value.content_hash or size != value.byte_size:
            raise ValueError("Refusing to remove a changed Knowledge Vault object")
        target.unlink()
        _fsync_directory(target.parent)
        return True

    def read_text(self, relative_path: str) -> str:
        target = self.resolve(relative_path)
        if target.is_symlink() or not target.is_file():
            raise ValueError("Knowledge Vault representation is missing or unsafe")
        if target.stat().st_size > MAX_REPRESENTATION_BYTES:
            raise ValueError("Knowledge Vault representation exceeds its read limit")
        return target.read_text(encoding="utf-8")
