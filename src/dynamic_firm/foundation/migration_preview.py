"""Historical employee-state compatibility inventory and no-transform backup.

Noruct has one executable Employee Runtime. This module keeps the former
cutover entry point only to inspect older state safely: it never selects an
alternate runtime, rewrites conversation data, or changes runtime config. An
apply validates the local ledger, produces a consistent SQLite backup,
rehearses that backup read-only, and records the compatibility receipt beside
the backup.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .source import foundation_cutover_status
from dynamic_firm.runtime.store import SCHEMA_VERSION as CURRENT_RUNTIME_SCHEMA_VERSION


SCHEMA = "noruct.employee-runtime-state-compatibility-preview.v2"
APPLY_SCHEMA = "noruct.employee-runtime-state-compatibility-apply.v2"
_TERMINAL_RUN_STATUSES = ("SUCCEEDED", "FAILED", "CANCELLED", "BUDGET_EXHAUSTED")
_SUPPORTED_RUNTIME_SCHEMAS = tuple(range(1, CURRENT_RUNTIME_SCHEMA_VERSION + 1))


class MigrationPreviewError(ValueError):
    """A state file cannot safely be inspected as a read-only migration input."""


class MigrationApplyError(ValueError):
    """A no-transform migration cannot safely create its backup receipt."""


def _schema_compatibility(runtime_schema_version: int | None) -> dict[str, Any]:
    """Describe migration readability without attempting a schema upgrade."""

    if runtime_schema_version is None:
        return {
            "state": "REVIEW_REQUIRED",
            "observed_schema_version": None,
            "current_supported_schema_version": CURRENT_RUNTIME_SCHEMA_VERSION,
            "migration_readable": False,
        }
    if runtime_schema_version in _SUPPORTED_RUNTIME_SCHEMAS:
        return {
            "state": "SUPPORTED_PENDING_AUTHORIZATION",
            "observed_schema_version": runtime_schema_version,
            "current_supported_schema_version": CURRENT_RUNTIME_SCHEMA_VERSION,
            "migration_readable": True,
        }
    if runtime_schema_version > CURRENT_RUNTIME_SCHEMA_VERSION:
        state = "UNSUPPORTED_FUTURE_SCHEMA"
    else:
        state = "UNSUPPORTED_SCHEMA"
    return {
        "state": state,
        "observed_schema_version": runtime_schema_version,
        "current_supported_schema_version": CURRENT_RUNTIME_SCHEMA_VERSION,
        "migration_readable": False,
    }


def _read_only_connection(path: Path) -> sqlite3.Connection:
    if path.is_symlink():
        raise MigrationPreviewError("migration preview refuses a symlinked state path")
    if not path.is_file():
        raise MigrationPreviewError("migration preview state path is not a regular file")
    try:
        connection = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro",
            uri=True,
            check_same_thread=False,
        )
    except sqlite3.Error as exc:
        raise MigrationPreviewError("migration preview could not open state read-only") from exc
    connection.row_factory = sqlite3.Row
    return connection


def _has_table(connection: sqlite3.Connection, name: str) -> bool:
    return bool(
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
    )


def _state_inventory(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "state": "ABSENT",
            "database_created": False,
            "runtime_schema_version": None,
            "schema_compatibility": {
                "state": "ABSENT",
                "observed_schema_version": None,
                "current_supported_schema_version": CURRENT_RUNTIME_SCHEMA_VERSION,
                "migration_readable": False,
            },
            "employee_session_records": 0,
            "employee_session_employees": 0,
            "employee_session_message_count": 0,
            "employee_session_bytes": 0,
            "run_status_counts": {},
            "active_employee_runs": 0,
        }

    connection = _read_only_connection(path)
    try:
        runtime_schema_version: int | None = None
        if _has_table(connection, "runtime_meta"):
            row = connection.execute(
                "SELECT value FROM runtime_meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is not None:
                try:
                    runtime_schema_version = int(row["value"])
                except (TypeError, ValueError):
                    raise MigrationPreviewError("migration preview found an invalid runtime schema") from None

        session_counts = {
            "employee_session_records": 0,
            "employee_session_employees": 0,
            "employee_session_message_count": 0,
            "employee_session_bytes": 0,
        }
        if _has_table(connection, "employee_session_state"):
            row = connection.execute(
                """
                SELECT COUNT(*) AS records,
                       COUNT(DISTINCT employee_id) AS employees,
                       COALESCE(SUM(message_count), 0) AS messages,
                       COALESCE(SUM(byte_length), 0) AS bytes
                FROM employee_session_state
                """
            ).fetchone()
            assert row is not None
            session_counts = {
                "employee_session_records": int(row["records"]),
                "employee_session_employees": int(row["employees"]),
                "employee_session_message_count": int(row["messages"]),
                "employee_session_bytes": int(row["bytes"]),
            }

        status_counts: dict[str, int] = {}
        if _has_table(connection, "employee_runs"):
            for row in connection.execute(
                "SELECT status, COUNT(*) AS count FROM employee_runs GROUP BY status ORDER BY status"
            ):
                status_counts[str(row["status"])] = int(row["count"])
        active = sum(
            count for status, count in status_counts.items() if status not in _TERMINAL_RUN_STATUSES
        )
        return {
            "state": "READ_ONLY_INVENTORIED",
            "database_created": False,
            "runtime_schema_version": runtime_schema_version,
            "schema_compatibility": _schema_compatibility(runtime_schema_version),
            **session_counts,
            "run_status_counts": status_counts,
            "active_employee_runs": active,
        }
    except sqlite3.Error as exc:
        raise MigrationPreviewError("migration preview could not inspect runtime state") from exc
    finally:
        connection.close()


def _migration_blockers(inventory: dict[str, Any], cutover: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    if inventory["state"] == "ABSENT":
        blockers.append("NO_STATE_TO_MIGRATE")
    if inventory["active_employee_runs"]:
        blockers.append("ACTIVE_EMPLOYEE_RUNS_PRESENT")
    schema_state = inventory["schema_compatibility"]["state"]
    if schema_state == "REVIEW_REQUIRED":
        blockers.append("RUNTIME_SCHEMA_REVIEW_REQUIRED")
    elif schema_state == "UNSUPPORTED_FUTURE_SCHEMA":
        blockers.append("RUNTIME_SCHEMA_UNSUPPORTED_FUTURE")
    elif schema_state == "UNSUPPORTED_SCHEMA":
        blockers.append("RUNTIME_SCHEMA_UNSUPPORTED")
    if not cutover["technical_default_ready"]:
        blockers.append("RUNTIME_DEPENDENCY_UNAVAILABLE")
    return blockers


def _portable_inventory(inventory: dict[str, Any]) -> dict[str, Any]:
    """Keep only aggregate facts that a backup rehearsal must preserve."""

    return {
        key: inventory[key]
        for key in (
            "runtime_schema_version",
            "employee_session_records",
            "employee_session_employees",
            "employee_session_message_count",
            "employee_session_bytes",
            "run_status_counts",
            "active_employee_runs",
        )
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _backup_directory(path: Path, requested: str | Path | None) -> Path:
    target = (
        Path(requested).expanduser()
        if requested is not None
        else path.parent / "migration-backups"
    ).resolve()
    if target.exists():
        if target.is_symlink() or not target.is_dir():
            raise MigrationApplyError("migration backup directory must be a real directory")
    else:
        target.mkdir(mode=0o700, parents=True)
    return target


def _write_receipt(path: Path, payload: dict[str, Any]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".noruct-migration-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _consistent_backup(state_path: Path, backup_directory: Path) -> Path:
    if state_path.is_symlink() or not state_path.is_file():
        raise MigrationApplyError("migration apply requires a regular SQLite state file")
    before_hash = _sha256(state_path)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f"{state_path.stem}.pre-noruct-",
        suffix=".sqlite",
        dir=backup_directory,
    )
    os.close(descriptor)
    backup_path = Path(temporary_name)
    try:
        source = _read_only_connection(state_path)
        destination = sqlite3.connect(backup_path)
        try:
            source.backup(destination)
            destination.commit()
            row = destination.execute("PRAGMA integrity_check").fetchone()
            if row is None or str(row[0]).lower() != "ok":
                raise MigrationApplyError("migration backup integrity check failed")
        finally:
            destination.close()
            source.close()
        os.chmod(backup_path, 0o600)
        if _sha256(state_path) != before_hash:
            raise MigrationApplyError("state changed while the migration backup was created")
        return backup_path
    except BaseException:
        backup_path.unlink(missing_ok=True)
        raise


def preview_employee_runtime_migration(state_path: str | Path) -> dict[str, Any]:
    """Return a non-mutating historical-state compatibility plan.

    The result intentionally contains only aggregate metadata.  In particular
    it does not reveal raw session keys, message history, prompts, tool data,
    credentials, or a path that can automatically apply the migration.
    """

    path = Path(state_path).expanduser()
    inventory = _state_inventory(path)
    cutover = foundation_cutover_status()
    blockers = _migration_blockers(inventory, cutover)
    return {
        "schema_version": SCHEMA,
        "execution": "READ_ONLY_PREVIEW",
        "network_access": "NOT_REQUESTED",
        "provider_calls": 0,
        "state_path": str(path.resolve()),
        "inventory": inventory,
        "transition": {
            "historical_state_label": "historical_employee_state",
            "runtime": "noruct",
            "runtime_changed": False,
            "apply_available": not blockers,
            "apply_status": "READY" if not blockers else "BLOCKED",
            "blockers": tuple(blockers),
            "shared_employee_session_projection": True,
            "state_preserved": True,
        },
        "runtime_rollback": {
            "available": False,
            "profile": None,
            "reason": "one_executable_runtime",
        },
        "backup": {
            "restore_required_for_state_recovery": False,
            "automatic": False,
        },
        "cutover": cutover,
        "privacy": {
            "raw_session_keys_exposed": False,
            "message_content_exposed": False,
            "tool_or_credential_content_exposed": False,
        },
    }


def apply_employee_runtime_migration(
    state_path: str | Path,
    *,
    backup_directory: str | Path | None = None,
) -> dict[str, Any]:
    """Create a backup-verified, no-transform compatibility receipt.

    There is intentionally no message rewrite, schema mutation, profile write,
    runtime selection, network access, or provider call. The backup and receipt
    make that fact independently auditable.
    """

    path = Path(state_path).expanduser().resolve()
    preview = preview_employee_runtime_migration(path)
    blockers = list(preview["transition"]["blockers"])
    if blockers:
        raise MigrationApplyError(
            "migration apply is blocked: " + ", ".join(blockers)
        )
    backup_root = _backup_directory(path, backup_directory)
    backup_path = _consistent_backup(path, backup_root)
    try:
        backup_inventory = _state_inventory(backup_path)
        if _portable_inventory(backup_inventory) != _portable_inventory(preview["inventory"]):
            raise MigrationApplyError("migration backup rehearsal did not preserve aggregate state")
        receipt_path = backup_path.with_suffix(".migration.json")
        receipt = {
            "schema_version": APPLY_SCHEMA,
            "status": "APPLIED_NO_DATA_TRANSFORM",
            "applied_at": datetime.now(timezone.utc).isoformat(),
            "state_path": str(path),
            "state_sha256": _sha256(path),
            "backup_path": str(backup_path),
            "backup_sha256": _sha256(backup_path),
            "backup_rehearsal": "read_only_inventory_match",
            "transition": {
                "historical_state_label": "historical_employee_state",
                "runtime": "noruct",
                "shared_employee_session_projection": True,
                "data_transform": "NONE",
                "config_changed": False,
                "default_runtime": "noruct",
                "runtime_rollback_available": False,
            },
            "inventory": _portable_inventory(preview["inventory"]),
            "privacy": {
                "message_content_exposed": False,
                "raw_session_keys_exposed": False,
                "tool_or_credential_content_exposed": False,
            },
        }
        _write_receipt(receipt_path, receipt)
        return {
            **receipt,
            "receipt_path": str(receipt_path),
            "provider_calls": 0,
            "network_access": "NOT_REQUESTED",
        }
    except BaseException:
        backup_path.unlink(missing_ok=True)
        raise
