"""Bounded compatibility for user-owned external ``SKILL.md`` packages.

The active instruction remains an immutable per-Job ``VersionedContent``
snapshot.  Unlike the first compatibility slice, a selected package may also
expose bounded, text-only files in its conventional ``references/``,
``templates/``, ``assets/`` and ``scripts/`` directories through one
first-party *read* tool.  This keeps the progressive-disclosure shape used by
other agent skill ecosystems without importing a skill's code, credentials or
filesystem authority into the Employee worker.

Support files are never executed or installed.  A script is ordinary text
until the employee explicitly uses an existing approved workspace command or
an executable capability package; a ``SKILL.md`` alone never grants execution
authority.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping, Sequence

from dynamic_firm.runtime.knowledge_retrieval import BoundedKnowledgeRetriever
from dynamic_firm.runtime.models import IdempotencyMode, ToolEffect, ToolRisk, VersionedContent
from dynamic_firm.runtime.ports import CancellationToken
from dynamic_firm.runtime.tools import ToolDefinition, ToolExecutionError, ToolValidationError


_MAX_DIRECTORIES = 8
_MAX_SKILLS_PER_DIRECTORY = 64
_MAX_FILE_BYTES = 16_000
_MAX_SUPPORT_FILES_PER_SKILL = 64
_MAX_SUPPORT_FILE_BYTES = 24_000
_PACKAGE_MANIFEST_SCHEMA = "noruct.external-skill-package-manifest.v1"
_SKILL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_PLATFORM = {"darwin": "macos", "linux": "linux", "win32": "windows"}.get(
    sys.platform,
    sys.platform,
)


@dataclass(frozen=True, slots=True)
class ExternalSkillLoad:
    snapshots: Mapping[str, tuple[VersionedContent, ...]]
    discovered_count: int
    skipped_count: int


@dataclass(frozen=True, slots=True)
class ExternalSkillInfo:
    """One compatible, user-owned instruction file with immutable identity."""

    name: str
    description: str
    platforms: tuple[str, ...]
    root: Path
    package_root: Path
    relative_path: str
    snapshot: VersionedContent
    support_files: tuple["ExternalSkillSupportFile", ...] = ()


@dataclass(frozen=True, slots=True)
class ExternalSkillSupportFile:
    """Content-free identity for one progressive-disclosure package file."""

    relative_path: str
    byte_size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ExternalSkillCatalog:
    """A fresh read-only filesystem scan; no catalog is persisted or cached."""

    roots: tuple[Path, ...]
    skills: tuple[ExternalSkillInfo, ...]
    skipped_count: int


class ExternalSkillPackageTools:
    """Read selected external skill package files through the ToolRegistry.

    The parent creates this object only for the immutable set selected for a
    Job.  The private Employee worker sees a normal first-party READ tool and
    never receives package paths or direct filesystem authority.
    """

    tool_name = "read_external_skill_support"

    def __init__(self, skills: Sequence[ExternalSkillInfo]) -> None:
        self._skills = {item.name: item for item in skills}

    def definitions(self) -> tuple[ToolDefinition, ...]:
        def validate(arguments: Mapping[str, object]) -> Mapping[str, object]:
            name = arguments.get("skill")
            relative_path = arguments.get("path")
            if set(arguments) != {"skill", "path"}:
                raise ToolValidationError("Skill support request must contain only skill and path")
            if not isinstance(name, str) or name not in self._skills:
                raise ToolValidationError("Selected external skill was not found")
            if not isinstance(relative_path, str) or not relative_path:
                raise ToolValidationError("Skill support path is invalid")
            support = next(
                (item for item in self._skills[name].support_files if item.relative_path == relative_path),
                None,
            )
            if support is None:
                raise ToolValidationError("Requested file is not an available skill support file")
            return {"skill": name, "path": support.relative_path}

        async def handle(arguments: Mapping[str, object], _cancellation: CancellationToken) -> str:
            name = str(arguments["skill"])
            relative_path = str(arguments["path"])
            skill = self._skills[name]
            try:
                content = _read_support_content(skill, relative_path)
            except ValueError as exc:
                raise ToolExecutionError("Skill support file is no longer readable") from exc
            return json.dumps(
                {"skill": name, "path": relative_path, "content": content},
                ensure_ascii=False,
                sort_keys=True,
            )

        return (
            ToolDefinition(
                name=self.tool_name,
                description=(
                    "Read one text support file from a selected external SKILL.md package. "
                    "This never executes package scripts or installs dependencies."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "skill": {"type": "string"},
                        "path": {"type": "string"},
                    },
                    "required": ["skill", "path"],
                    "additionalProperties": False,
                },
                effect=ToolEffect.READ,
                risk=ToolRisk.LOW,
                idempotency_mode=IdempotencyMode.CALL_KEY,
                validator=validate,
                resource_key=lambda arguments: (
                    f"skill-package:{self._skills[str(arguments['skill'])].snapshot.content_id}:{arguments['path']}"
                ),
                handler=handle,
                timeout_ms=2_000,
                output_limit_bytes=_MAX_SUPPORT_FILE_BYTES + 2_000,
                parallel_safe=True,
            ),
        )


def external_skill_directories(value: object) -> tuple[Path, ...]:
    """Normalize at most eight user-owned skill roots without reading files."""
    if value is None:
        return ()
    raw = [value] if isinstance(value, (str, Path)) else value
    if not isinstance(raw, (list, tuple)):
        raise ValueError("skills.external_dirs must be a path or list of paths")
    if len(raw) > _MAX_DIRECTORIES:
        raise ValueError(f"skills.external_dirs supports at most {_MAX_DIRECTORIES} directories")
    result: list[Path] = []
    seen: set[Path] = set()
    for item in raw:
        if not isinstance(item, (str, Path)) or not str(item).strip():
            raise ValueError("skills.external_dirs entries must be non-empty paths")
        path = Path(item).expanduser().resolve()
        if path in seen:
            continue
        seen.add(path)
        if path.is_dir():
            result.append(path)
    return tuple(result)


def load_external_skill_snapshots(
    directories: Sequence[Path],
    *,
    employee_ids: Sequence[str],
    query: str,
    limit_per_employee: int = 3,
) -> ExternalSkillLoad:
    """Return selected read-only skill instructions for the current Job only."""
    catalog = discover_external_skills(directories)
    selected = tuple(
        item.snapshot
        for item in select_external_skills(
            catalog, query=query, limit=limit_per_employee
        )
    )
    snapshots = {employee_id: selected for employee_id in employee_ids}
    return ExternalSkillLoad(snapshots, len(catalog.skills), catalog.skipped_count)


def discover_external_skills(directories: Sequence[Path]) -> ExternalSkillCatalog:
    """Scan configured roots now for list/inspect/preview and Job projection.

    This function is intentionally fresh on every call.  It does not install,
    copy, cache, or follow linked content from a skill folder.
    """
    found: list[ExternalSkillInfo] = []
    names: set[str] = set()
    skipped = 0
    roots: list[Path] = []
    for root_index, configured_root in enumerate(directories):
        root = Path(configured_root).expanduser().resolve()
        if not root.is_dir():
            continue
        roots.append(root)
        scanned = 0
        try:
            candidates = sorted(root.rglob("SKILL.md"))
        except OSError:
            continue
        for skill_file in candidates:
            if scanned >= _MAX_SKILLS_PER_DIRECTORY:
                break
            scanned += 1
            parsed = _read_skill(root, skill_file, root_index)
            if parsed is None:
                skipped += 1
                continue
            if parsed.name in names:
                skipped += 1
                continue
            names.add(parsed.name)
            found.append(parsed)
    return ExternalSkillCatalog(tuple(roots), tuple(found), skipped)


def select_external_skills(
    catalog: ExternalSkillCatalog,
    *,
    query: str,
    limit: int = 3,
) -> tuple[ExternalSkillInfo, ...]:
    """Return the same bounded, query-aware selection used for one Job."""
    selected = BoundedKnowledgeRetriever().select(
        tuple(item.snapshot for item in catalog.skills),
        query=query,
        limit=limit,
        max_bytes=12_000,
        fallback_count=limit,
    ).items
    by_identity = {
        (item.snapshot.content_id, item.snapshot.revision): item
        for item in catalog.skills
    }
    return tuple(
        by_identity[(item.content_id, item.revision)]
        for item in selected
        if (item.content_id, item.revision) in by_identity
    )


def _read_skill(root: Path, skill_file: Path, root_index: int) -> ExternalSkillInfo | None:
    try:
        if skill_file.is_symlink() or not skill_file.is_file():
            return None
        resolved = skill_file.resolve()
        relative = resolved.relative_to(root)
        raw = resolved.read_bytes()
    except (OSError, ValueError):
        return None
    if len(raw) > _MAX_FILE_BYTES or b"\x00" in raw:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    metadata, body = _frontmatter(text)
    name = metadata.get("name", skill_file.parent.name).strip()
    if _SKILL_NAME.fullmatch(name) is None:
        return None
    platforms = _platforms(metadata.get("platforms", ""))
    if platforms and _PLATFORM not in platforms:
        return None
    description = metadata.get("description", "").strip()
    if not body.strip():
        return None
    support_files = _discover_support_files(skill_file.parent)
    rendered = f"# External Skill: {name}\n"
    if description:
        rendered += f"{description}\n\n"
    rendered += body.strip()
    if support_files:
        rendered += (
            "\n\n## Package support files\n"
            "Use `read_external_skill_support` only when a listed file is needed. "
            "Files are read-only text; scripts are never executed by this skill.\n"
            + "\n".join(f"- `{item.relative_path}`" for item in support_files)
        )
    # Freeze the whole readable package closure, not only SKILL.md.  This
    # digest survives in the per-Job VersionedContent snapshot, so a later
    # same-Job continuation can reassemble the local READ tool only when every
    # progressive-disclosure file is still byte-identical.  Paths and hashes
    # are content-free metadata; package contents remain local.
    digest = hashlib.sha256(
        json.dumps(
            {
                "schema": _PACKAGE_MANIFEST_SCHEMA,
                "skill_markdown_sha256": hashlib.sha256(raw).hexdigest(),
                "support_files": [
                    {
                        "path": item.relative_path,
                        "byte_size": item.byte_size,
                        "sha256": item.sha256,
                    }
                    for item in support_files
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    identity = hashlib.sha256(
        f"{root_index}:{relative.as_posix()}".encode("utf-8")
    ).hexdigest()[:16]
    return ExternalSkillInfo(
        name=name,
        description=description,
        platforms=tuple(sorted(platforms)),
        root=root,
        package_root=skill_file.parent.resolve(),
        relative_path=relative.as_posix(),
        snapshot=VersionedContent(
            content_id=f"external-skill:{identity}",
            revision=f"sha256:{digest[:16]}",
            content=rendered,
            content_hash=digest,
        ),
        support_files=support_files,
    )


def _discover_support_files(skill_root: Path) -> tuple[ExternalSkillSupportFile, ...]:
    """Return bounded metadata for conventional skill package support files."""

    result: list[ExternalSkillSupportFile] = []
    for directory_name in ("references", "templates", "assets", "scripts"):
        directory = skill_root / directory_name
        if not directory.is_dir() or directory.is_symlink():
            continue
        try:
            candidates = sorted(path for path in directory.rglob("*") if path.is_file())
        except OSError:
            continue
        for candidate in candidates:
            if len(result) >= _MAX_SUPPORT_FILES_PER_SKILL:
                return tuple(result)
            try:
                if candidate.is_symlink():
                    continue
                resolved = candidate.resolve()
                relative = resolved.relative_to(skill_root.resolve()).as_posix()
                raw = resolved.read_bytes()
            except (OSError, ValueError):
                continue
            if not raw or len(raw) > _MAX_SUPPORT_FILE_BYTES or b"\x00" in raw:
                continue
            try:
                raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
            result.append(
                ExternalSkillSupportFile(
                    relative_path=relative,
                    byte_size=len(raw),
                    sha256=hashlib.sha256(raw).hexdigest(),
                )
            )
    return tuple(result)


def _read_support_content(skill: ExternalSkillInfo, relative_path: str) -> str:
    """Re-read one frozen-at-selection text support path and reject changes."""

    requested = PurePosixPath(relative_path)
    if requested.is_absolute() or ".." in requested.parts:
        raise ValueError("Skill support path escaped its package")
    expected = next((item for item in skill.support_files if item.relative_path == relative_path), None)
    if expected is None:
        raise ValueError("Skill support file was not selected")
    try:
        unresolved = skill.package_root / requested
        if unresolved.is_symlink():
            raise ValueError("Skill support file is not regular")
        candidate = unresolved.resolve()
        candidate.relative_to(skill.package_root.resolve())
        if not candidate.is_file():
            raise ValueError("Skill support file is not regular")
        raw = candidate.read_bytes()
    except OSError as exc:
        raise ValueError("Skill support file is unreadable") from exc
    if not raw or len(raw) > _MAX_SUPPORT_FILE_BYTES or b"\x00" in raw:
        raise ValueError("Skill support file is invalid")
    if hashlib.sha256(raw).hexdigest() != expected.sha256:
        raise ValueError("Skill support file changed after Job selection")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Skill support file is not UTF-8") from exc


def _frontmatter(text: str) -> tuple[dict[str, str], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    try:
        end = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration:
        return {}, text
    metadata: dict[str, str] = {}
    for line in lines[1:end]:
        if ":" not in line or line.startswith((" ", "\t")):
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        if key in {"name", "description", "platforms"}:
            metadata[key] = value.strip().strip("'\"")
    return metadata, "\n".join(lines[end + 1 :])


def _platforms(value: str) -> frozenset[str]:
    normalized = value.strip().strip("[]")
    if not normalized:
        return frozenset()
    return frozenset(
        item.strip().strip("'\"").lower()
        for item in normalized.split(",")
        if item.strip()
    )
