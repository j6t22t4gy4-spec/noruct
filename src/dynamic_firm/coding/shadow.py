from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Mapping

from dynamic_firm._vendor.mini_swe_runtime import run_bounded_step_loop
from dynamic_firm.runtime.models import (
    IdempotencyMode,
    ToolEffect,
    ToolRisk,
    Usage,
)
from dynamic_firm.runtime.ports import CancellationToken
from dynamic_firm.runtime.redaction import redact_prompt_text
from dynamic_firm.runtime.tools import (
    ToolDefinition,
    ToolValidationError,
    atomic_write_text,
    checked_workspace_mutation_target,
    validate_workspace_mutation_path,
)

from .constants import APPLY_CHANGE_SET_TOOL
from .models import (
    CodingExecutionProgress,
    CodingExecutionProgressKind,
    CodingWorkRequest,
    CodingWorkResult,
    FileChangeKind,
    ShadowCodingOutcome,
    ValidationAttempt,
    WorkspaceChangeSet,
    WorkspaceFileChange,
)
from .ports import CodingValidatorPort, CodingWorkerPort

_EXCLUDED_DIRECTORY_NAMES = {
    ".cache",
    ".codex",
    ".git",
    ".hg",
    ".mypy_cache",
    ".noruct",
    ".pytest_cache",
    ".ruff_cache",
    ".ssh",
    ".svn",
    ".venv",
    "__pycache__",
    "node_modules",
}
_EXCLUDED_FILE_NAMES = {
    ".env",
    "credentials",
    "credentials.json",
    "id_ed25519",
    "id_rsa",
}
_SAFE_VALIDATION_NAME = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_SAFE_VALIDATION_DETAIL = re.compile(r"[A-Za-z0-9_.:,;=+() -]{0,512}")


class ShadowWorkspaceError(Exception):
    def __init__(self, code: str, message_safe: str, *, retryable: bool = False) -> None:
        super().__init__(message_safe)
        self.code = code
        self.message_safe = message_safe
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class ShadowWorkspaceLimits:
    max_files: int = 1_000
    max_file_bytes: int = 2_000_000
    max_total_bytes: int = 64_000_000
    max_changed_files: int = 32
    max_changed_file_bytes: int = 512_000
    max_change_set_bytes: int = 2_000_000
    max_preview_bytes: int = 16_000


@dataclass(frozen=True, slots=True)
class _SnapshotEntry:
    sha256: str
    content: bytes


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _excluded(relative: PurePosixPath) -> bool:
    lowered = tuple(part.lower() for part in relative.parts)
    if any(part in _EXCLUDED_DIRECTORY_NAMES for part in lowered[:-1]):
        return True
    name = relative.name.lower()
    return name in _EXCLUDED_FILE_NAMES or name.startswith(".env.")


