"""Approval-gated remote workspace worker behind the native ToolExecutor."""
from __future__ import annotations

import asyncio
import json
import os
import shlex
import signal
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
from typing import Any, Mapping

from .models import IdempotencyMode, ToolEffect, ToolRisk
from .ports import CancellationToken, OperationCancelled
from .tools import ToolDefinition, ToolExecutionError, ToolValidationError


@dataclass(frozen=True, slots=True)
class RemoteWorkspaceWorkerConfig:
    target_id: str
    host: str
    user: str
    port: int
    identity_file: Path | None
    snapshot_directory: str
    snapshot_sha256: str
    programs: Mapping[str, str]
    timeout_seconds: float = 120.0
    max_output_bytes: int = 64_000


@dataclass(frozen=True, slots=True)
class RemoteWorkspaceWorkerVerification:
    """Result of a fixed-marker liveness check for a configured snapshot.

    This is an operator diagnostic, not a remote employee run.  It intentionally
    checks only the directory recorded in the verified transfer receipt and
    never accepts an arbitrary command, forwards environment values, or starts
    a Company Job.
    """

    target_id: str
    host: str
    port: int
    snapshot_sha256: str
    reachable: bool
    snapshot_present: bool
    authority: str = "operator_confirmed_fixed_marker_remote_snapshot_check_no_company_job_or_execution"

    def to_dict(self) -> dict[str, object]:
        return {
            "target_id": self.target_id,
            "host": self.host,
            "port": self.port,
            "snapshot_sha256": self.snapshot_sha256,
            "reachable": self.reachable,
            "snapshot_present": self.snapshot_present,
            "authority": self.authority,
            "host_key_policy": "STRICT_KNOWN_HOSTS_ONLY",
            "remote_job_execution": "NOT_STARTED",
        }


@dataclass(frozen=True, slots=True)
class RemoteWorkspaceContentVerification:
    """Fixed-ledger content audit of a receipt-bound remote snapshot."""

    target_id: str
    host: str
    port: int
    snapshot_sha256: str
    reachable: bool
    snapshot_present: bool
    content_verified: bool
    integrity_state: str
    authority: str = "operator_confirmed_fixed_remote_snapshot_ledger_audit_no_company_job_or_program_started"

    def to_dict(self) -> dict[str, object]:
        return {
            "target_id": self.target_id, "host": self.host, "port": self.port,
            "snapshot_sha256": self.snapshot_sha256, "reachable": self.reachable,
            "snapshot_present": self.snapshot_present, "content_verified": self.content_verified,
            "integrity_state": self.integrity_state, "authority": self.authority,
            "host_key_policy": "STRICT_KNOWN_HOSTS_ONLY", "remote_job_execution": "NOT_STARTED",
        }


_TARGET_ID = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_SSH_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")
_SSH_USER = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,31}$")


