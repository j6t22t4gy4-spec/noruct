"""Non-secret configuration lifecycle for the approval-gated container worker."""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from dynamic_firm.runtime.container_workspace import ContainerWorkspaceConfig, container_config_from_settings


CONTAINER_SETTINGS_SCHEMA = "noruct.container-workspace-settings.v1"
_TABLE = "container"
_HEADER = re.compile(r"(?m)^\[([A-Za-z0-9_.-]+)\][ \t]*(?:\r?\n|$)")


@dataclass(frozen=True, slots=True)
class ContainerSettings:
    image: str
    programs: Mapping[str, tuple[str, ...]]
    docker_command: str = "docker"
    timeout_seconds: float = 120.0
    memory_limit_mb: int = 2048
    cpu_limit: float = 2.0
    pids_limit: int = 256
    max_output_bytes: int = 64_000

    def to_mapping(self) -> Mapping[str, object]:
        return {
            "enabled": True,
            "image": self.image,
            "docker_command": self.docker_command,
            "timeout_seconds": self.timeout_seconds,
            "memory_limit_mb": self.memory_limit_mb,
            "cpu_limit": self.cpu_limit,
            "pids_limit": self.pids_limit,
            "max_output_bytes": self.max_output_bytes,
            "programs": {key: list(value) for key, value in self.programs.items()},
        }

    def validated_runtime_config(self) -> ContainerWorkspaceConfig:
        config = container_config_from_settings({"container": self.to_mapping()})
        if config is None:
            raise ValueError("Container settings did not enable a worker")
        return config


def _table_range(text: str) -> tuple[int, int] | None:
    headers = tuple(_HEADER.finditer(text))
    index = next((position for position, item in enumerate(headers) if item.group(1) == _TABLE), None)
    if index is None:
        return None
    start, end = headers[index].start(), len(text)
    for item in headers[index + 1 :]:
        name = item.group(1)
        if name != _TABLE and not name.startswith(f"{_TABLE}."):
            end = item.start()
            break
    return start, end


def _without_table(text: str) -> str:
    location = _table_range(text)
    if location is None:
        return text.strip()
    start, end = location
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


def container_table_text(settings: ContainerSettings) -> str:
    config = settings.validated_runtime_config()
    quote = lambda value: json.dumps(value, ensure_ascii=False)
    lines = [
        "[container]", "enabled = true", f"image = {quote(config.image)}",
        f"docker_command = {quote(config.docker_command)}", f"timeout_seconds = {config.timeout_seconds:g}",
        f"memory_limit_mb = {config.memory_limit_mb}", f"cpu_limit = {config.cpu_limit:g}",
        f"pids_limit = {config.pids_limit}", f"max_output_bytes = {config.max_output_bytes}", "",
        "[container.programs]",
    ]
    lines.extend(f"{name} = {quote(list(command))}" for name, command in sorted(config.programs.items()))
    lines.append("")
    return "\n".join(lines)


def write_container_settings(path: Path, settings: ContainerSettings) -> Path:
    target = path.expanduser().resolve()
    existing = target.read_text(encoding="utf-8") if target.is_file() else ""
    remainder = _without_table(existing)
    return _atomic_write(target, (remainder + "\n\n" if remainder else "") + container_table_text(settings))


def remove_container_settings(path: Path) -> bool:
    target = path.expanduser().resolve()
    if not target.is_file():
        return False
    existing = target.read_text(encoding="utf-8")
    if _table_range(existing) is None:
        return False
    remainder = _without_table(existing)
    _atomic_write(target, remainder + ("\n" if remainder else ""))
    return True


def container_status(config: ContainerWorkspaceConfig | None) -> Mapping[str, Any]:
    if config is None:
        return {"schema": CONTAINER_SETTINGS_SCHEMA, "enabled": False, "ready": False, "authority": "no_container_workspace", "next_action": "noruct environment container-configure"}
    command = Path(config.docker_command).expanduser()
    executable = (
        command.is_absolute() and command.is_file() and os.access(command, os.X_OK)
    ) or (not command.is_absolute() and shutil.which(config.docker_command) is not None)
    return {
        "schema": CONTAINER_SETTINGS_SCHEMA,
        "enabled": True,
        "ready": executable,
        "image": config.image,
        "program_ids": sorted(config.programs),
        "docker_command": config.docker_command,
        "network": "disabled",
        "root_filesystem": "read_only",
        "permission_mode_required": "ask",
        "authority": "per_tool_durable_approval_only",
        "automatic_activation": False,
        "next_action": "Run Noruct with --permission-mode ask; every container tool call requires approval.",
    }