class ShadowWorkspaceService:
    """Create a bounded disposable snapshot and derive changes independently."""

    def __init__(
        self,
        limits: ShadowWorkspaceLimits | None = None,
        *,
        excluded_paths: tuple[str, ...] = (),
    ) -> None:
        self.limits = limits or ShadowWorkspaceLimits()
        normalized: set[str] = set()
        for value in excluded_paths:
            path = PurePosixPath(value)
            if path.is_absolute() or not path.parts or ".." in path.parts:
                raise ValueError("Shadow exclusions must be safe workspace-relative paths")
            normalized.add(str(path))
        self.excluded_paths = frozenset(normalized)

    async def execute(
        self,
        *,
        source_root: Path,
        workspace_id: str,
        request: CodingWorkRequest,
        worker: CodingWorkerPort,
        cancellation: CancellationToken,
        validator: CodingValidatorPort | None = None,
        validation_recovery: bool = False,
        max_worker_calls: int = 1,
        retry_admission_reason: Callable[[Usage], str | None] | None = None,
        progress: Callable[[CodingExecutionProgress], None] | None = None,
    ) -> ShadowCodingOutcome:
        if max_worker_calls not in {1, 2}:
            raise ValueError("Shadow coding supports one initial call and at most one recovery call")
        if validation_recovery and validator is None:
            raise ValueError("Validation recovery requires a first-party validator")
        cancellation.raise_if_cancelled()
        with tempfile.TemporaryDirectory(prefix="noruct-shadow-") as temporary:
            shadow_root = Path(temporary) / "workspace"
            manifest = await asyncio.to_thread(self._copy_snapshot, source_root, shadow_root)
            cancellation.raise_if_cancelled()
            shadow_request = replace(request, workspace=shadow_root, validation_feedback=())
            validations: list[ValidationAttempt] = []
            aggregate_usage = Usage()

            async def run_worker(call_index: int, call_request: CodingWorkRequest) -> CodingWorkResult:
                nonlocal aggregate_usage
                cancellation.raise_if_cancelled()
                if progress is not None:
                    progress(
                        CodingExecutionProgress(
                            CodingExecutionProgressKind.WORKER_STARTED,
                            call_index,
                        )
                    )
                result = await worker.execute(call_request, cancellation)
                cancellation.raise_if_cancelled()
                aggregate_usage = aggregate_usage.plus(
                    Usage(
                        model_calls=1,
                        input_tokens=result.usage.input_tokens,
                        cached_input_tokens=result.usage.cached_input_tokens,
                        output_tokens=result.usage.output_tokens,
                        cost_usd=result.usage.cost_usd,
                    )
                )
                if progress is not None:
                    progress(
                        CodingExecutionProgress(
                            CodingExecutionProgressKind.WORKER_COMPLETED,
                            call_index,
                            worker_result=result,
                        )
                    )
                return result

            async def run_validation(
                call_index: int,
                result: CodingWorkResult,
            ) -> tuple[ValidationAttempt, ...]:
                if validator is not None:
                    cancellation.raise_if_cancelled()
                    raw_attempts = (
                        await validator.validate(
                            replace(shadow_request, validation_feedback=()),
                            cancellation,
                        ),
                    )
                else:
                    raw_attempts = tuple(result.validation_attempts)
                normalized = tuple(
                    self._safe_validation_attempt(attempt) for attempt in raw_attempts
                )
                candidate_change_set = await asyncio.to_thread(
                    self._derive_change_set,
                    shadow_root,
                    workspace_id,
                    manifest,
                )
                candidate_changed_paths = (
                    tuple(change.path for change in candidate_change_set.files)
                    if candidate_change_set is not None
                    else ()
                )
                validations.extend(normalized)
                if progress is not None:
                    for attempt in normalized:
                        progress(
                            CodingExecutionProgress(
                                CodingExecutionProgressKind.VALIDATION_RECORDED,
                                call_index,
                                validation_attempt=attempt,
                                candidate_changed_paths=candidate_changed_paths,
                            )
                        )
                return normalized

            current_request = shadow_request

            async def step(
                call_index: int,
            ) -> CodingWorkResult:
                return await run_worker(call_index, current_request)

            async def observe(
                call_index: int,
                result: CodingWorkResult,
            ) -> tuple[ValidationAttempt, ...]:
                nonlocal current_request
                current_validations = await run_validation(call_index, result)
                if (
                    call_index == 1
                    and validator is not None
                    and current_validations
                    and not current_validations[-1].passed
                    and validation_recovery
                ):
                    current_request = replace(
                        shadow_request,
                        validation_feedback=(current_validations[-1],),
                    )
                return current_validations

            def should_continue(
                call_index: int,
                current_validations: tuple[ValidationAttempt, ...],
            ) -> bool:
                return bool(
                    call_index == 1
                    and validator is not None
                    and current_validations
                    and not current_validations[-1].passed
                    and validation_recovery
                )

            def admission_reason(call_index: int) -> str | None:
                if call_index == 1:
                    return None
                if call_index > max_worker_calls:
                    return "max_model_calls"
                if retry_admission_reason is not None:
                    return retry_admission_reason(aggregate_usage)
                return None

            loop = await run_bounded_step_loop(
                max_steps=2 if validation_recovery else 1,
                run_step=step,
                observe_step=observe,
                should_continue=should_continue,
                admission_reason=admission_reason,
            )
            final_result = loop.steps[-1]

            combined_result = replace(
                final_result,
                validation_attempts=tuple(validations),
                usage=aggregate_usage,
            )
            cancellation.raise_if_cancelled()
            change_set = await asyncio.to_thread(
                self._derive_change_set,
                shadow_root,
                workspace_id,
                manifest,
            )
            return ShadowCodingOutcome(
                combined_result,
                change_set,
                tuple(loop.steps),
                loop.admission_blocked_reason is not None,
                loop.admission_blocked_reason,
            )

    @staticmethod
    def _safe_validation_attempt(attempt: ValidationAttempt) -> ValidationAttempt:
        if (
            not isinstance(attempt, ValidationAttempt)
            or type(attempt.passed) is not bool
            or not isinstance(attempt.name, str)
            or not isinstance(attempt.detail, str)
        ):
            raise ShadowWorkspaceError(
                "CODING_VALIDATION_INVALID",
                "The coding validator returned an invalid validation record.",
            )
        name = attempt.name.strip()
        if _SAFE_VALIDATION_NAME.fullmatch(name) is None:
            raise ShadowWorkspaceError(
                "CODING_VALIDATION_INVALID",
                "The coding validator returned an invalid validation name.",
            )
        detail = " ".join(redact_prompt_text(attempt.detail).split())
        if _SAFE_VALIDATION_DETAIL.fullmatch(detail) is None:
            detail = "details-redacted"
        return ValidationAttempt(name, attempt.passed, detail)

    def _copy_snapshot(
        self,
        source_root: Path,
        shadow_root: Path,
    ) -> dict[str, _SnapshotEntry]:
        root = source_root.expanduser().resolve()
        if not root.is_dir():
            raise ShadowWorkspaceError("SHADOW_SOURCE_INVALID", "Workspace is not a directory.")
        shadow_root.mkdir(parents=True, exist_ok=False)
        manifest: dict[str, _SnapshotEntry] = {}
        total_bytes = 0
        for current, directories, files in os.walk(root, topdown=True, followlinks=False):
            current_path = Path(current)
            directories[:] = sorted(
                name
                for name in directories
                if name.lower() not in _EXCLUDED_DIRECTORY_NAMES
                and not (current_path / name).is_symlink()
            )
            for name in sorted(files):
                source = current_path / name
                if source.is_symlink() or not source.is_file():
                    continue
                relative = PurePosixPath(source.relative_to(root).as_posix())
                if _excluded(relative) or str(relative) in self.excluded_paths:
                    continue
                size = source.stat().st_size
                if size > self.limits.max_file_bytes:
                    raise ShadowWorkspaceError(
                        "SHADOW_FILE_LIMIT",
                        f"Workspace file exceeds shadow limit: {relative}",
                    )
                if len(manifest) >= self.limits.max_files:
                    raise ShadowWorkspaceError(
                        "SHADOW_FILE_COUNT_LIMIT",
                        "Workspace contains too many files for a shadow run.",
                    )
                total_bytes += size
                if total_bytes > self.limits.max_total_bytes:
                    raise ShadowWorkspaceError(
                        "SHADOW_TOTAL_BYTES_LIMIT",
                        "Workspace exceeds the shadow snapshot byte limit.",
                    )
                content = source.read_bytes()
                destination = shadow_root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination, follow_symlinks=False)
                manifest[str(relative)] = _SnapshotEntry(_sha256(content), content)
        return manifest

    def _scan_shadow(self, shadow_root: Path) -> dict[str, bytes]:
        values: dict[str, bytes] = {}
        total_bytes = 0
        for current, directories, files in os.walk(
            shadow_root,
            topdown=True,
            followlinks=False,
        ):
            current_path = Path(current)
            for name in tuple(directories):
                child = current_path / name
                if child.is_symlink():
                    raise ShadowWorkspaceError(
                        "SHADOW_SYMLINK_UNSUPPORTED",
                        "Shadow worker created a symbolic link.",
                    )
            directories[:] = sorted(
                name
                for name in directories
                if name.lower() not in _EXCLUDED_DIRECTORY_NAMES
            )
            for name in sorted(files):
                path = current_path / name
                if path.is_symlink():
                    raise ShadowWorkspaceError(
                        "SHADOW_SYMLINK_UNSUPPORTED",
                        "Shadow worker created a symbolic link.",
                    )
                if not path.is_file():
                    continue
                relative = PurePosixPath(path.relative_to(shadow_root).as_posix())
                if _excluded(relative) or str(relative) in self.excluded_paths:
                    continue
                size = path.stat().st_size
                if size > self.limits.max_file_bytes:
                    raise ShadowWorkspaceError(
                        "SHADOW_FILE_LIMIT",
                        f"Shadow file exceeds limit: {relative}",
                    )
                if len(values) >= self.limits.max_files:
                    raise ShadowWorkspaceError(
                        "SHADOW_FILE_COUNT_LIMIT",
                        "Shadow workspace contains too many files.",
                    )
                total_bytes += size
                if total_bytes > self.limits.max_total_bytes:
                    raise ShadowWorkspaceError(
                        "SHADOW_TOTAL_BYTES_LIMIT",
                        "Shadow workspace exceeds the total byte limit.",
                    )
                values[str(relative)] = path.read_bytes()
        return values

    def _derive_change_set(
        self,
        shadow_root: Path,
        workspace_id: str,
        manifest: Mapping[str, _SnapshotEntry],
    ) -> WorkspaceChangeSet | None:
        current = self._scan_shadow(shadow_root)
        deleted = sorted(set(manifest) - set(current))
        if deleted:
            raise ShadowWorkspaceError(
                "SHADOW_DELETE_UNSUPPORTED",
                f"Shadow worker deleted unsupported path: {deleted[0]}",
            )
        changed: list[WorkspaceFileChange] = []
        total_bytes = 0
        for path in sorted(current):
            content = current[path]
            before = manifest.get(path)
            digest = _sha256(content)
            if before is not None and before.sha256 == digest:
                continue
            try:
                validate_workspace_mutation_path(path)
                new_text = content.decode("utf-8")
                if b"\x00" in content:
                    raise ValueError("NUL byte")
                old_text = before.content.decode("utf-8") if before is not None else None
            except (ToolValidationError, UnicodeDecodeError, ValueError):
                raise ShadowWorkspaceError(
                    "SHADOW_CHANGE_UNSUPPORTED",
                    f"Shadow change is binary or protected: {path}",
                ) from None
            if len(content) > self.limits.max_changed_file_bytes:
                raise ShadowWorkspaceError(
                    "SHADOW_CHANGED_FILE_LIMIT",
                    f"Changed file exceeds limit: {path}",
                )
            total_bytes += len(content)
            if total_bytes > self.limits.max_change_set_bytes:
                raise ShadowWorkspaceError(
                    "SHADOW_CHANGE_SET_LIMIT",
                    "Shadow change set exceeds the total byte limit.",
                )
            if len(changed) >= self.limits.max_changed_files:
                raise ShadowWorkspaceError(
                    "SHADOW_CHANGE_COUNT_LIMIT",
                    "Shadow worker changed too many files.",
                )
            changed.append(
                WorkspaceFileChange(
                    path=path,
                    kind=FileChangeKind.MODIFY if before is not None else FileChangeKind.ADD,
                    base_sha256=before.sha256 if before is not None else None,
                    new_sha256=digest,
                    old_content=old_text,
                    new_content=new_text,
                )
            )
        if not changed:
            return None
        return WorkspaceChangeSet(
            change_set_id=str(uuid.uuid4()),
            workspace_id=workspace_id,
            files=tuple(changed),
            total_bytes=total_bytes,
        )