def remote_worker_config_from_settings(settings: Mapping[str, object]) -> RemoteWorkspaceWorkerConfig | None:
    """Load only an explicit operator target tied to a verified transfer receipt."""
    value = settings.get("remote_worker")
    if value is None:
        return None
    if not isinstance(value, Mapping) or value.get("enabled") is not True:
        return None
    target_id = value.get("target_id")
    receipt_path = value.get("receipt")
    programs = value.get("programs")
    if not isinstance(target_id, str) or not _TARGET_ID.fullmatch(target_id):
        raise ValueError("remote_worker.target_id must be a bounded identifier")
    if not isinstance(receipt_path, str) or not isinstance(programs, Mapping) or not programs:
        raise ValueError("remote_worker requires receipt and non-empty programs")
    receipt = Path(receipt_path).expanduser()
    if receipt.is_symlink() or not receipt.is_file():
        raise ValueError("remote_worker.receipt must be a regular receipt file")
    try:
        payload = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("remote_worker.receipt is invalid") from exc
    required = {"host", "user", "port", "remote_snapshot_directory", "snapshot_sha256", "transferred", "integrity_state", "host_key_policy", "remote_job_execution"}
    if not isinstance(payload, Mapping) or not required.issubset(payload) or payload.get("transferred") is not True or payload.get("integrity_state") != "VERIFIED_REMOTE_SNAPSHOT" or payload.get("host_key_policy") != "STRICT_KNOWN_HOSTS_ONLY" or payload.get("remote_job_execution") != "NOT_IMPLEMENTED":
        raise ValueError("remote_worker.receipt is not a verified strict-host snapshot transfer")
    host, user, port, stage, digest = (payload.get(key) for key in ("host", "user", "port", "remote_snapshot_directory", "snapshot_sha256"))
    stage_path = PurePosixPath(stage) if isinstance(stage, str) else None
    if (
        not isinstance(host, str)
        or not isinstance(user, str)
        or not isinstance(stage, str)
        or not isinstance(digest, str)
        or not isinstance(port, int)
        or not _SSH_HOST.fullmatch(host)
        or not _SSH_USER.fullmatch(user)
        or not 1 <= port <= 65_535
        or stage_path is None
        or not stage_path.is_absolute()
        or ".." in stage_path.parts
        or str(stage_path) != stage
        or not stage.endswith(f"/.noruct-remote-snapshots/{digest}")
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
    ):
        raise ValueError("remote_worker.receipt has invalid target facts")
    normalized_programs: dict[str, str] = {}
    for name, program in programs.items():
        if not isinstance(name, str) or not _TARGET_ID.fullmatch(name) or not isinstance(program, str):
            raise ValueError("remote_worker.programs must map bounded names to absolute programs")
        path = PurePosixPath(program)
        if not path.is_absolute() or str(path) != program or ".." in path.parts:
            raise ValueError("remote_worker programs must be normalized absolute POSIX paths")
        normalized_programs[name] = program
    identity = value.get("identity_file")
    key = None
    if identity is not None:
        if not isinstance(identity, str): raise ValueError("remote_worker.identity_file must be a path")
        key = Path(identity).expanduser()
        if key.is_symlink() or not key.is_file(): raise ValueError("remote_worker.identity_file must be a regular file")
        key = key.resolve()
    timeout = float(value.get("timeout_seconds", 120.0))
    if not 1 <= timeout <= 300: raise ValueError("remote_worker.timeout_seconds must be between 1 and 300")
    return RemoteWorkspaceWorkerConfig(target_id, host, user, port, key, stage, digest, normalized_programs, timeout)


