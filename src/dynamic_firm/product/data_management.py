from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping

from dynamic_firm import __version__
from dynamic_firm.runtime.models import utc_now
from dynamic_firm.runtime.redaction import redact_runtime_value


_WINDOWS_ABSOLUTE_PATH = re.compile(r"^[A-Za-z]:[\\/]")
_LOCAL_PATH_FRAGMENT = re.compile(
    r"(?:^|\s)(?:~[\\/]|/[A-Za-z0-9_.-]|\\\\|[A-Za-z]:[\\/])"
)


@dataclass(frozen=True, slots=True)
class StateExportRecord:
    schema_version: str
    data_scope: str
    source_exists: bool
    destination: str
    bytes_written: int
    sha256: str
    integrity_check: str
    sensitive_user_data_included: bool
    separate_knowledge_state_included: bool
    separate_knowledge_command: str


@dataclass(frozen=True, slots=True)
class StateDeletionRecord:
    schema_version: str
    data_scope: str
    deleted: bool
    deleted_files: tuple[str, ...]
    separate_knowledge_state_deleted: bool
    separate_knowledge_command: str
    residual_backup_warning: str


@dataclass(frozen=True, slots=True)
class SupportBundleRecord:
    schema_version: str
    destination: str
    bytes_written: int
    sha256: str
    secret_redaction_applied: bool
    raw_user_content_included: bool


def _require_regular_state(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if path.expanduser().is_symlink():
        raise ValueError("State path must not be a symbolic link")
    if not resolved.exists():
        raise ValueError(f"State database does not exist: {resolved}")
    if not resolved.is_file():
        raise ValueError(f"State path is not a regular file: {resolved}")
    return resolved


def _atomic_destination(destination: Path) -> tuple[Path, Path]:
    resolved = destination.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{resolved.name}.",
        suffix=".tmp",
        dir=resolved.parent,
    )
    os.close(descriptor)
    temporary_path = Path(temporary)
    temporary_path.chmod(0o600)
    return resolved, temporary_path


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def export_state_database(
    source: Path,
    destination: Path,
    *,
    overwrite: bool = False,
) -> StateExportRecord:
    source_path = _require_regular_state(source)
    destination_path = destination.expanduser().resolve()
    if source_path == destination_path:
        raise ValueError("State export destination must differ from the live database")
    if destination_path.exists() and not overwrite:
        raise ValueError("State export destination already exists; use --force to replace it")
    destination_path, temporary_path = _atomic_destination(destination_path)
    try:
        source_connection = sqlite3.connect(str(source_path))
        destination_connection = sqlite3.connect(str(temporary_path))
        try:
            source_connection.backup(destination_connection)
            integrity_row = destination_connection.execute(
                "PRAGMA integrity_check"
            ).fetchone()
            integrity = str(integrity_row[0]) if integrity_row else "missing"
            if integrity != "ok":
                raise ValueError(f"Exported state failed integrity check: {integrity}")
        finally:
            destination_connection.close()
            source_connection.close()
        _fsync_file(temporary_path)
        os.replace(temporary_path, destination_path)
        destination_path.chmod(0o600)
        payload = destination_path.read_bytes()
        return StateExportRecord(
            schema_version="noruct.state-export.v1",
            data_scope="runtime-company-state",
            source_exists=True,
            destination=str(destination_path),
            bytes_written=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            integrity_check=integrity,
            sensitive_user_data_included=True,
            separate_knowledge_state_included=False,
            separate_knowledge_command=(
                "noruct knowledge export DESTINATION --state STATE_DB"
            ),
        )
    finally:
        temporary_path.unlink(missing_ok=True)


def delete_state_database(source: Path) -> StateDeletionRecord:
    source_path = source.expanduser().resolve()
    if source.expanduser().is_symlink():
        raise ValueError("State path must not be a symbolic link")
    targets = (
        source_path,
        Path(f"{source_path}-wal"),
        Path(f"{source_path}-shm"),
    )
    deleted: list[str] = []
    for target in targets:
        if not target.exists():
            continue
        if not target.is_file() or target.is_symlink():
            raise ValueError(f"Refusing to delete non-regular state file: {target.name}")
        target.unlink()
        deleted.append(target.name)
    return StateDeletionRecord(
        schema_version="noruct.state-deletion.v1",
        data_scope="runtime-company-state",
        deleted=bool(deleted),
        deleted_files=tuple(deleted),
        separate_knowledge_state_deleted=False,
        separate_knowledge_command=(
            "noruct knowledge delete --state STATE_DB --confirm"
        ),
        residual_backup_warning=(
            "Deletion removes Noruct's runtime/company SQLite files only. It does not "
            "delete the separate Knowledge DB or Vault; use `noruct knowledge delete "
            "--state STATE_DB --confirm` for that data. Filesystem snapshots, Time "
            "Machine, copied exports, and provider-side records are outside this command."
        ),
    )