class ChangeSetCatalog:
    """Process-local holder for validated changes awaiting one explicit apply."""

    def __init__(
        self,
        workspaces: Mapping[str, Path],
        *,
        max_preview_bytes: int = 16_000,
    ) -> None:
        self.workspaces = {key: value.expanduser().resolve() for key, value in workspaces.items()}
        self.max_preview_bytes = max_preview_bytes
        self._values: dict[str, WorkspaceChangeSet] = {}
        self._applied: set[str] = set()
        self._lock = threading.RLock()

    def add(self, change_set: WorkspaceChangeSet) -> None:
        if change_set.workspace_id not in self.workspaces:
            raise ValueError("Change set references an unknown workspace")
        with self._lock:
            if change_set.change_set_id in self._values:
                raise ValueError("Change set id already exists")
            self._values[change_set.change_set_id] = change_set

    def get(self, change_set_id: str) -> WorkspaceChangeSet:
        with self._lock:
            value = self._values.get(change_set_id)
        if value is None:
            raise ToolValidationError("Unknown or expired change set")
        return value

    def preview(self, change_set_id: str) -> str:
        change_set = self.get(change_set_id)
        lines = [
            f"Apply {len(change_set.files)} shadow-generated file change(s) "
            f"({change_set.total_bytes} bytes)",
        ]
        for change in change_set.files:
            before = (change.old_content or "").splitlines(keepends=True)
            after = change.new_content.splitlines(keepends=True)
            lines.extend(
                difflib.unified_diff(
                    before,
                    after,
                    fromfile=f"a/{change.path}" if change.old_content is not None else "/dev/null",
                    tofile=f"b/{change.path}",
                    lineterm="",
                )
            )
        value = "\n".join(lines)
        encoded = value.encode("utf-8")
        if len(encoded) <= self.max_preview_bytes:
            return value
        suffix = "\n… diff preview truncated; apply still covers the complete validated change set"
        return encoded[: self.max_preview_bytes - len(suffix.encode("utf-8"))].decode(
            "utf-8",
            errors="ignore",
        ) + suffix

    def definition(self) -> ToolDefinition:
        def validate(arguments: Mapping[str, object]) -> Mapping[str, object]:
            if set(arguments) != {"workspace_id", "change_set_id"}:
                raise ToolValidationError(
                    "apply_workspace_change_set requires workspace_id and change_set_id"
                )
            workspace_id = arguments.get("workspace_id")
            change_set_id = arguments.get("change_set_id")
            if not isinstance(workspace_id, str) or workspace_id not in self.workspaces:
                raise ToolValidationError("Unknown workspace_id")
            if not isinstance(change_set_id, str) or not change_set_id.strip():
                raise ToolValidationError("change_set_id must be non-empty")
            change_set = self.get(change_set_id)
            if change_set.workspace_id != workspace_id:
                raise ToolValidationError("Change set belongs to a different workspace")
            return {"workspace_id": workspace_id, "change_set_id": change_set_id}

        async def handle(
            arguments: Mapping[str, object],
            cancellation: CancellationToken,
        ) -> str:
            cancellation.raise_if_cancelled()
            change_set_id = str(arguments["change_set_id"])
            applied = await asyncio.to_thread(self._apply, change_set_id)
            cancellation.raise_if_cancelled()
            return json.dumps(
                {
                    "change_set_id": applied.change_set_id,
                    "files": [change.path for change in applied.files],
                    "bytes_written": applied.total_bytes,
                },
                ensure_ascii=False,
                sort_keys=True,
            )

        return ToolDefinition(
            name=APPLY_CHANGE_SET_TOOL,
            description=(
                "Apply one Noruct-validated shadow workspace change set to the real workspace "
                "after checking every base hash."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "string"},
                    "change_set_id": {"type": "string"},
                },
                "required": ["workspace_id", "change_set_id"],
                "additionalProperties": False,
            },
            effect=ToolEffect.WRITE,
            risk=ToolRisk.MEDIUM,
            idempotency_mode=IdempotencyMode.CALL_KEY,
            validator=validate,
            resource_key=lambda arguments: (
                f"workspace:{arguments['workspace_id']}:change-set:{arguments['change_set_id']}"
            ),
            handler=handle,
            timeout_ms=30_000,
            output_limit_bytes=64_000,
            requires_approval=True,
            approval_preview=lambda arguments: self.preview(str(arguments["change_set_id"])),
            allow_session_approval=False,
        )

    def _apply(self, change_set_id: str) -> WorkspaceChangeSet:
        with self._lock:
            change_set = self.get(change_set_id)
            if change_set_id in self._applied:
                raise ToolValidationError("Change set was already applied")
            root = self.workspaces[change_set.workspace_id]
            targets: list[tuple[WorkspaceFileChange, Path, bytes | None]] = []
            for change in change_set.files:
                target = checked_workspace_mutation_target(root, change.path)
                if target.exists():
                    original = target.read_bytes()
                    if change.base_sha256 is None:
                        raise ToolValidationError(
                            f"New change target already exists: {change.path}"
                        )
                    if _sha256(original) != change.base_sha256:
                        raise ToolValidationError(
                            f"Workspace changed since shadow snapshot: {change.path}"
                        )
                else:
                    original = None
                    if change.base_sha256 is not None:
                        raise ToolValidationError(
                            f"Original change target no longer exists: {change.path}"
                        )
                if _sha256(change.new_content.encode("utf-8")) != change.new_sha256:
                    raise ToolValidationError("Change set content hash is invalid")
                targets.append((change, target, original))

            applied: list[tuple[Path, bytes | None]] = []
            try:
                for change, target, original in targets:
                    atomic_write_text(target, change.new_content)
                    applied.append((target, original))
            except Exception as exc:
                rollback_failed = False
                for target, original in reversed(applied):
                    try:
                        if original is None:
                            target.unlink(missing_ok=True)
                        else:
                            atomic_write_text(target, original.decode("utf-8"))
                    except Exception:
                        rollback_failed = True
                if rollback_failed:
                    raise RuntimeError("Change set apply became indeterminate during rollback") from exc
                raise
            self._applied.add(change_set_id)
            return change_set
