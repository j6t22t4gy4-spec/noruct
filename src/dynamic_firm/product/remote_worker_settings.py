"""Non-secret operator configuration for the bounded remote Company worker.

This config never creates a network connection.  It only records an operator's
reference to a previously verified snapshot-transfer receipt and the small
remote program allowlist that the native ToolExecutor may later expose.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from dynamic_firm.runtime.remote_workspace import (
    RemoteWorkspaceWorkerConfig,
    remote_worker_config_from_settings,
)


REMOTE_WORKER_SETTINGS_SCHEMA = "noruct.remote-worker-settings.v1"
_TABLE = "remote_worker"
_HEADER = re.compile(r"(?m)^\[([A-Za-z0-9_.-]+)\][ \t]*(?:\r?\n|$)")


@dataclass(frozen=True, slots=True)
class RemoteWorkerSettings:
    target_id: str
    receipt: Path
    programs: Mapping[str, str]
    identity_file: Path | None = None
    timeout_seconds: float = 120.0

    def to_mapping(self) -> Mapping[str, object]:
        value: dict[str, object] = {
            "enabled": True,
            "target_id": self.target_id,
            "receipt": str(self.receipt.expanduser().resolve()),
            "programs": dict(self.programs),
            "timeout_seconds": self.timeout_seconds,
        }
        if self.identity_file is not None:
            value["identity_file"] = str(self.identity_file.expanduser().resolve())
        return value

    def validated_runtime_config(self) -> RemoteWorkspaceWorkerConfig:
        config = remote_worker_config_from_settings({"remote_worker": self.to_mapping()})
        if config is None:  # Defensive: enabled is always written above.
            raise ValueError("Remote worker configuration did not enable a worker")
        return config


def _table_range(text: str) -> tuple[int, int] | None:
    headers = tuple(_HEADER.finditer(text))
    start_index = next(
        (index for index, item in enumerate(headers) if item.group(1) == _TABLE),
        None,
    )
    if start_index is None:
        return None
    start = headers[start_index].start()
    end = len(text)
    for item in headers[start_index + 1 :]:
        name = item.group(1)
        if name != _TABLE and not name.startswith(f"{_TABLE}."):
            end = item.start()
            break
    return start, end


def _without_table(text: str) -> str:
    section = _table_range(text)
    if section is None:
        return text.strip()
    start, end = section
    return (text[:start] + text[end:]).strip()


def _atomic_write(path: Path, value: str) -> Path:
    target = path.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".noruct-config-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return target


def remote_worker_table_text(settings: RemoteWorkerSettings) -> str:
    config = settings.validated_runtime_config()
    quote = lambda value: json.dumps(value, ensure_ascii=False)
    fields = [
        "[remote_worker]",
        "enabled = true",
        f"target_id = {quote(config.target_id)}",
        f"receipt = {quote(str(settings.receipt.expanduser().resolve()))}",
    ]
    if config.identity_file is not None:
        fields.append(f"identity_file = {quote(str(config.identity_file))}")
    fields.extend(
        (
            f"timeout_seconds = {config.timeout_seconds:g}",
            "",
            "[remote_worker.programs]",
            *(f"{name} = {quote(program)}" for name, program in sorted(config.programs.items())),
            "",
        )
    )
    return "\n".join(fields)


def write_remote_worker_settings(path: Path, settings: RemoteWorkerSettings) -> Path:
    target = path.expanduser().resolve()
    existing = target.read_text(encoding="utf-8") if target.is_file() else ""
    remainder = _without_table(existing)
    return _atomic_write(
        target,
        (remainder + "\n\n" if remainder else "") + remote_worker_table_text(settings),
    )


def remove_remote_worker_settings(path: Path) -> bool:
    target = path.expanduser().resolve()
    if not target.is_file():
        return False
    existing = target.read_text(encoding="utf-8")
    if _table_range(existing) is None:
        return False
    remainder = _without_table(existing)
    _atomic_write(target, remainder + ("\n" if remainder else ""))
    return True


def remote_worker_status(config: RemoteWorkspaceWorkerConfig | None) -> Mapping[str, Any]:
    if config is None:
        return {
            "schema": REMOTE_WORKER_SETTINGS_SCHEMA,
            "enabled": False,
            "ready": False,
            "authority": "no_remote_company_worker",
            "next_action": "noruct environment worker-configure",
        }
    return {
        "schema": REMOTE_WORKER_SETTINGS_SCHEMA,
        "enabled": True,
        "ready": True,
        "target_id": config.target_id,
        "host": config.host,
        "port": config.port,
        "snapshot_sha256": config.snapshot_sha256,
        "program_ids": sorted(config.programs),
        "permission_mode_required": "ask",
        "authority": "per_tool_durable_approval_only",
        "automatic_activation": False,
        "reverse_sync": False,
        "next_action": "Run Noruct with --permission-mode ask; every remote tool call requires approval.",
    }
