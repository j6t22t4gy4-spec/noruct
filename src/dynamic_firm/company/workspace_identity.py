from __future__ import annotations

import os
import time
from collections import Counter, deque
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence

from .models import content_digest


WORKSPACE_STRUCTURE_PROJECTION_REVISION = "noruct.workspace-structure.v2"
WORKFLOW_CONTEXT_FINGERPRINT_REVISION = "noruct.workflow-context.v2"

DEFAULT_MAX_SCANNED_ENTRIES = 10_000
DEFAULT_MAX_SCAN_DEPTH = 24
DEFAULT_MAX_SCAN_SECONDS = 2.0
MAX_EXTENSION_KINDS = 64

# This is a versioned company-identity policy, not an ambient .gitignore. Changes
# alter workspace identity and therefore require a projection revision change.
PROTECTED_TREE_SEGMENTS = frozenset(
    {
        ".cache",
        ".codex",
        ".git",
        ".hg",
        ".mypy_cache",
        ".next",
        ".noruct",
        ".npm",
        ".pnpm-store",
        ".pytest_cache",
        ".ruff_cache",
        ".svn",
        ".tox",
        ".turbo",
        ".venv",
        ".vite",
        ".yarn",
        "__pycache__",
        "build",
        "coverage",
        "dist",
        "node_modules",
        "runtime-services",
        "target",
        "vendor",
        "venv",
    }
)

KNOWN_PROJECT_MARKERS = frozenset(
    {
        ".cursorrules",
        "agents.md",
        "build.gradle",
        "build.gradle.kts",
        "cargo.toml",
        "claude.md",
        "cmakelists.txt",
        "composer.json",
        "deno.json",
        "dockerfile",
        "gemfile",
        "go.mod",
        "makefile",
        "mix.exs",
        "package.json",
        "pom.xml",
        "pubspec.yaml",
        "pyproject.toml",
        "requirements.txt",
        "setup.cfg",
        "setup.py",
        "tsconfig.json",
    }
)

_SENSITIVE_FILE_NAMES = frozenset(
    {
        ".env",
        ".netrc",
        ".npmrc",
        ".pypirc",
        "credentials",
        "credentials.json",
        "id_ed25519",
        "id_rsa",
        "kubeconfig",
    }
)
_SENSITIVE_SUFFIXES = frozenset({".key", ".p12", ".pem", ".pfx"})


class WorkspaceProjectionFailureCode(StrEnum):
    INVALID_LIMITS = "INVALID_LIMITS"
    ROOT_UNAVAILABLE = "ROOT_UNAVAILABLE"
    ROOT_NOT_DIRECTORY = "ROOT_NOT_DIRECTORY"
    ROOT_SYMLINK = "ROOT_SYMLINK"
    ROOT_UNREADABLE = "ROOT_UNREADABLE"
    TIME_BUDGET_EXCEEDED = "TIME_BUDGET_EXCEEDED"


class WorkspaceProjectionError(RuntimeError):
    """A redacted, stable failure at the workspace identity boundary."""

    def __init__(self, code: WorkspaceProjectionFailureCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, slots=True)
class WorkspaceStructureProjection:
    """Privacy-bounded structure used to identify a workflow context.

    This value deliberately cannot contain a workspace root, relative path, file
    name outside the marker allow-list, content, timestamps, or exact file counts.
    """

    revision: str
    execution_profile: str
    extension_histogram: tuple[tuple[str, int], ...]
    project_markers: tuple[str, ...]
    file_count_bucket: int
    maximum_depth_bucket: int
    truncated: bool
    truncation_reasons: tuple[str, ...]