def verify_remote_workspace_worker(
    config: RemoteWorkspaceWorkerConfig,
    *,
    runner: object = subprocess.run,
) -> RemoteWorkspaceWorkerVerification:
    """Confirm that the receipt-bound remote snapshot still exists.

    A successful marker proves only SSH reachability and the existence of the
    immutable staging directory.  It does not rehash remote content or imply
    that any allowlisted program is safe or ready to run.
    """

    ssh = shutil.which("ssh")
    if not ssh:
        raise ValueError("SSH executable was not found")
    marker = f"noruct-remote-worker-v1:present:{config.snapshot_sha256}"
    missing_marker = f"noruct-remote-worker-v1:missing:{config.snapshot_sha256}"
    remote = (
        "if test -d -- " + shlex.quote(config.snapshot_directory)
        + "; then printf %s " + shlex.quote(marker)
        + "; else printf %s " + shlex.quote(missing_marker)
        + "; fi"
    )
    command = [
        ssh,
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "ForwardAgent=no",
        "-o", "ClearAllForwardings=yes",
        "-o", "RequestTTY=no",
        f"ConnectTimeout={min(30, int(config.timeout_seconds))}",
        "-p", str(config.port),
    ]
    if config.identity_file is not None:
        command.extend(("-i", str(config.identity_file)))
    command.extend((f"{config.user}@{config.host}", remote))
    try:
        completed = runner(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=config.timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return RemoteWorkspaceWorkerVerification(config.target_id, config.host, config.port, config.snapshot_sha256, False, False)
    if completed.returncode != 0:
        return RemoteWorkspaceWorkerVerification(config.target_id, config.host, config.port, config.snapshot_sha256, False, False)
    output = completed.stdout if isinstance(completed.stdout, str) else ""
    return RemoteWorkspaceWorkerVerification(config.target_id, config.host, config.port, config.snapshot_sha256, True, output == marker)


def verify_remote_workspace_worker_content(
    config: RemoteWorkspaceWorkerConfig,
    *,
    runner: object = subprocess.run,
) -> RemoteWorkspaceContentVerification:
    """Re-run only the retained fixed SHA-256 ledger on a remote snapshot.

    This performs no generic shell action: the command has no operator/model
    arguments and is restricted to the snapshot path and its transfer ledger.
    It detects changes to transferred manifest entries, but intentionally does
    not claim that extra files were absent or that allowlisted programs are
    safe to execute.
    """

    ssh = shutil.which("ssh")
    if not ssh:
        raise ValueError("SSH executable was not found")
    prefix = f"noruct-remote-audit-v1:{config.snapshot_sha256}:"
    remote = (
        "if ! test -d -- " + shlex.quote(config.snapshot_directory) + "; then printf %s " + shlex.quote(prefix + "missing")
        + "; elif ! test -f -- " + shlex.quote(config.snapshot_directory + "/.noruct-transfer-sha256") + "; then printf %s " + shlex.quote(prefix + "ledger-missing")
        + "; else cd -- " + shlex.quote(config.snapshot_directory)
        + "; if command -v sha256sum >/dev/null 2>&1; then sha256sum -c .noruct-transfer-sha256 >/dev/null 2>&1"
        + "; elif command -v shasum >/dev/null 2>&1; then shasum -a 256 -c .noruct-transfer-sha256 >/dev/null 2>&1"
        + "; else printf %s " + shlex.quote(prefix + "hash-tool-missing") + "; exit 0; fi"
        + "; if [ $? -eq 0 ]; then printf %s " + shlex.quote(prefix + "verified") + "; else printf %s " + shlex.quote(prefix + "mismatch") + "; fi; fi"
    )
    command = [ssh, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ForwardAgent=no", "-o", "ClearAllForwardings=yes", "-o", "RequestTTY=no", f"ConnectTimeout={min(30, int(config.timeout_seconds))}", "-p", str(config.port)]
    if config.identity_file is not None:
        command.extend(("-i", str(config.identity_file)))
    command.extend((f"{config.user}@{config.host}", remote))
    try:
        completed = runner(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=config.timeout_seconds, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return RemoteWorkspaceContentVerification(config.target_id, config.host, config.port, config.snapshot_sha256, False, False, False, "SSH_UNAVAILABLE")
    if completed.returncode != 0:
        return RemoteWorkspaceContentVerification(config.target_id, config.host, config.port, config.snapshot_sha256, False, False, False, "SSH_UNAVAILABLE")
    output = completed.stdout if isinstance(completed.stdout, str) else ""
    state = output.removeprefix(prefix) if output.startswith(prefix) else "invalid-response"
    if state == "verified":
        return RemoteWorkspaceContentVerification(config.target_id, config.host, config.port, config.snapshot_sha256, True, True, True, "VERIFIED_REMOTE_LEDGER")
    if state == "missing":
        return RemoteWorkspaceContentVerification(config.target_id, config.host, config.port, config.snapshot_sha256, True, False, False, "SNAPSHOT_MISSING")
    if state == "ledger-missing":
        return RemoteWorkspaceContentVerification(config.target_id, config.host, config.port, config.snapshot_sha256, True, True, False, "LEDGER_UNAVAILABLE")
    if state == "hash-tool-missing":
        return RemoteWorkspaceContentVerification(config.target_id, config.host, config.port, config.snapshot_sha256, True, True, False, "HASH_TOOL_UNAVAILABLE")
    return RemoteWorkspaceContentVerification(config.target_id, config.host, config.port, config.snapshot_sha256, True, True, False, "REMOTE_LEDGER_MISMATCH")


class RemoteWorkspaceTools:
    """One operator-configured SSH target; never a generic remote shell."""
    def __init__(self, config: RemoteWorkspaceWorkerConfig) -> None:
        self.config = config

    def definition(self) -> ToolDefinition:
        def validate(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
            if set(arguments) != {"program_id", "arguments"}:
                raise ToolValidationError("run_remote_workspace_program requires program_id and arguments")
            program_id = arguments.get("program_id")
            values = arguments.get("arguments")
            if not isinstance(program_id, str) or program_id not in self.config.programs:
                raise ToolValidationError("Remote program is not in the operator allowlist")
            if not isinstance(values, list) or len(values) > 16 or not all(isinstance(item, str) for item in values):
                raise ToolValidationError("Remote program accepts at most 16 string arguments")
            if any("\x00" in item or "\n" in item or "\r" in item or len(item.encode()) > 1024 for item in values):
                raise ToolValidationError("Remote program argument is invalid")
            return {"program_id": program_id, "arguments": tuple(values)}

        async def handle(arguments: Mapping[str, object], cancellation: CancellationToken) -> str:
            return await self._execute(str(arguments["program_id"]), tuple(arguments["arguments"]), cancellation)

        return ToolDefinition(
            name="run_remote_workspace_program",
            description="Run one operator-allowlisted program in the verified remote workspace snapshot.",
            input_schema={"type":"object","properties":{"program_id":{"type":"string"},"arguments":{"type":"array","items":{"type":"string"},"maxItems":16}},"required":["program_id","arguments"],"additionalProperties":False},
            effect=ToolEffect.EXECUTE, risk=ToolRisk.HIGH, idempotency_mode=IdempotencyMode.CALL_KEY,
            validator=validate,
            resource_key=lambda value: f"remote-workspace:{self.config.target_id}:{value['program_id']}:{self.config.snapshot_sha256}",
            handler=handle, timeout_ms=int(self.config.timeout_seconds * 1000), requires_approval=True,
            approval_preview=lambda value: f"Remote {self.config.target_id}/{value['program_id']} in verified snapshot {self.config.snapshot_sha256[:12]}",
        )

    async def _execute(self, program_id: str, arguments: tuple[str, ...], cancellation: CancellationToken) -> str:
        cfg = self.config
        program = cfg.programs[program_id]
        remote = "cd -- " + shlex.quote(cfg.snapshot_directory) + " && exec env -i PATH=/usr/bin:/bin " + " ".join(shlex.quote(item) for item in (program, *arguments))
        command = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ForwardAgent=no", "-o", "ClearAllForwardings=yes", "-o", "RequestTTY=no", "-o", f"ConnectTimeout={min(30, int(cfg.timeout_seconds))}", "-p", str(cfg.port)]
        if cfg.identity_file is not None: command.extend(("-i", str(cfg.identity_file)))
        command.extend((f"{cfg.user}@{cfg.host}", remote))
        process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=(os.name == "posix"))
        waiter, cancelled = asyncio.create_task(process.communicate()), asyncio.create_task(cancellation.wait())
        try:
            done, _ = await asyncio.wait({waiter, cancelled}, timeout=cfg.timeout_seconds, return_when=asyncio.FIRST_COMPLETED)
            if cancelled in done:
                await self._terminate(process); raise OperationCancelled(cancellation.reason or "Remote worker cancelled")
            if waiter not in done:
                await self._terminate(process); raise TimeoutError
            output, _ = waiter.result()
        finally:
            cancelled.cancel()
            if process.returncode is None: await self._terminate(process)
        text = output.decode("utf-8", errors="replace")[:cfg.max_output_bytes]
        if process.returncode != 0:
            raise ToolExecutionError(f"Remote program exited with status {process.returncode}")
        return json.dumps({"target_id":cfg.target_id,"program_id":program_id,"snapshot_sha256":cfg.snapshot_sha256,"exit_code":process.returncode,"output":text}, ensure_ascii=False, sort_keys=True)

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None: return
        try:
            if os.name == "posix" and process.pid: os.killpg(process.pid, signal.SIGTERM)
            else: process.terminate()
            await asyncio.wait_for(process.wait(), timeout=1)
        except (ProcessLookupError, TimeoutError):
            try: process.kill()
            except ProcessLookupError: return
            await process.wait()
