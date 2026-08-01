"""Knowledge archive export, restore, and crash-recovery lifecycle."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Mapping

from . import lifecycle as _lifecycle
from .locking import KnowledgeStateLock, knowledge_mutation_marker_path
from .store import SCHEMA_VERSION, KnowledgeStore
from .vault import MAX_ASSET_BYTES

ARCHIVE_SCHEMA = _lifecycle.ARCHIVE_SCHEMA
DATABASE_ARCHIVE_NAME = _lifecycle.DATABASE_ARCHIVE_NAME
MANIFEST_NAME = _lifecycle.MANIFEST_NAME
MAX_ARCHIVE_EXPANDED_BYTES = _lifecycle.MAX_ARCHIVE_EXPANDED_BYTES
MAX_ARCHIVE_MEMBERS = _lifecycle.MAX_ARCHIVE_MEMBERS
MAX_DATABASE_BYTES = _lifecycle.MAX_DATABASE_BYTES
MAX_MANIFEST_BYTES = _lifecycle.MAX_MANIFEST_BYTES
MAX_MUTATION_MARKER_BYTES = _lifecycle.MAX_MUTATION_MARKER_BYTES
VAULT_ARCHIVE_PREFIX = _lifecycle.VAULT_ARCHIVE_PREFIX
_BUFFER_BYTES = _lifecycle._BUFFER_BYTES
_DATABASE_SIDECARS = _lifecycle._DATABASE_SIDECARS
_WINDOWS_RESERVED_NAMES = _lifecycle._WINDOWS_RESERVED_NAMES
_VaultReference = _lifecycle._VaultReference
_canonical_json = _lifecycle._canonical_json
_connect_read_only = _lifecycle._connect_read_only
_integrity = _lifecycle._integrity
_open_regular = _lifecycle._open_regular
_require_regular = _lifecycle._require_regular
_require_vault = _lifecycle._require_vault
_safe_relative = _lifecycle._safe_relative
_safe_vault_file = _lifecycle._safe_vault_file
_schema_and_references = _lifecycle._schema_and_references
_sha256_file = _lifecycle._sha256_file
_sha256_handle = _lifecycle._sha256_handle
_validate_schema_surface = _lifecycle._validate_schema_surface


@dataclass(frozen=True, slots=True)
class KnowledgeExportRecord:
    schema_version: str
    archive_sha256: str
    bytes_written: int
    database_integrity: str
    database_bytes: int
    vault_object_count: int
    vault_bytes: int


@dataclass(frozen=True, slots=True)
class KnowledgeRestoreRecord:
    schema_version: str
    archive_sha256: str
    database_integrity: str
    database_bytes: int
    vault_object_count: int
    vault_bytes: int
    overwritten: bool

def _sqlite_snapshot(source: Path, destination: Path) -> None:
    source_connection = _connect_read_only(source)
    destination_connection = sqlite3.connect(str(destination))
    destination_connection.row_factory = sqlite3.Row
    try:
        source_connection.backup(destination_connection)
        destination_connection.execute("PRAGMA trusted_schema = OFF")
        destination_connection.execute("PRAGMA foreign_keys = ON")
        if _integrity(destination_connection) != "ok":
            raise ValueError("Exported Knowledge database failed its integrity check")
        _validate_schema_surface(destination_connection)
        # Online backup is page-oriented and may copy SQLite freelist pages. A
        # VACUUM rebuild keeps only live logical rows so forgotten/deleted text is
        # not retained in the database member of an export archive.
        destination_connection.execute("PRAGMA secure_delete = ON")
        destination_connection.execute("VACUUM")
        if _integrity(destination_connection) != "ok":
            raise ValueError("Sanitized Knowledge database failed its integrity check")
        _validate_schema_surface(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()
    destination.chmod(0o600)


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o600) << 16
    return info


def _write_file(archive: zipfile.ZipFile, name: str, source: Path, expected_hash: str, expected_size: int) -> None:
    digest = hashlib.sha256()
    observed = 0
    with _open_regular(source) as input_handle, archive.open(_zip_info(name), "w", force_zip64=True) as output:
        while chunk := input_handle.read(_BUFFER_BYTES):
            observed += len(chunk)
            if observed > expected_size:
                raise ValueError("Knowledge lifecycle source changed while being archived")
            digest.update(chunk)
            output.write(chunk)
    if observed != expected_size or digest.hexdigest() != expected_hash:
        raise ValueError("Knowledge lifecycle source changed while being archived")


def _fsync(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


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


def _write_mutation_marker(database: Path, payload: Mapping[str, object]) -> None:
    marker = knowledge_mutation_marker_path(database)
    raw = _canonical_json(payload)
    if len(raw) > MAX_MUTATION_MARKER_BYTES:
        raise ValueError("Knowledge lifecycle mutation marker is oversized")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{marker.name}.", suffix=".tmp", dir=marker.parent
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
        _fsync_directory(marker.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _read_mutation_marker(database: Path, vault: Path) -> dict[str, object] | None:
    marker = knowledge_mutation_marker_path(database)
    if not marker.exists() and not marker.is_symlink():
        return None
    if marker.is_symlink():
        raise ValueError("Knowledge lifecycle mutation marker is unsafe")
    with _open_regular(marker) as handle:
        raw = handle.read(MAX_MUTATION_MARKER_BYTES + 1)
    if len(raw) > MAX_MUTATION_MARKER_BYTES:
        raise ValueError("Knowledge lifecycle mutation marker is oversized")
    try:
        value = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError("Knowledge lifecycle mutation marker is invalid") from error
    expected = {
        "database",
        "database_present",
        "operation",
        "phase",
        "schema_version",
        "sidecars",
        "transaction",
        "vault",
        "vault_present",
    }
    if not isinstance(value, dict) or set(value) != expected or _canonical_json(value) != raw:
        raise ValueError("Knowledge lifecycle mutation marker is invalid")
    transaction = value.get("transaction")
    sidecars = value.get("sidecars")
    marker_database = value.get("database")
    marker_vault = value.get("vault")
    try:
        targets_match = (
            isinstance(marker_database, str)
            and isinstance(marker_vault, str)
            and Path(marker_database).expanduser().resolve() == database.resolve()
            and Path(marker_vault).expanduser().resolve() == vault.resolve()
        )
    except (OSError, ValueError):
        targets_match = False
    if (
        value.get("schema_version") != "noruct.knowledge-mutation.v1"
        or value.get("operation") not in ("restore", "delete")
        or value.get("phase") not in ("prepared", "published")
        or not isinstance(transaction, str)
        or len(transaction) != 32
        or any(character not in "0123456789abcdef" for character in transaction)
        or not isinstance(value.get("database_present"), bool)
        or not isinstance(value.get("vault_present"), bool)
        or not isinstance(sidecars, list)
        or sidecars != sorted(set(sidecars))
        or any(suffix not in _DATABASE_SIDECARS[1:] for suffix in sidecars)
        or not targets_match
    ):
        raise ValueError("Knowledge lifecycle mutation marker does not match these targets")
    return value


def _clear_mutation_marker(database: Path) -> None:
    marker = knowledge_mutation_marker_path(database)
    if marker.is_symlink():
        raise ValueError("Knowledge lifecycle mutation marker is unsafe")
    marker.unlink(missing_ok=True)
    _fsync_directory(marker.parent)


def _destination(path: str | Path, *, overwrite: bool) -> Path:
    requested = Path(path).expanduser()
    if requested.is_symlink():
        raise ValueError("Knowledge archive destination must not be a symbolic link")
    requested.parent.mkdir(parents=True, exist_ok=True)
    resolved = requested.parent.resolve() / requested.name
    if resolved.exists():
        if not resolved.is_file():
            raise ValueError("Knowledge archive destination is not a regular file")
        if not overwrite:
            raise ValueError("Knowledge archive destination exists; explicit overwrite is required")
    return resolved


def _export_knowledge_archive_unlocked(
    database_path: str | Path,
    vault_path: str | Path,
    destination: str | Path,
    *,
    overwrite: bool = False,
) -> KnowledgeExportRecord:
    """Export one self-verifying snapshot containing only DB-referenced objects."""

    database = _require_regular(database_path, "Knowledge database")
    requested_vault = Path(vault_path).expanduser()
    if requested_vault.is_symlink():
        raise ValueError("Knowledge Vault root must not be a symbolic link")
    vault = requested_vault.resolve()
    output = _destination(destination, overwrite=overwrite)
    if output == database:
        raise ValueError("Knowledge archive destination must differ from the live database")
    if output.is_relative_to(vault):
        raise ValueError("Knowledge archive destination must remain outside the live Vault")

    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    temporary.chmod(0o600)
    snapshot_root = Path(tempfile.mkdtemp(prefix=".knowledge-export-", dir=output.parent))
    snapshot = snapshot_root / DATABASE_ARCHIVE_NAME
    try:
        _sqlite_snapshot(database, snapshot)
        knowledge_schema, references = _schema_and_references(snapshot)
        if knowledge_schema != SCHEMA_VERSION:
            raise ValueError("Knowledge database schema is unsupported by this runtime")
        if references:
            vault = _require_vault(vault)
        elif vault.exists() and not vault.is_dir():
            raise ValueError("Knowledge Vault root must be a directory when present")
        verified: list[tuple[_VaultReference, Path]] = []
        vault_bytes = 0
        for reference in references:
            source = _safe_vault_file(vault, reference.relative_path)
            digest, observed = _sha256_file(source, maximum=reference.byte_size)
            if digest != reference.sha256 or observed != reference.byte_size:
                raise ValueError("Knowledge Vault object does not match its database reference")
            verified.append((reference, source))
            vault_bytes += observed
        database_hash, database_bytes = _sha256_file(snapshot, maximum=MAX_DATABASE_BYTES)
        if (
            len(references) + 2 > MAX_ARCHIVE_MEMBERS
            or vault_bytes + database_bytes > MAX_ARCHIVE_EXPANDED_BYTES
        ):
            raise ValueError("Knowledge export exceeds the restorable archive limits")
        manifest: dict[str, object] = {
            "database": {
                "archive_path": DATABASE_ARCHIVE_NAME,
                "byte_size": database_bytes,
                "integrity_check": "ok",
                "knowledge_schema_version": knowledge_schema,
                "sha256": database_hash,
            },
            "schema_version": ARCHIVE_SCHEMA,
            "vault": {
                "object_count": len(references),
                "objects": [reference.manifest_payload() for reference in references],
                "total_bytes": vault_bytes,
            },
        }
        manifest_bytes = _canonical_json(manifest)
        if len(manifest_bytes) > MAX_MANIFEST_BYTES:
            raise ValueError("Knowledge archive manifest exceeds its bounded size limit")
        with zipfile.ZipFile(temporary, "w", allowZip64=True) as archive:
            archive.writestr(_zip_info(MANIFEST_NAME), manifest_bytes)
            _write_file(archive, DATABASE_ARCHIVE_NAME, snapshot, database_hash, database_bytes)
            for reference, source in verified:
                _write_file(
                    archive,
                    f"{VAULT_ARCHIVE_PREFIX}{reference.relative_path}",
                    source,
                    reference.sha256,
                    reference.byte_size,
                )
        _fsync(temporary)
        os.replace(temporary, output)
        output.chmod(0o600)
        _fsync_directory(output.parent)
        archive_hash, archive_bytes = _sha256_file(output)
        return KnowledgeExportRecord(
            schema_version=ARCHIVE_SCHEMA,
            archive_sha256=archive_hash,
            bytes_written=archive_bytes,
            database_integrity="ok",
            database_bytes=database_bytes,
            vault_object_count=len(references),
            vault_bytes=vault_bytes,
        )
    finally:
        temporary.unlink(missing_ok=True)
        shutil.rmtree(snapshot_root, ignore_errors=True)


def export_knowledge_archive(
    database_path: str | Path,
    vault_path: str | Path,
    destination: str | Path,
    *,
    overwrite: bool = False,
) -> KnowledgeExportRecord:
    """Export a sanitized snapshot while holding a shared state lock."""

    with KnowledgeStateLock(database_path, mode="shared"):
        return _export_knowledge_archive_unlocked(
            database_path,
            vault_path,
            destination,
            overwrite=overwrite,
        )


def _member_is_regular(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0xFFFF
    return not stat.S_ISLNK(mode) and (mode == 0 or stat.S_ISREG(mode))


def _validated_members(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    infos = archive.infolist()
    if len(infos) < 2 or len(infos) > MAX_ARCHIVE_MEMBERS:
        raise ValueError("Knowledge archive member count is invalid")
    names = [info.filename for info in infos]
    if len(names) != len(set(names)):
        raise ValueError("Knowledge archive contains duplicate members")
    expanded = 0
    for info in infos:
        name = info.filename
        if (
            not name
            or "\\" in name
            or "\x00" in name
            or info.is_dir()
            or not _member_is_regular(info)
            or info.flag_bits & 0x1
        ):
            raise ValueError("Knowledge archive contains an unsafe member")
        pure = PurePosixPath(name)
        unsafe_component = any(
            part in ("", ".", "..")
            or ":" in part
            or part.endswith((" ", "."))
            or part.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES
            for part in pure.parts
        )
        if pure.is_absolute() or name != pure.as_posix() or unsafe_component:
            raise ValueError("Knowledge archive contains an unsafe member path")
        if info.file_size < 0:
            raise ValueError("Knowledge archive contains an invalid member size")
        expanded += info.file_size
        if expanded > MAX_ARCHIVE_EXPANDED_BYTES:
            raise ValueError("Knowledge archive exceeds its expanded size limit")
    return {info.filename: info for info in infos}


def _manifest(archive: zipfile.ZipFile, members: Mapping[str, zipfile.ZipInfo]) -> tuple[dict[str, object], bytes]:
    info = members.get(MANIFEST_NAME)
    if info is None or info.file_size > MAX_MANIFEST_BYTES:
        raise ValueError("Knowledge archive manifest is missing or oversized")
    raw = archive.read(info)
    try:
        value = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError("Knowledge archive manifest is invalid") from error
    if not isinstance(value, dict) or _canonical_json(value) != raw:
        raise ValueError("Knowledge archive manifest is not canonical")
    if set(value) != {"database", "schema_version", "vault"}:
        raise ValueError("Knowledge archive manifest structure is invalid")
    if value.get("schema_version") != ARCHIVE_SCHEMA:
        raise ValueError("Knowledge archive schema is unsupported")
    return value, raw


def _integer(value: object, label: str, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > maximum:
        raise ValueError(f"Knowledge archive {label} is invalid")
    return value


def _hash(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError(f"Knowledge archive {label} is invalid")
    return value


def _parse_manifest(value: Mapping[str, object]) -> tuple[int, str, int, tuple[_VaultReference, ...], int]:
    database = value.get("database")
    vault = value.get("vault")
    if not isinstance(database, dict) or not isinstance(vault, dict):
        raise ValueError("Knowledge archive manifest structure is invalid")
    expected_database_keys = {
        "archive_path", "byte_size", "integrity_check", "knowledge_schema_version", "sha256"
    }
    if set(database) != expected_database_keys or database.get("archive_path") != DATABASE_ARCHIVE_NAME or database.get("integrity_check") != "ok":
        raise ValueError("Knowledge archive database manifest is invalid")
    database_bytes = _integer(database.get("byte_size"), "database size", maximum=MAX_DATABASE_BYTES)
    database_hash = _hash(database.get("sha256"), "database hash")
    schema = _integer(database.get("knowledge_schema_version"), "schema version", maximum=1_000_000)

    if set(vault) != {"object_count", "objects", "total_bytes"} or not isinstance(vault.get("objects"), list):
        raise ValueError("Knowledge archive Vault manifest is invalid")
    raw_objects = vault["objects"]
    object_count = _integer(vault.get("object_count"), "object count", maximum=MAX_ARCHIVE_MEMBERS)
    total_bytes = _integer(vault.get("total_bytes"), "Vault byte count", maximum=MAX_ARCHIVE_EXPANDED_BYTES)
    if object_count != len(raw_objects):
        raise ValueError("Knowledge archive Vault object count does not match")
    references: list[_VaultReference] = []
    seen: set[str] = set()
    observed_total = 0
    previous_path = ""
    for item in raw_objects:
        if not isinstance(item, dict) or set(item) != {
            "archive_path", "byte_size", "reference_kinds", "sha256", "vault_relative_path"
        }:
            raise ValueError("Knowledge archive Vault object manifest is invalid")
        relative = _safe_relative(item.get("vault_relative_path"))
        if relative in seen or (previous_path and relative <= previous_path):
            raise ValueError("Knowledge archive Vault objects are duplicate or unsorted")
        seen.add(relative)
        previous_path = relative
        if item.get("archive_path") != f"{VAULT_ARCHIVE_PREFIX}{relative}":
            raise ValueError("Knowledge archive Vault member mapping is invalid")
        raw_kinds = item.get("reference_kinds")
        if (
            not isinstance(raw_kinds, list)
            or not raw_kinds
            or any(not isinstance(kind, str) for kind in raw_kinds)
            or raw_kinds != sorted(set(raw_kinds))
            or any(kind not in ("asset", "representation") for kind in raw_kinds)
        ):
            raise ValueError("Knowledge archive Vault reference kinds are invalid")
        size = _integer(item.get("byte_size"), "Vault object size", maximum=MAX_ASSET_BYTES)
        digest = _hash(item.get("sha256"), "Vault object hash")
        observed_total += size
        references.append(_VaultReference(relative, digest, size, tuple(raw_kinds)))
    if observed_total != total_bytes:
        raise ValueError("Knowledge archive Vault byte accounting is invalid")
    return schema, database_hash, database_bytes, tuple(references), total_bytes


def _extract_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo, destination: Path, digest: str, size: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    observed = 0
    calculated = hashlib.sha256()
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output, archive.open(info, "r") as source:
            while chunk := source.read(_BUFFER_BYTES):
                observed += len(chunk)
                if observed > size:
                    raise ValueError("Knowledge archive member exceeds its manifest size")
                calculated.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    if observed != size or calculated.hexdigest() != digest:
        destination.unlink(missing_ok=True)
        raise ValueError("Knowledge archive member failed hash or size validation")


def _restore_target(path: str | Path, label: str, *, create_parent: bool = True) -> Path:
    requested = Path(path).expanduser()
    if requested.is_symlink():
        raise ValueError(f"{label} must not be a symbolic link")
    if create_parent:
        requested.parent.mkdir(parents=True, exist_ok=True)
    return requested.parent.resolve() / requested.name


def _validate_existing_tree(root: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Existing Knowledge Vault target is unsafe")
    for directory, names, files in os.walk(root, followlinks=False):
        base = Path(directory)
        for name in (*names, *files):
            target = base / name
            if target.is_symlink():
                raise ValueError("Existing Knowledge Vault contains a symbolic link")
            mode = target.stat().st_mode
            if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise ValueError("Existing Knowledge Vault contains a special file")


def _safe_remove_tree(path: Path) -> None:
    if path.exists():
        _validate_existing_tree(path)
        shutil.rmtree(path)


def _remove_known_target(path: Path, *, directory: bool) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink():
        raise ValueError("Knowledge lifecycle recovery target is unsafe")
    if directory:
        _safe_remove_tree(path)
    elif path.is_file():
        path.unlink()
    else:
        raise ValueError("Knowledge lifecycle recovery target is unsafe")


def _restore_backup_path(target: Path, transaction: str) -> Path:
    return target.with_name(f".{target.name}.restore-backup-{transaction}")


def _delete_tombstone_path(target: Path, transaction: str) -> Path:
    return target.with_name(f".{target.name}.delete-{transaction}")


def _recover_restore_marker(database: Path, vault: Path) -> None:
    marker = _read_mutation_marker(database, vault)
    if marker is None:
        return
    if marker["operation"] != "restore":
        raise ValueError(
            "An interrupted Knowledge deletion requires the same explicit deletion command"
        )
    transaction = str(marker["transaction"])
    phase = str(marker["phase"])
    sidecars = tuple(str(value) for value in marker["sidecars"])
    components: list[tuple[Path, bool, bool]] = [
        (database, bool(marker["database_present"]), False),
        *((Path(f"{database}{suffix}"), True, False) for suffix in sidecars),
        (vault, bool(marker["vault_present"]), True),
    ]

    if phase == "published":
        if database.is_symlink() or not database.is_file():
            raise ValueError("Published Knowledge restore database is unavailable")
        if vault.is_symlink() or not vault.is_dir():
            raise ValueError("Published Knowledge restore Vault is unavailable")
        _validate_existing_tree(vault)
        for target, existed, directory in components:
            backup = _restore_backup_path(target, transaction)
            if backup.exists() or backup.is_symlink():
                _remove_known_target(backup, directory=directory)
            if target != database and target != vault and target.exists():
                raise ValueError("Published Knowledge restore contains an unexpected sidecar")
        _clear_mutation_marker(database)
        return

    for target, existed, directory in reversed(components):
        backup = _restore_backup_path(target, transaction)
        backup_present = backup.exists() or backup.is_symlink()
        if backup_present:
            if backup.is_symlink():
                raise ValueError("Knowledge restore backup is unsafe")
            _remove_known_target(target, directory=directory)
            os.replace(backup, target)
            continue
        if existed:
            if target.is_symlink() or not target.exists():
                raise ValueError("Interrupted Knowledge restore cannot be recovered safely")
            if directory:
                _validate_existing_tree(target)
            elif not target.is_file():
                raise ValueError("Interrupted Knowledge restore target is unsafe")
        else:
            _remove_known_target(target, directory=directory)
    _fsync_directory(database.parent)
    if vault.parent != database.parent:
        _fsync_directory(vault.parent)
    _clear_mutation_marker(database)


def _recover_delete_marker(database: Path, vault: Path) -> tuple[str, ...]:
    marker = _read_mutation_marker(database, vault)
    if marker is None:
        return ()
    if marker["operation"] != "delete":
        raise ValueError("An interrupted Knowledge restore must be recovered by restore")
    transaction = str(marker["transaction"])
    phase = str(marker["phase"])
    present_sidecars = frozenset(str(value) for value in marker["sidecars"])
    components: list[tuple[str, Path, bool, bool]] = [
        ("database", database, bool(marker["database_present"]), False),
        *(
            (
                f"database_{suffix[1:]}",
                Path(f"{database}{suffix}"),
                suffix in present_sidecars,
                False,
            )
            for suffix in _DATABASE_SIDECARS[1:]
        ),
        ("vault", vault, bool(marker["vault_present"]), True),
    ]

    states: list[tuple[str, Path, Path, bool, bool, bool]] = []
    for label, target, expected, directory in components:
        tombstone = _delete_tombstone_path(target, transaction)
        target_present = target.exists() or target.is_symlink()
        tombstone_present = tombstone.exists() or tombstone.is_symlink()
        if phase == "prepared":
            if not expected:
                if target_present or tombstone_present:
                    raise ValueError(
                        "Prepared Knowledge deletion found an unexpected replacement target"
                    )
                states.append(
                    (label, target, tombstone, expected, directory, tombstone_present)
                )
                continue
            if target_present == tombstone_present:
                raise ValueError("Prepared Knowledge deletion state is ambiguous")
            candidate = target if target_present else tombstone
            if candidate.is_symlink():
                raise ValueError("Prepared Knowledge deletion component is unsafe")
            if directory:
                _validate_existing_tree(candidate)
            elif not candidate.is_file():
                raise ValueError("Prepared Knowledge deletion component is unsafe")
        else:
            if target_present:
                raise ValueError("Published Knowledge deletion found a replacement target")
            if not expected and tombstone_present:
                raise ValueError(
                    "Published Knowledge deletion found an unexpected tombstone"
                )
            if tombstone_present:
                if tombstone.is_symlink():
                    raise ValueError("Published Knowledge deletion tombstone is unsafe")
                if directory:
                    _validate_existing_tree(tombstone)
                elif not tombstone.is_file():
                    raise ValueError("Published Knowledge deletion tombstone is unsafe")
        states.append((label, target, tombstone, expected, directory, tombstone_present))

    if phase == "prepared":
        for _, target, tombstone, expected, _, tombstone_present in states:
            if not expected or not tombstone_present:
                continue
            if target.exists() or target.is_symlink():
                raise ValueError("Prepared Knowledge deletion state became ambiguous")
            os.replace(tombstone, target)
        _fsync_directory(database.parent)
        if vault.parent != database.parent:
            _fsync_directory(vault.parent)
        _clear_mutation_marker(database)
        return ()

    recovered = [label for label, _, _, expected, _, _ in states if expected]
    for _, target, tombstone, expected, directory, tombstone_present in states:
        if not expected or not tombstone_present:
            continue
        if target.exists() or target.is_symlink():
            raise ValueError("Published Knowledge deletion found a replacement target")
        _remove_known_target(tombstone, directory=directory)
    _fsync_directory(database.parent)
    if vault.parent != database.parent:
        _fsync_directory(vault.parent)
    for _, target, tombstone, expected, _, _ in states:
        if target.exists() or target.is_symlink():
            raise ValueError("Published Knowledge deletion found a replacement target")
        if tombstone.exists() or tombstone.is_symlink():
            if expected:
                raise ValueError("Published Knowledge deletion tombstone removal is incomplete")
            raise ValueError("Published Knowledge deletion found an unexpected tombstone")
    _clear_mutation_marker(database)
    return tuple(recovered)


def _install_restore(database_stage: Path, vault_stage: Path, database: Path, vault: Path, *, overwrite: bool) -> bool:
    existing_database = database.exists()
    existing_vault = vault.exists()
    if database.is_symlink() or vault.is_symlink():
        raise ValueError("Knowledge restore target must not be a symbolic link")
    if existing_database and not database.is_file():
        raise ValueError("Knowledge database restore target is not a regular file")
    if existing_vault:
        _validate_existing_tree(vault)
    sidecar_suffixes = [
        suffix for suffix in _DATABASE_SIDECARS[1:] if Path(f"{database}{suffix}").exists()
    ]
    sidecars = [Path(f"{database}{suffix}") for suffix in sidecar_suffixes]
    for sidecar in sidecars:
        if sidecar.is_symlink() or not sidecar.is_file():
            raise ValueError("Knowledge database sidecar restore target is unsafe")
    if (existing_database or existing_vault or sidecars) and not overwrite:
        raise ValueError("Knowledge restore targets exist; explicit overwrite is required")

    transaction = uuid.uuid4().hex
    marker: dict[str, object] = {
        "database": str(database),
        "database_present": existing_database,
        "operation": "restore",
        "phase": "prepared",
        "schema_version": "noruct.knowledge-mutation.v1",
        "sidecars": sorted(sidecar_suffixes),
        "transaction": transaction,
        "vault": str(vault),
        "vault_present": existing_vault,
    }
    _write_mutation_marker(database, marker)
    moved: list[tuple[Path, Path]] = []
    installed: list[Path] = []
    try:
        for target in ([database] if existing_database else []) + sidecars + ([vault] if existing_vault else []):
            backup = target.with_name(f".{target.name}.restore-backup-{transaction}")
            os.replace(target, backup)
            moved.append((target, backup))
        os.replace(vault_stage, vault)
        installed.append(vault)
        os.replace(database_stage, database)
        installed.append(database)
        database.chmod(0o600)
        vault.chmod(0o700)
        _fsync_directory(database.parent)
        if vault.parent != database.parent:
            _fsync_directory(vault.parent)
        marker["phase"] = "published"
        _write_mutation_marker(database, marker)
    except BaseException:
        for target in reversed(installed):
            if target.is_dir():
                _safe_remove_tree(target)
            else:
                target.unlink(missing_ok=True)
        for target, backup in reversed(moved):
            if backup.exists():
                os.replace(backup, target)
        _clear_mutation_marker(database)
        raise
    for _, backup in moved:
        if backup.is_dir():
            _safe_remove_tree(backup)
        else:
            backup.unlink(missing_ok=True)
    _clear_mutation_marker(database)
    return bool(moved)


def _restore_knowledge_archive_unlocked(
    archive_path: str | Path,
    database_path: str | Path,
    vault_path: str | Path,
    *,
    overwrite: bool = False,
) -> KnowledgeRestoreRecord:
    """Validate an entire archive before atomically publishing restored targets."""

    source = _require_regular(archive_path, "Knowledge archive")
    database = _restore_target(database_path, "Knowledge database restore target")
    vault = _restore_target(vault_path, "Knowledge Vault restore target")
    if database == vault or database.is_relative_to(vault) or vault.is_relative_to(database):
        raise ValueError("Knowledge database and Vault restore targets overlap")
    if source == database or source.is_relative_to(vault):
        raise ValueError("Knowledge archive must remain outside restore targets")

    database_stage_root = Path(tempfile.mkdtemp(prefix=".knowledge-restore-db-", dir=database.parent))
    vault_stage_root = Path(tempfile.mkdtemp(prefix=".knowledge-restore-vault-", dir=vault.parent))
    database_stage = database_stage_root / database.name
    vault_stage = vault_stage_root / vault.name
    vault_stage.mkdir(mode=0o700)
    archive_handle: BinaryIO | None = None
    try:
        archive_handle = _open_regular(source)
        archive_hash, _ = _sha256_handle(archive_handle)
        with zipfile.ZipFile(archive_handle, "r") as archive:
            members = _validated_members(archive)
            manifest, _ = _manifest(archive, members)
            schema, database_hash, database_bytes, references, vault_bytes = _parse_manifest(manifest)
            if schema != SCHEMA_VERSION:
                raise ValueError("Knowledge archive schema is unsupported by this runtime")
            expected_names = {MANIFEST_NAME, DATABASE_ARCHIVE_NAME} | {
                f"{VAULT_ARCHIVE_PREFIX}{reference.relative_path}" for reference in references
            }
            if set(members) != expected_names:
                raise ValueError("Knowledge archive contains missing or unreferenced members")
            database_info = members[DATABASE_ARCHIVE_NAME]
            if database_info.file_size != database_bytes:
                raise ValueError("Knowledge archive database size does not match its manifest")
            _extract_member(archive, database_info, database_stage, database_hash, database_bytes)
            for reference in references:
                info = members[f"{VAULT_ARCHIVE_PREFIX}{reference.relative_path}"]
                if info.file_size != reference.byte_size:
                    raise ValueError("Knowledge archive Vault size does not match its manifest")
                destination = vault_stage.joinpath(*PurePosixPath(reference.relative_path).parts)
                _extract_member(archive, info, destination, reference.sha256, reference.byte_size)

        observed_schema, observed_references = _schema_and_references(database_stage)
        if schema != observed_schema or references != observed_references:
            raise ValueError("Knowledge archive database and Vault manifest disagree")
        for reference in references:
            target = _safe_vault_file(vault_stage, reference.relative_path)
            digest, size = _sha256_file(target, maximum=reference.byte_size)
            if digest != reference.sha256 or size != reference.byte_size:
                raise ValueError("Restored Knowledge Vault verification failed")
        observed_archive_hash, _ = _sha256_handle(archive_handle)
        if observed_archive_hash != archive_hash:
            raise ValueError("Knowledge archive changed while it was being restored")
        overwritten = _install_restore(
            database_stage,
            vault_stage,
            database,
            vault,
            overwrite=overwrite,
        )
        return KnowledgeRestoreRecord(
            schema_version=ARCHIVE_SCHEMA,
            archive_sha256=archive_hash,
            database_integrity="ok",
            database_bytes=database_bytes,
            vault_object_count=len(references),
            vault_bytes=vault_bytes,
            overwritten=overwritten,
        )
    except (OSError, sqlite3.DatabaseError, zipfile.BadZipFile, zipfile.LargeZipFile) as error:
        raise ValueError("Knowledge archive restore validation failed") from error
    finally:
        if archive_handle is not None:
            archive_handle.close()
        shutil.rmtree(database_stage_root, ignore_errors=True)
        shutil.rmtree(vault_stage_root, ignore_errors=True)


def restore_knowledge_archive(
    archive_path: str | Path,
    database_path: str | Path,
    vault_path: str | Path,
    *,
    overwrite: bool = False,
) -> KnowledgeRestoreRecord:
    """Restore only while no store or other lifecycle operation is open."""

    database = _restore_target(database_path, "Knowledge database restore target")
    vault = _restore_target(vault_path, "Knowledge Vault restore target")
    with KnowledgeStateLock(database, mode="exclusive", create_parent=True):
        _recover_restore_marker(database, vault)
        return _restore_knowledge_archive_unlocked(
            archive_path,
            database,
            vault,
            overwrite=overwrite,
        )
