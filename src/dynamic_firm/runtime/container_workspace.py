"""Approval-gated local container workspace execution.

The Company never receives a generic Docker shell.  An operator selects one
image and a small command allowlist; each Employee call is still a high-risk,
durably approved ToolIntent.  This is deliberately a narrow adaptation of the
registered Docker-environment reference: Noruct owns the command boundary,
workspace mount and all state/approval authority.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .models import IdempotencyMode, ToolEffect, ToolRisk
from .ports import CancellationToken, OperationCancelled
from .tools import ToolDefinition, ToolExecutionError, ToolValidationError


_IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


@dataclass(frozen=True, slots=True)
class ContainerWorkspaceConfig:
    """Non-secret operator configuration for one local Docker sandbox."""

    image: str
    programs: Mapping[str, tuple[str, ...]]
    docker_command: str = "docker"
    timeout_seconds: float = 120.0
    memory_limit_mb: int = 2048
    cpu_limit: float = 2.0
    pids_limit: int = 256
    max_output_bytes: int = 64_000


@dataclass(frozen=True, slots=True)
class ContainerWorkspaceVerification:
    """A non-executing Docker engine and image preflight result."""

    image: str
    runtime_available: bool
    image_present: bool
    image_id: str | None
    image_reference_pinned: bool
    authority: str = "operator_confirmed_local_container_metadata_check_no_container_or_company_job_started"

    def to_dict(self) -> dict[str, object]:
        return {
            "image": self.image,
            "runtime_available": self.runtime_available,
            "image_present": self.image_present,
            "image_id": self.image_id,
            "image_reference_pinned": self.image_reference_pinned,
            "authority": self.authority,
            "network": "not_enabled_by_preflight",
            "container_execution": "NOT_STARTED",
        }


def container_config_from_settings(settings: Mapping[str, object]) -> ContainerWorkspaceConfig | None:
    value = settings.get("container")
    if value is None:
        return None
    if not isinstance(value, Mapping) or value.get("enabled") is not True:
        return None
    image = value.get("image")
    docker_command = value.get("docker_command", "docker")
    programs = value.get("programs")
    if (
        not isinstance(image, str)
        or not image.strip()
        or len(image.encode()) > 255
        or any(char.isspace() or char == "\x00" for char in image)
        or not isinstance(docker_command, str)
        or not docker_command.strip()
        or len(docker_command.encode()) > 1024
        or any(char.isspace() or char == "\x00" for char in docker_command)
        or not isinstance(programs, Mapping)
        or not programs
    ):
        raise ValueError("container requires a bounded image, Docker command, and non-empty program allowlist")
    normalized: dict[str, tuple[str, ...]] = {}
    for program_id, command in programs.items():
        if not isinstance(program_id, str) or not _IDENTIFIER.fullmatch(program_id):
            raise ValueError("container.programs must use bounded identifiers")
        if not isinstance(command, list) or not 1 <= len(command) <= 8 or not all(isinstance(item, str) for item in command):
            raise ValueError("container programs must be arrays of one to eight strings")
        if any(not item or "\x00" in item or "\n" in item or "\r" in item or len(item.encode()) > 1024 for item in command):
            raise ValueError("container program values are invalid")
        normalized[program_id] = tuple(command)
    timeout = float(value.get("timeout_seconds", 120.0))
    memory = int(value.get("memory_limit_mb", 2048))
    cpus = float(value.get("cpu_limit", 2.0))
    pids = int(value.get("pids_limit", 256))
    output = int(value.get("max_output_bytes", 64_000))
    if not 1 <= timeout <= 300 or not 64 <= memory <= 16_384 or not 0.1 <= cpus <= 16 or not 32 <= pids <= 4096 or not 1024 <= output <= 1_000_000:
        raise ValueError("container limits are outside the approved bounded range")
    return ContainerWorkspaceConfig(
        image=image,
        programs=normalized,
        docker_command=docker_command,
        timeout_seconds=timeout,
        memory_limit_mb=memory,
        cpu_limit=cpus,
        pids_limit=pids,
        max_output_bytes=output,
    )


def verify_container_workspace(
    config: ContainerWorkspaceConfig,
    *,
    runner: object = subprocess.run,
) -> ContainerWorkspaceVerification:
    """Check only Docker engine metadata and a locally available image.

    The probe calls `docker version` then `docker image inspect`; neither
    starts a container, pulls an image, mounts a workspace, forwards an
    environment value, or creates a Company Job.  A tag may be usable but is
    reported as non-pinned so an operator can decide whether to replace it
    with an immutable digest reference.
    """

    configured = Path(config.docker_command).expanduser()
    executable = str(configured) if configured.is_absolute() and configured.is_file() and os.access(configured, os.X_OK) else shutil.which(config.docker_command)
    pinned = "@sha256:" in config.image
    if not executable:
        return ContainerWorkspaceVerification(config.image, False, False, None, pinned)
    try:
        engine = runner(
            [executable, "version", "--format", "{{.Server.Version}}"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=min(30.0, config.timeout_seconds), check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ContainerWorkspaceVerification(config.image, False, False, None, pinned)
    if engine.returncode != 0:
        return ContainerWorkspaceVerification(config.image, False, False, None, pinned)
    try:
        image = runner(
            [executable, "image", "inspect", "--format", "{{.Id}}", config.image],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=min(30.0, config.timeout_seconds), check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ContainerWorkspaceVerification(config.image, True, False, None, pinned)
    image_id = image.stdout.strip() if isinstance(image.stdout, str) else ""
    if image.returncode != 0 or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        return ContainerWorkspaceVerification(config.image, True, False, None, pinned)
    return ContainerWorkspaceVerification(config.image, True, True, image_id, pinned)


class ContainerWorkspaceTools:
    """Expose an explicit local image/program allowlist through ToolExecutor."""

    def __init__(self, config: ContainerWorkspaceConfig, workspace: Path) -> None:
        self.config = config
        self.workspace = workspace.expanduser().resolve()

    def definition(self) -> ToolDefinition:
        def validate(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
            if set(arguments) != {"program_id", "arguments"}:
                raise ToolValidationError("run_container_workspace_program requires program_id and arguments")
            program_id = arguments.get("program_id")
            values = arguments.get("arguments")
            if not isinstance(program_id, str) or program_id not in self.config.programs:
                raise ToolValidationError("Container program is not in the operator allowlist")
            if not isinstance(values, list) or len(values) > 16 or not all(isinstance(item, str) for item in values):
                raise ToolValidationError("Container program accepts at most 16 string arguments")
            if any("\x00" in item or "\n" in item or "\r" in item or len(item.encode()) > 1024 for item in values):
                raise ToolValidationError("Container program argument is invalid")
            return {"program_id": program_id, "arguments": tuple(values)}

        async def handle(arguments: Mapping[str, object], cancellation: CancellationToken) -> str:
            return await self._execute(str(arguments["program_id"]), tuple(arguments["arguments"]), cancellation)

        return ToolDefinition(
            name="run_container_workspace_program",
            description="Run one operator-allowlisted command in the configured local container workspace.",
            input_schema={"type": "object", "properties": {"program_id": {"type": "string"}, "arguments": {"type": "array", "items": {"type": "string"}, "maxItems": 16}}, "required": ["program_id", "arguments"], "additionalProperties": False},
            effect=ToolEffect.EXECUTE,
            risk=ToolRisk.HIGH,
            idempotency_mode=IdempotencyMode.CALL_KEY,
            validator=validate,
            resource_key=lambda value: f"container-workspace:{self.config.image}:{value['program_id']}",
            handler=handle,
            timeout_ms=int(self.config.timeout_seconds * 1000),
            requires_approval=True,
            approval_preview=lambda value: f"Container {self.config.image} / {value['program_id']} in {self.workspace}",
        )

    def _command(self, program_id: str, arguments: tuple[str, ...]) -> list[str]:
        cfg = self.config
        # No network, elevated Linux capabilities, writable root filesystem,
        # host PID namespace, or forwarded environment variables are granted.
        uid = getattr(os, "getuid", lambda: 1000)()
        gid = getattr(os, "getgid", lambda: 1000)()
        return [
            cfg.docker_command,
            "run", "--rm", "--init", "--network", "none", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--pids-limit", str(cfg.pids_limit),
            "--memory", f"{cfg.memory_limit_mb}m", "--cpus", f"{cfg.cpu_limit:g}",
            "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=256m",
            "--workdir", "/workspace", "--mount", f"type=bind,src={self.workspace},dst=/workspace",
            "--user", f"{uid}:{gid}", cfg.image,
            *self.config.programs[program_id], *arguments,
        ]

    async def _execute(self, program_id: str, arguments: tuple[str, ...], cancellation: CancellationToken) -> str:
        command = self._command(program_id, arguments)
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=(os.name == "posix"),
            )
        except OSError as exc:
            raise ToolExecutionError(f"Container runtime could not start: {exc.strerror or exc}") from exc
        waiter, cancelled = asyncio.create_task(process.communicate()), asyncio.create_task(cancellation.wait())
        try:
            done, _ = await asyncio.wait({waiter, cancelled}, timeout=self.config.timeout_seconds, return_when=asyncio.FIRST_COMPLETED)
            if cancelled in done:
                await self._terminate(process)
                raise OperationCancelled(cancellation.reason or "Container workspace cancelled")
            if waiter not in done:
                await self._terminate(process)
                raise ToolExecutionError("Container program timed out")
            output, _ = waiter.result()
        finally:
            cancelled.cancel()
            if process.returncode is None:
                await self._terminate(process)
        text = output.decode("utf-8", errors="replace")[: self.config.max_output_bytes]
        if process.returncode != 0:
            raise ToolExecutionError(f"Container program exited with status {process.returncode}")
        return json.dumps({"image": self.config.image, "program_id": program_id, "exit_code": process.returncode, "output": text}, ensure_ascii=False, sort_keys=True)

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            if os.name == "posix" and process.pid:
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
            await asyncio.wait_for(process.wait(), timeout=1)
        except (ProcessLookupError, TimeoutError):
            try:
                process.kill()
            except ProcessLookupError:
                return
            await process.wait()