def _database_diagnostics(path: Path) -> Mapping[str, object]:
    if not path.exists():
        return {
            "present": False,
            "integrity_check": "not-present",
            "runtime_schema_version": None,
            "company_schema_version": None,
            "table_counts": {},
        }
    source_path = _require_regular_state(path)
    connection = sqlite3.connect(str(source_path))
    connection.row_factory = sqlite3.Row
    try:
        integrity_row = connection.execute("PRAGMA integrity_check").fetchone()
        tables = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

        def meta(table: str, key: str) -> int | None:
            if table not in tables:
                return None
            row = connection.execute(
                f"SELECT value FROM {table} WHERE key = ?",
                (key,),
            ).fetchone()
            return None if row is None else int(row["value"])

        allowlisted_counts = (
            "employee_runs",
            "run_events",
            "job_snapshots",
            "job_attempts",
            "job_mutations",
            "job_terminal_events",
            "organization_episodes",
            "workflow_patch_candidates",
            "roster_patch_candidates",
            "employee_skill_patch_candidates",
            "company_sessions",
            "company_turns",
        )
        counts = {}
        for table in allowlisted_counts:
            if table not in tables:
                continue
            counts[table] = int(
                connection.execute(
                    f"SELECT COUNT(*) AS count FROM {table}"
                ).fetchone()["count"]
            )
        return {
            "present": True,
            "integrity_check": (
                str(integrity_row[0]) if integrity_row is not None else "missing"
            ),
            "runtime_schema_version": meta("runtime_meta", "schema_version"),
            "company_schema_version": meta(
                "company_state_meta",
                "schema_version",
            ),
            "table_counts": counts,
        }
    finally:
        connection.close()


def _remove_local_paths(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _remove_local_paths(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_remove_local_paths(item) for item in value]
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if stripped.startswith(("http://", "https://")):
        return value
    if (
        stripped.startswith(("~", "/", "file://", "\\\\"))
        or _WINDOWS_ABSOLUTE_PATH.match(stripped)
        or _LOCAL_PATH_FRAGMENT.search(value)
    ):
        return "«redacted:local-path»"
    home = str(Path.home())
    if home and home in value:
        return value.replace(home, "«redacted:local-path»")
    return value


def create_support_bundle(
    state_path: Path,
    config_path: Path,
    config: Mapping[str, object],
    destination: Path,
    *,
    overwrite: bool = False,
) -> SupportBundleRecord:
    destination_path = destination.expanduser().resolve()
    if destination_path.exists() and not overwrite:
        raise ValueError("Support bundle destination already exists; use --force to replace it")
    safe_config = _remove_local_paths(redact_runtime_value(config))
    from dynamic_firm.knowledge.lifecycle import knowledge_diagnostics
    from dynamic_firm.knowledge.store import knowledge_state_path, knowledge_vault_path

    knowledge_database = knowledge_state_path(state_path)
    knowledge_vault = knowledge_vault_path(knowledge_database)
    payload = {
        "schema_version": "noruct.support-bundle.v1",
        "created_at": utc_now().isoformat(),
        "noruct_version": __version__,
        "platform": {
            "python": sys.version.split()[0],
            "system": os.uname().sysname if hasattr(os, "uname") else os.name,
        },
        "configuration": {
            "present": config_path.expanduser().exists(),
            "values": safe_config,
        },
        "state": _database_diagnostics(state_path.expanduser().resolve()),
        "knowledge": asdict(
            knowledge_diagnostics(knowledge_database, knowledge_vault)
        ),
        "privacy": {
            "secret_redaction_applied": True,
            "raw_user_content_included": False,
            "state_path_included": False,
            "config_path_included": False,
            "local_path_values_included": False,
            "environment_values_included": False,
        },
    }
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    ).encode("utf-8") + b"\n"
    destination_path, temporary_path = _atomic_destination(destination_path)
    try:
        temporary_path.write_bytes(serialized)
        _fsync_file(temporary_path)
        os.replace(temporary_path, destination_path)
        destination_path.chmod(0o600)
    finally:
        temporary_path.unlink(missing_ok=True)
    return SupportBundleRecord(
        schema_version="noruct.support-bundle-record.v1",
        destination=str(destination_path),
        bytes_written=len(serialized),
        sha256=hashlib.sha256(serialized).hexdigest(),
        secret_redaction_applied=True,
        raw_user_content_included=False,
    )
