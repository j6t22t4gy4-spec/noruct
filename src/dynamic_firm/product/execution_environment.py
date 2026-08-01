"""Local execution-environment diagnostics and explicit SSH preparation.

This is deliberately not a second agent runtime or a remote execution
transport.  The Company runtime already owns workspace commands, approvals,
and shadow workspaces.  This module makes that environment observable and
lets an operator perform one bounded SSH connectivity probe before a future
remote-worker adapter is considered.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import tarfile
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable, Mapping

from dynamic_firm._vendor.runtime_safety.redact import redact_terminal_output


EXECUTION_ENVIRONMENT_SCHEMA = "noruct.execution-environment.v1"
_SSH_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")
_SSH_USER = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,31}$")
_SNAPSHOT_EXCLUDED_DIRECTORIES = frozenset(
    {".codex", ".git", ".hg", ".noruct", ".ssh", ".svn", ".venv", "__pycache__", "node_modules"}
)
_SNAPSHOT_EXCLUDED_FILES = frozenset({".env", "credentials", "credentials.json", "id_ed25519", "id_rsa"})
_SNAPSHOT_MAX_FILES = 1_000
_SNAPSHOT_MAX_FILE_BYTES = 2_000_000
_SNAPSHOT_MAX_TOTAL_BYTES = 64_000_000


@dataclass(frozen=True, slots=True)
class ExecutionEnvironmentStatus:
    schema: str
    python_executable: str
    python_version: str
    platform: str
    machine: str
    workspace: Mapping[str, Any]
    executables: Mapping[str, bool]
    local_execution: str
    shadow_workspace: str
    remote_ssh: str
    remote_job_execution: str
    os_sandbox: str

    def to_dict(self) -> Mapping[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SshProbeResult:
    schema: str
    host: str
    user: str
    port: int
    identity_file: str | None
    reachable: bool
    authentication: str
    host_key_policy: str
    remote_command: str
    remote_job_execution: str
    output: str

    def to_dict(self) -> Mapping[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SshOperatorCommandResult:
    """Receipt for one operator-confirmed non-Company remote command."""

    schema: str
    host: str
    user: str
    port: int
    remote_workspace: str
    program: str
    argument_count: int
    completed: bool
    host_key_policy: str
    authority: str
    remote_job_execution: str
    output: str

    def to_dict(self) -> Mapping[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshotReceipt:
    """Evidence for an explicit, local-only future remote-workspace handoff."""

    schema: str
    workspace: str
    output_path: str
    file_count: int
    total_bytes: int
    snapshot_sha256: str
    exclusion_policy: str
    remote_job_execution: str

    def to_dict(self) -> Mapping[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshotInspection:
    """Read-only integrity result for a future-handoff manifest."""

    schema: str
    manifest_path: str
    valid: bool
    integrity_state: str
    file_count: int | None
    total_bytes: int | None
    snapshot_sha256: str | None
    remote_job_execution: str

    def to_dict(self) -> Mapping[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RemoteWorkspaceTransferReceipt:
    """Receipt for one explicitly confirmed, manifest-bound SSH transfer.

    The receipt is intentionally an operator artifact rather than a Company
    Job result.  It proves that one bounded snapshot was checked locally,
    transferred to a deterministic staging directory, and re-hashed by the
    remote host.  It does not grant an Employee any remote capability.
    """

    schema: str
    host: str
    user: str
    port: int
    remote_workspace: str
    remote_snapshot_directory: str
    snapshot_sha256: str
    file_count: int
    total_bytes: int
    transferred: bool
    integrity_state: str
    host_key_policy: str
    authority: str
    remote_job_execution: str
    output: str

    def to_dict(self) -> Mapping[str, Any]:
        return asdict(self)


def execution_environment_status(workspace: Path | None = None) -> ExecutionEnvironmentStatus:
    """Return local readiness facts without writing or contacting a service."""

    root = (workspace or Path.cwd()).expanduser().resolve()
    workspace_record = {
        "path": str(root),
        "exists": root.exists(),
        "is_directory": root.is_dir(),
        "writable_by_current_user": os.access(root, os.W_OK) if root.is_dir() else False,
    }
    return ExecutionEnvironmentStatus(
        schema=EXECUTION_ENVIRONMENT_SCHEMA,
        python_executable=str(Path(sys.executable).resolve()),
        python_version=platform.python_version(),
        platform=platform.system().lower(),
        machine=platform.machine().lower(),
        workspace=workspace_record,
        executables={name: shutil.which(name) is not None for name in ("git", "ssh", "docker", "podman")},
        local_execution="AVAILABLE_PER_ACTION_APPROVAL",
        shadow_workspace="AVAILABLE_FOR_USER_MANAGED_CODEX_ASK_MODE",
        remote_ssh="AVAILABLE_FOR_EXPLICIT_STRICT_HOST_PROBE_AND_TRANSFER",
        remote_job_execution="DISABLED_UNTIL_EXPLICIT_OPERATOR_CONFIGURATION",
        os_sandbox="NOT_CLAIMED",
    )


def write_workspace_snapshot_manifest(*, workspace: Path, output_path: Path) -> WorkspaceSnapshotReceipt:
    """Write one explicit content-hash manifest; never transfers it or starts a Job.

    This reuses Noruct's existing shadow-workspace exclusion and size envelope,
    but deliberately does *not* copy customer files.  The only output is a
    user-selected local JSON manifest.  A later remote adapter must still
    obtain an approval, establish remote trust, and implement a separate
    verified transfer/cancellation/audit contract before it can consume it.
    """

    declared_workspace = workspace.expanduser()
    if declared_workspace.is_symlink():
        raise ValueError("Workspace snapshot source must be a regular directory")
    root = declared_workspace.resolve()
    if not root.is_dir():
        raise ValueError("Workspace snapshot source must be a regular directory")
    declared_output = output_path.expanduser()
    if declared_output.is_symlink():
        raise ValueError("Workspace snapshot output must not be a symbolic link")
    output = declared_output.resolve()
    if not output.parent.is_dir():
        raise ValueError("Workspace snapshot output parent must already exist")
    if output.exists() and not output.is_file():
        raise ValueError("Workspace snapshot output must be a regular file path")
    try:
        output.relative_to(root)
    except ValueError:
        pass
    else:
        raise ValueError("Workspace snapshot output must be outside the snapshotted workspace")

    entries: list[dict[str, object]] = []
    total_bytes = 0
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        allowed_directories: list[str] = []
        for name in sorted(directories):
            child = current_path / name
            if name.lower() in _SNAPSHOT_EXCLUDED_DIRECTORIES:
                continue
            if child.is_symlink():
                raise ValueError("Workspace snapshot does not support symbolic-link directories")
            allowed_directories.append(name)
        directories[:] = allowed_directories
        for name in sorted(files):
            source = current_path / name
            relative = PurePosixPath(source.relative_to(root).as_posix())
            if _snapshot_excluded(relative):
                continue
            if source.is_symlink():
                raise ValueError("Workspace snapshot does not support symbolic-link files")
            if not source.is_file():
                raise ValueError("Workspace snapshot supports regular files only")
            content = source.read_bytes()
            size = len(content)
            if size > _SNAPSHOT_MAX_FILE_BYTES:
                raise ValueError("Workspace snapshot file exceeds the per-file byte limit")
            if len(entries) >= _SNAPSHOT_MAX_FILES:
                raise ValueError("Workspace snapshot exceeds the file-count limit")
            total_bytes += size
            if total_bytes > _SNAPSHOT_MAX_TOTAL_BYTES:
                raise ValueError("Workspace snapshot exceeds the total byte limit")
            entries.append({"path": str(relative), "bytes": size, "sha256": _sha256(content)})

    canonical_entries = json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = _sha256(canonical_entries.encode("utf-8"))
    payload = {
        "schema": "noruct.remote-workspace-snapshot.v1",
        "workspace": str(root),
        "file_count": len(entries),
        "total_bytes": total_bytes,
        "snapshot_sha256": digest,
        "exclusion_policy": "noruct-shadow-compatible-v1",
        "entries": entries,
    }
    _atomic_json_write(output, payload)
    return WorkspaceSnapshotReceipt(
        schema="noruct.remote-workspace-snapshot.v1",
        workspace=str(root),
        output_path=str(output),
        file_count=len(entries),
        total_bytes=total_bytes,
        snapshot_sha256=digest,
        exclusion_policy="noruct-shadow-compatible-v1",
        remote_job_execution="NOT_IMPLEMENTED",
    )


def inspect_workspace_snapshot_manifest(source: Path) -> WorkspaceSnapshotInspection:
    """Validate a local manifest's own deterministic integrity, without re-reading a workspace.

    This intentionally verifies no remote endpoint and transfers no file.  A
    valid result means only that the manifest has not been structurally altered
    since its declared digest was made; it does not attest to current workspace
    contents or authorize a remote worker.
    """

    declared = source.expanduser()
    if declared.is_symlink():
        raise ValueError("Workspace snapshot manifest must not be a symbolic link")
    path = declared.resolve()
    if not path.is_file():
        raise ValueError("Workspace snapshot manifest must be a regular file")
    try:
        if path.stat().st_size > 1_000_000:
            raise ValueError("Workspace snapshot manifest exceeds the byte limit")
        value = json.loads(path.read_text(encoding="utf-8"))
        file_count, total_bytes, digest = _validate_snapshot_manifest(value)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return WorkspaceSnapshotInspection(
            schema="noruct.remote-workspace-snapshot.v1",
            manifest_path=str(path),
            valid=False,
            integrity_state="INVALID_MANIFEST",
            file_count=None,
            total_bytes=None,
            snapshot_sha256=None,
            remote_job_execution="NOT_IMPLEMENTED",
        )
    return WorkspaceSnapshotInspection(
        schema="noruct.remote-workspace-snapshot.v1",
        manifest_path=str(path),
        valid=True,
        integrity_state="VALID_LOCAL_MANIFEST",
        file_count=file_count,
        total_bytes=total_bytes,
        snapshot_sha256=digest,
        remote_job_execution="NOT_IMPLEMENTED",
    )


def _validate_snapshot_manifest(value: object) -> tuple[int, int, str]:
    if not isinstance(value, dict) or set(value) != {
        "schema", "workspace", "file_count", "total_bytes", "snapshot_sha256", "exclusion_policy", "entries"
    }:
        raise ValueError("invalid workspace snapshot schema")
    if value.get("schema") != "noruct.remote-workspace-snapshot.v1":
        raise ValueError("invalid workspace snapshot schema")
    workspace = value.get("workspace")
    if (
        not isinstance(workspace, str)
        or "\x00" in workspace
        or not (PurePosixPath(workspace).is_absolute() or PureWindowsPath(workspace).is_absolute())
    ):
        raise ValueError("invalid workspace snapshot workspace")
    if value.get("exclusion_policy") != "noruct-shadow-compatible-v1":
        raise ValueError("invalid workspace snapshot exclusion policy")
    entries = value.get("entries")
    if not isinstance(entries, list) or len(entries) > _SNAPSHOT_MAX_FILES:
        raise ValueError("invalid workspace snapshot entries")
    paths: list[str] = []
    total = 0
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "bytes", "sha256"}:
            raise ValueError("invalid workspace snapshot entry")
        relative = entry.get("path")
        byte_count = entry.get("bytes")
        digest = entry.get("sha256")
        path = PurePosixPath(relative) if isinstance(relative, str) else PurePosixPath("/")
        if (
            not isinstance(relative, str)
            or path.is_absolute()
            or not path.parts
            or ".." in path.parts
            or str(path) != relative
            or not isinstance(byte_count, int)
            or isinstance(byte_count, bool)
            or not 0 <= byte_count <= _SNAPSHOT_MAX_FILE_BYTES
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise ValueError("invalid workspace snapshot entry")
        paths.append(relative)
        total += byte_count
        if total > _SNAPSHOT_MAX_TOTAL_BYTES:
            raise ValueError("invalid workspace snapshot total")
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise ValueError("invalid workspace snapshot entry ordering")
    if value.get("file_count") != len(entries) or value.get("total_bytes") != total:
        raise ValueError("invalid workspace snapshot counts")
    canonical_entries = json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    expected = _sha256(canonical_entries.encode("utf-8"))
    if value.get("snapshot_sha256") != expected:
        raise ValueError("invalid workspace snapshot digest")
    return len(entries), total, expected


def _load_snapshot_manifest(source: Path) -> tuple[Path, dict[str, object], int, int, str]:
    """Load and validate one bounded manifest before reading its workspace."""

    declared = source.expanduser()
    if declared.is_symlink():
        raise ValueError("Workspace snapshot manifest must not be a symbolic link")
    path = declared.resolve()
    if not path.is_file():
        raise ValueError("Workspace snapshot manifest must be a regular file")
    if path.stat().st_size > 1_000_000:
        raise ValueError("Workspace snapshot manifest exceeds the byte limit")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Workspace snapshot manifest is invalid") from exc
    file_count, total_bytes, digest = _validate_snapshot_manifest(value)
    if not isinstance(value, dict):  # Kept for type narrowing after validation.
        raise ValueError("Workspace snapshot manifest is invalid")
    return path, value, file_count, total_bytes, digest


def _snapshot_entries_for_transfer(
    *, workspace: Path, manifest: Mapping[str, object]
) -> tuple[Path, tuple[tuple[Path, PurePosixPath, str], ...]]:
    """Re-read precisely the manifest entries and reject any workspace drift."""

    declared_workspace = workspace.expanduser()
    if declared_workspace.is_symlink():
        raise ValueError("Workspace transfer source must be a regular directory")
    root = declared_workspace.resolve()
    if not root.is_dir():
        raise ValueError("Workspace transfer source must be a regular directory")
    recorded_workspace = manifest.get("workspace")
    if not isinstance(recorded_workspace, str) or str(root) != recorded_workspace:
        raise ValueError("Workspace transfer source does not match the snapshot manifest")
    raw_entries = manifest.get("entries")
    if not isinstance(raw_entries, list):
        raise ValueError("Workspace snapshot manifest is invalid")
    entries: list[tuple[Path, PurePosixPath, str]] = []
    for entry in raw_entries:
        if not isinstance(entry, Mapping):
            raise ValueError("Workspace snapshot manifest is invalid")
        raw_path = entry.get("path")
        expected_bytes = entry.get("bytes")
        expected_digest = entry.get("sha256")
        if not isinstance(raw_path, str) or not isinstance(expected_bytes, int) or not isinstance(expected_digest, str):
            raise ValueError("Workspace snapshot manifest is invalid")
        relative = PurePosixPath(raw_path)
        candidate = root.joinpath(*relative.parts)
        # Do not resolve a candidate before rejecting an operator-controlled
        # symlink.  This keeps the manifest's relative namespace authoritative.
        if candidate.is_symlink() or not candidate.is_file():
            raise ValueError("Workspace transfer source changed since the snapshot manifest")
        resolved = candidate.resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError("Workspace transfer source escapes its workspace") from exc
        content = resolved.read_bytes()
        if len(content) != expected_bytes or _sha256(content) != expected_digest:
            raise ValueError("Workspace transfer source changed since the snapshot manifest")
        entries.append((resolved, relative, expected_digest))
    return root, tuple(entries)


def _validated_remote_workspace(value: str) -> str:
    workspace = value.strip()
    path = PurePosixPath(workspace)
    if (
        not path.is_absolute()
        or str(path) != workspace
        or workspace == "/"
        or ".." in path.parts
        or "\x00" in workspace
        or "\n" in workspace
        or "\r" in workspace
    ):
        raise ValueError("Remote workspace must be an absolute normalized non-root POSIX path")
    return workspace


def _create_verified_transfer_archive(
    *, entries: tuple[tuple[Path, PurePosixPath, str], ...], archive_path: Path
) -> None:
    """Create a finite gzip archive with a remote-verifiable SHA-256 ledger."""

    lines = "".join(f"{digest} *{relative}\n" for _, relative, digest in entries)
    with tarfile.open(archive_path, mode="w:gz", format=tarfile.PAX_FORMAT) as archive:
        for source, relative, _ in entries:
            archive.add(source, arcname=str(relative), recursive=False)
        ledger = lines.encode("utf-8")
        member = tarfile.TarInfo(name=".noruct-transfer-sha256")
        member.size = len(ledger)
        member.mode = 0o600
        archive.addfile(member, io.BytesIO(ledger))


def _ssh_base_command(
    *, ssh: str, host: str, user: str, port: int, identity_file: Path | None, timeout_seconds: float
) -> list[str]:
    command = [
        ssh,
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "ForwardAgent=no",
        "-o", "ClearAllForwardings=yes",
        "-o", "RequestTTY=no",
        "-o", f"ConnectTimeout={min(30, max(1, int(timeout_seconds)))}",
        "-p", str(port),
    ]
    if identity_file is not None:
        command.extend(("-i", str(identity_file)))
    command.append(f"{user}@{host}")
    return command


def transfer_workspace_snapshot(
    *,
    workspace: Path,
    snapshot_manifest: Path,
    host: str,
    user: str,
    remote_workspace: str,
    port: int = 22,
    identity_file: Path | None = None,
    timeout_seconds: float = 120.0,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> RemoteWorkspaceTransferReceipt:
    """Upload one immutable snapshot to an isolated remote staging directory.

    The function is intentionally not registered as an Employee tool.  It is
    reached only from the explicit operator CLI and requires that caller to
    obtain confirmation.  The transfer adapts the registered upstream's
    tar-over-SSH batching invariant while rejecting its credential sync,
    reverse-sync and automatic host-trust behavior.
    """

    normalized_host, normalized_user, normalized_port, key = _validate_ssh_target(
        host=host, user=user, port=port, identity_file=identity_file
    )
    normalized_workspace = _validated_remote_workspace(remote_workspace)
    if not 1 <= timeout_seconds <= 300:
        raise ValueError("Remote workspace transfer timeout must be between 1 and 300 seconds")
    _, manifest, file_count, total_bytes, digest = _load_snapshot_manifest(snapshot_manifest)
    _, entries = _snapshot_entries_for_transfer(workspace=workspace, manifest=manifest)
    ssh = shutil.which("ssh")
    if ssh is None:
        raise ValueError("OpenSSH client was not found on PATH")

    stage = f"{normalized_workspace}/.noruct-remote-snapshots/{digest}"
    # The input archive is finite (the manifest caps it at 64 MB), and the
    # remote command is constant apart from locally validated quoted atoms.
    remote_command = "\n".join(
        (
            "set -eu",
            f"stage={shlex.quote(stage)}",
            'if [ -e "$stage" ]; then printf %s noruct-transfer-stage-exists; exit 73; fi',
            'mkdir -p -- "$stage"',
            'tar -xzf - -C "$stage"',
            'cd -- "$stage"',
            'if command -v sha256sum >/dev/null 2>&1; then sha256sum -c .noruct-transfer-sha256; '
            'elif command -v shasum >/dev/null 2>&1; then shasum -a 256 -c .noruct-transfer-sha256; '
            'else printf %s noruct-transfer-hash-tool-missing; exit 72; fi',
            # Keep the verified immutable ledger inside the private snapshot
            # staging directory. A later operator audit can re-check it before
            # remote Company execution; it is not a workspace source file.
            f"printf %s {shlex.quote('noruct-transfer-ok:' + digest)}",
        )
    )
    command = _ssh_base_command(
        ssh=ssh,
        host=normalized_host,
        user=normalized_user,
        port=normalized_port,
        identity_file=key,
        timeout_seconds=timeout_seconds,
    )
    # OpenSSH joins all remote argv members into one command string.  Preserve
    # the multiline script as one shell atom instead of relying on that join to
    # retain its original argument boundary.
    command.append("sh -ceu " + shlex.quote(remote_command))
    with tempfile.TemporaryDirectory(prefix="noruct-remote-transfer-") as temporary:
        archive_path = Path(temporary) / "workspace.tar.gz"
        _create_verified_transfer_archive(entries=entries, archive_path=archive_path)
        archive_bytes = archive_path.read_bytes()
        try:
            completed = runner(
                command,
                input=archive_bytes,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return RemoteWorkspaceTransferReceipt(
                schema="noruct.remote-workspace-transfer.v1",
                host=normalized_host,
                user=normalized_user,
                port=normalized_port,
                remote_workspace=normalized_workspace,
                remote_snapshot_directory=stage,
                snapshot_sha256=digest,
                file_count=file_count,
                total_bytes=total_bytes,
                transferred=False,
                integrity_state="TRANSFER_TIMEOUT",
                host_key_policy="STRICT_KNOWN_HOSTS_ONLY",
                authority="operator_confirmed_manifest_transfer_only_no_company_job",
                remote_job_execution="NOT_IMPLEMENTED",
                output="Remote workspace transfer timed out",
            )
    stdout = completed.stdout.decode("utf-8", errors="replace") if isinstance(completed.stdout, bytes) else (completed.stdout or "")
    stderr = completed.stderr.decode("utf-8", errors="replace") if isinstance(completed.stderr, bytes) else (completed.stderr or "")
    output = redact_terminal_output(
        stdout + stderr,
        command="ssh verified workspace transfer",
        force=True,
    ).strip()
    expected_marker = f"noruct-transfer-ok:{digest}"
    success = completed.returncode == 0 and output.endswith(expected_marker)
    return RemoteWorkspaceTransferReceipt(
        schema="noruct.remote-workspace-transfer.v1",
        host=normalized_host,
        user=normalized_user,
        port=normalized_port,
        remote_workspace=normalized_workspace,
        remote_snapshot_directory=stage,
        snapshot_sha256=digest,
        file_count=file_count,
        total_bytes=total_bytes,
        transferred=success,
        integrity_state="VERIFIED_REMOTE_SNAPSHOT" if success else "TRANSFER_OR_REMOTE_VERIFICATION_FAILED",
        host_key_policy="STRICT_KNOWN_HOSTS_ONLY",
        authority="operator_confirmed_manifest_transfer_only_no_company_job",
        remote_job_execution="NOT_IMPLEMENTED",
        output=output[:2_000] if output else ("Remote workspace transfer failed" if not success else output),
    )


def _snapshot_excluded(relative: PurePosixPath) -> bool:
    lowered = tuple(part.lower() for part in relative.parts)
    if any(part in _SNAPSHOT_EXCLUDED_DIRECTORIES for part in lowered[:-1]):
        return True
    return relative.name.lower() in _SNAPSHOT_EXCLUDED_FILES or relative.name.lower().startswith(".env.")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _atomic_json_write(path: Path, payload: Mapping[str, object]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".noruct-snapshot-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
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


def _validate_ssh_target(*, host: str, user: str, port: int, identity_file: Path | None) -> tuple[str, str, int, Path | None]:
    normalized_host = host.strip()
    normalized_user = user.strip()
    if not _SSH_HOST.fullmatch(normalized_host) or ".." in normalized_host:
        raise ValueError("SSH host must be a hostname or IPv4 address without shell characters")
    if not _SSH_USER.fullmatch(normalized_user):
        raise ValueError("SSH user must be a bounded account name")
    if not 1 <= port <= 65_535:
        raise ValueError("SSH port must be between 1 and 65535")
    if identity_file is None:
        return normalized_host, normalized_user, port, None
    declared_key = identity_file.expanduser()
    # As with channel commands, a resolved symlink cannot be distinguished
    # from a directly selected regular file.  Reject the operator's declared
    # symlink before canonicalising the safe path for subprocess argv.
    if declared_key.is_symlink():
        raise ValueError("SSH identity file must be a regular non-symbolic-link file")
    key = declared_key.resolve()
    if not key.is_file():
        raise ValueError("SSH identity file must be a regular non-symbolic-link file")
    return normalized_host, normalized_user, port, key


def probe_ssh_environment(
    *,
    host: str,
    user: str,
    port: int = 22,
    identity_file: Path | None = None,
    timeout_seconds: float = 10.0,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> SshProbeResult:
    """Run a user-confirmed, non-interactive SSH reachability probe.

    The command uses strict host-key checking, disables forwarding and TTY
    allocation, and runs only a fixed marker command.  It never copies files,
    exposes environment variables, starts an employee worker, or records a
    credential.  A successful probe is explicitly *not* remote-job approval.
    """

    normalized_host, normalized_user, normalized_port, key = _validate_ssh_target(
        host=host, user=user, port=port, identity_file=identity_file
    )
    if not 1 <= timeout_seconds <= 60:
        raise ValueError("SSH probe timeout must be between 1 and 60 seconds")
    ssh = shutil.which("ssh")
    if ssh is None:
        raise ValueError("OpenSSH client was not found on PATH")
    command = [
        ssh,
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "ForwardAgent=no",
        "-o", "ClearAllForwardings=yes",
        "-o", "RequestTTY=no",
        "-o", f"ConnectTimeout={min(30, max(1, int(timeout_seconds)))}",
        "-p", str(normalized_port),
    ]
    if key is not None:
        command.extend(("-i", str(key)))
    command.extend((f"{normalized_user}@{normalized_host}", "printf", "%s", "noruct-remote-probe-v1"))
    try:
        completed = runner(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return SshProbeResult(
            schema=EXECUTION_ENVIRONMENT_SCHEMA,
            host=normalized_host,
            user=normalized_user,
            port=normalized_port,
            identity_file=str(key) if key else None,
            reachable=False,
            authentication="NOT_CONFIRMED",
            host_key_policy="STRICT_KNOWN_HOSTS_ONLY",
            remote_command="fixed_marker_only",
            remote_job_execution="NOT_IMPLEMENTED",
            output="SSH probe timed out",
        )
    output = redact_terminal_output(
        (completed.stdout or "") + (completed.stderr or ""),
        command="ssh remote probe",
        force=True,
    ).strip()
    success = completed.returncode == 0 and output == "noruct-remote-probe-v1"
    return SshProbeResult(
        schema=EXECUTION_ENVIRONMENT_SCHEMA,
        host=normalized_host,
        user=normalized_user,
        port=normalized_port,
        identity_file=str(key) if key else None,
        reachable=success,
        authentication="CONFIRMED_BY_FIXED_MARKER" if success else "NOT_CONFIRMED",
        host_key_policy="STRICT_KNOWN_HOSTS_ONLY",
        remote_command="fixed_marker_only",
        remote_job_execution="NOT_IMPLEMENTED",
        output=output[:1_000] if output else ("SSH probe failed" if not success else output),
    )


def _validate_remote_command(
    *, remote_workspace: str, program: str, arguments: tuple[str, ...]
) -> tuple[str, str, tuple[str, ...]]:
    workspace = remote_workspace.strip()
    workspace_path = PurePosixPath(workspace)
    if not workspace_path.is_absolute() or ".." in workspace_path.parts or "\x00" in workspace or "\n" in workspace or "\r" in workspace:
        raise ValueError("Remote workspace must be an absolute normalized POSIX path")
    executable = program.strip()
    executable_path = PurePosixPath(executable)
    if not executable_path.is_absolute() or ".." in executable_path.parts or "\x00" in executable or "\n" in executable or "\r" in executable:
        raise ValueError("Remote program must be an absolute normalized POSIX path")
    if len(arguments) > 16:
        raise ValueError("Remote operator command accepts at most 16 arguments")
    normalized = tuple(arguments)
    for argument in normalized:
        if not isinstance(argument, str) or "\x00" in argument or "\n" in argument or "\r" in argument or len(argument.encode("utf-8")) > 1_024:
            raise ValueError("Remote operator command argument is invalid")
    return str(workspace_path), str(executable_path), normalized


def run_ssh_operator_command(
    *,
    host: str,
    user: str,
    remote_workspace: str,
    program: str,
    arguments: tuple[str, ...] = (),
    port: int = 22,
    identity_file: Path | None = None,
    timeout_seconds: float = 60.0,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> SshOperatorCommandResult:
    """Run one explicitly confirmed operator command without creating a Job.

    The Dynamic Firm compiler and Employee tools cannot call this function.
    It is intentionally a separate operator surface for a pre-existing remote
    workspace.  No file sync, source upload, environment forwarding, provider
    credential forwarding, remote Noruct installation, Company state access,
    or automatic retry is performed.
    """

    normalized_host, normalized_user, normalized_port, key = _validate_ssh_target(
        host=host, user=user, port=port, identity_file=identity_file
    )
    workspace, executable, normalized_arguments = _validate_remote_command(
        remote_workspace=remote_workspace, program=program, arguments=arguments
    )
    if not 1 <= timeout_seconds <= 300:
        raise ValueError("Remote operator command timeout must be between 1 and 300 seconds")
    ssh = shutil.which("ssh")
    if ssh is None:
        raise ValueError("OpenSSH client was not found on PATH")
    # SSH ultimately submits one remote command string. Quote every atom
    # ourselves and reject line/control characters above, instead of passing a
    # user-provided shell expression through the transport.
    remote_command = "cd -- " + shlex.quote(workspace) + " && exec env -i PATH=/usr/bin:/bin " + " ".join(
        shlex.quote(value) for value in (executable, *normalized_arguments)
    )
    command = [
        ssh,
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "ForwardAgent=no",
        "-o", "ClearAllForwardings=yes",
        "-o", "RequestTTY=no",
        "-o", f"ConnectTimeout={min(30, max(1, int(timeout_seconds)))}",
        "-p", str(normalized_port),
    ]
    if key is not None:
        command.extend(("-i", str(key)))
    command.extend((f"{normalized_user}@{normalized_host}", remote_command))
    try:
        completed_process = runner(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            stdin=subprocess.DEVNULL,
            check=False,
        )
        output = redact_terminal_output(
            (completed_process.stdout or "") + (completed_process.stderr or ""),
            command="ssh operator command",
            force=True,
        ).strip()
        completed = completed_process.returncode == 0
    except subprocess.TimeoutExpired:
        completed = False
        output = "Remote operator command timed out"
    return SshOperatorCommandResult(
        schema=EXECUTION_ENVIRONMENT_SCHEMA,
        host=normalized_host,
        user=normalized_user,
        port=normalized_port,
        remote_workspace=workspace,
        program=executable,
        argument_count=len(normalized_arguments),
        completed=completed,
        host_key_policy="STRICT_KNOWN_HOSTS_ONLY",
        authority="operator_confirmed_one_shot_no_company_job_no_file_sync_no_credential_forwarding",
        remote_job_execution="NOT_IMPLEMENTED",
        output=output[:4_000] if output else ("Remote operator command failed" if not completed else ""),
    )