def _count_bucket(count: int) -> int:
    if count <= 0:
        return 0
    return min(20, 1 + (count - 1) // 10)


def _extension(file_name: str) -> str:
    return PurePosixPath(file_name).suffix.lower() or "<none>"


def _is_sensitive_file(file_name: str) -> bool:
    lowered = file_name.casefold()
    return (
        lowered in _SENSITIVE_FILE_NAMES
        or lowered.startswith(".env.")
        or PurePosixPath(lowered).suffix in _SENSITIVE_SUFFIXES
    )


def _normalize_excluded_paths(paths: Sequence[str | Path]) -> frozenset[tuple[str, ...]]:
    normalized: set[tuple[str, ...]] = set()
    for value in paths:
        raw = str(value).replace("\\", "/").strip("/")
        path = PurePosixPath(raw)
        if not raw or path.is_absolute() or ".." in path.parts:
            continue
        parts = tuple(part.casefold() for part in path.parts if part not in {"", "."})
        if parts:
            normalized.add(parts)
    return frozenset(normalized)


def _is_explicitly_excluded(
    relative_parts: tuple[str, ...],
    exclusions: frozenset[tuple[str, ...]],
) -> bool:
    lowered = tuple(part.casefold() for part in relative_parts)
    return any(lowered[: len(excluded)] == excluded for excluded in exclusions)


def _projection(
    *,
    execution_profile: str,
    extensions: Counter[str],
    markers: set[str],
    file_count: int,
    maximum_depth: int,
    truncation_reasons: set[str],
) -> WorkspaceStructureProjection:
    histogram = sorted(
        (extension, _count_bucket(count))
        for extension, count in extensions.items()
    )
    if len(histogram) > MAX_EXTENSION_KINDS:
        histogram = histogram[:MAX_EXTENSION_KINDS]
        truncation_reasons.add("EXTENSION_KIND_LIMIT")
    reasons = tuple(sorted(truncation_reasons))
    return WorkspaceStructureProjection(
        revision=WORKSPACE_STRUCTURE_PROJECTION_REVISION,
        execution_profile=execution_profile,
        extension_histogram=tuple(histogram),
        project_markers=tuple(sorted(markers)),
        file_count_bucket=_count_bucket(file_count),
        maximum_depth_bucket=min(10, maximum_depth),
        truncated=bool(reasons),
        truncation_reasons=reasons,
    )


def project_workspace_structure(
    root: Path,
    execution_profile: str,
    *,
    excluded_paths: Sequence[str | Path] = (),
    max_entries: int = DEFAULT_MAX_SCANNED_ENTRIES,
    max_depth: int = DEFAULT_MAX_SCAN_DEPTH,
    max_seconds: float = DEFAULT_MAX_SCAN_SECONDS,
) -> WorkspaceStructureProjection:
    """Aggregate one bounded, deterministic workspace structure projection.

    Directory entries are sorted before traversal, symlinks are never followed,
    protected/generated trees are pruned, and only bounded aggregate buckets are
    returned. Entry/depth overflow is a valid projection; a time-budget failure is
    fail-closed because a speed-dependent partial fingerprint would not be stable.
    """

    if max_entries < 1 or max_depth < 0 or max_seconds <= 0:
        raise WorkspaceProjectionError(WorkspaceProjectionFailureCode.INVALID_LIMITS)
    if not root.exists():
        raise WorkspaceProjectionError(WorkspaceProjectionFailureCode.ROOT_UNAVAILABLE)
    if root.is_symlink():
        raise WorkspaceProjectionError(WorkspaceProjectionFailureCode.ROOT_SYMLINK)
    if not root.is_dir():
        raise WorkspaceProjectionError(WorkspaceProjectionFailureCode.ROOT_NOT_DIRECTORY)

    deadline = time.monotonic() + max_seconds
    root = root.resolve()
    exclusions = _normalize_excluded_paths(excluded_paths)
    pending: deque[tuple[Path, tuple[str, ...], int]] = deque([(root, (), 0)])
    extensions: Counter[str] = Counter()
    markers: set[str] = set()
    truncation_reasons: set[str] = set()
    file_count = 0
    maximum_depth = 0
    scanned_entries = 0
    root_read = False

    while pending:
        if time.monotonic() >= deadline:
            raise WorkspaceProjectionError(
                WorkspaceProjectionFailureCode.TIME_BUDGET_EXCEEDED
            )
        directory, relative_parts, depth = pending.popleft()
        try:
            with os.scandir(directory) as iterator:
                entries = []
                for entry in iterator:
                    if time.monotonic() >= deadline:
                        raise WorkspaceProjectionError(
                            WorkspaceProjectionFailureCode.TIME_BUDGET_EXCEEDED
                        )
                    entries.append(entry)
            entries.sort(key=lambda item: (item.name.casefold(), item.name))
            root_read = root_read or directory == root
        except WorkspaceProjectionError:
            raise
        except OSError:
            if directory == root and not root_read:
                raise WorkspaceProjectionError(
                    WorkspaceProjectionFailureCode.ROOT_UNREADABLE
                ) from None
            truncation_reasons.add("UNREADABLE_ENTRY")
            continue

        for entry in entries:
            if time.monotonic() >= deadline:
                raise WorkspaceProjectionError(
                    WorkspaceProjectionFailureCode.TIME_BUDGET_EXCEEDED
                )
            entry_parts = relative_parts + (entry.name,)
            lowered_name = entry.name.casefold()
            if lowered_name in PROTECTED_TREE_SEGMENTS:
                continue
            if _is_explicitly_excluded(entry_parts, exclusions):
                continue

            scanned_entries += 1
            if scanned_entries > max_entries:
                truncation_reasons.add("ENTRY_LIMIT")
                pending.clear()
                break

            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if depth >= max_depth:
                        truncation_reasons.add("DEPTH_LIMIT")
                        continue
                    pending.append((Path(entry.path), entry_parts, depth + 1))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
            except OSError:
                truncation_reasons.add("UNREADABLE_ENTRY")
                continue

            if _is_sensitive_file(entry.name):
                continue
            file_depth = depth + 1
            maximum_depth = max(maximum_depth, file_depth)
            file_count += 1
            extensions[_extension(entry.name)] += 1
            if lowered_name in KNOWN_PROJECT_MARKERS:
                markers.add(lowered_name)

    return _projection(
        execution_profile=execution_profile,
        extensions=extensions,
        markers=markers,
        file_count=file_count,
        maximum_depth=maximum_depth,
        truncation_reasons=truncation_reasons,
    )


def project_workspace_manifest(
    execution_profile: str,
    workspace_manifest: Iterable[str],
) -> WorkspaceStructureProjection:
    """Compatibility/test adapter that normalizes separators before v2 projection."""

    extensions: Counter[str] = Counter()
    markers: set[str] = set()
    file_count = 0
    maximum_depth = 0
    for raw_path in workspace_manifest:
        normalized = str(raw_path).replace("\\", "/").strip("/")
        path = PurePosixPath(normalized)
        if not normalized or path.is_absolute() or ".." in path.parts:
            continue
        lowered_parts = tuple(part.casefold() for part in path.parts)
        if any(part in PROTECTED_TREE_SEGMENTS for part in lowered_parts[:-1]):
            continue
        name = path.name
        if _is_sensitive_file(name):
            continue
        file_count += 1
        maximum_depth = max(maximum_depth, len(path.parts))
        extensions[_extension(name)] += 1
        if name.casefold() in KNOWN_PROJECT_MARKERS:
            markers.add(name.casefold())
    return _projection(
        execution_profile=execution_profile,
        extensions=extensions,
        markers=markers,
        file_count=file_count,
        maximum_depth=maximum_depth,
        truncation_reasons=set(),
    )


def workflow_context_fingerprint_v2(
    projection: WorkspaceStructureProjection,
) -> str:
    """Hash only the canonical v2 bounded projection, never a path or content."""

    payload = {
        "revision": WORKFLOW_CONTEXT_FINGERPRINT_REVISION,
        "projection": projection,
    }
    return f"wctx2-{content_digest(payload)[:24]}"
