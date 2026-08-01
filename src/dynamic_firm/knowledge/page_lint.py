"""Bounded, read-only health checks for human-readable Knowledge pages."""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path, PurePosixPath

from .folder_service import KnowledgeFolderService
from .store import KnowledgeStore


_WIKILINK = re.compile(r"\[\[([^\[\]]{1,512})\]\]")
_REQUIRED_FRONTMATTER = frozenset({"title", "created", "updated", "type"})


@dataclass(frozen=True, slots=True)
class KnowledgePageLintIssue:
    severity: str
    code: str
    relative_path: str
    reference: str = ""


@dataclass(frozen=True, slots=True)
class KnowledgePageLintReport:
    folder_id: str
    scanned_pages: int
    scanned_bytes: int
    truncated: bool
    issues: tuple[KnowledgePageLintIssue, ...]

    @property
    def passed(self) -> bool:
        return not any(issue.severity == "ERROR" for issue in self.issues)


def _frontmatter(text: str) -> dict[str, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    result: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return result
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        normalized = key.strip().casefold()
        if normalized and normalized not in result:
            result[normalized] = value.strip()
    return {}


def _list_value(value: str) -> tuple[str, ...]:
    normalized = value.strip()
    if not normalized:
        return ()
    if normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1]
    return tuple(
        item.strip().strip("\"'")
        for item in normalized.split(",")
        if item.strip().strip("\"'")
    )


def _page_key(relative: str) -> str:
    return PurePosixPath(relative).with_suffix("").as_posix()


def _link_target(raw: str) -> str:
    target = raw.split("|", 1)[0].split("#", 1)[0].strip().replace("\\", "/")
    if target.startswith("./"):
        target = target[2:]
    if target.startswith("pages/"):
        target = target[6:]
    if target.casefold().endswith(".md"):
        target = target[:-3]
    return target.strip("/")


def _discover_markdown_pages(
    pages_root: Path,
    *,
    max_entries: int,
    max_pages: int,
) -> tuple[list[Path], list[KnowledgePageLintIssue], bool]:
    """Discover a bounded, deterministic-enough local page set without links."""

    paths: list[Path] = []
    issues: list[KnowledgePageLintIssue] = []
    stack = [pages_root]
    observed_entries = 0
    truncated = False
    while stack and not truncated:
        directory = stack.pop()
        entries: list[os.DirEntry[str]] = []
        try:
            iterator = os.scandir(directory)
            with iterator:
                for entry in iterator:
                    observed_entries += 1
                    if observed_entries > max_entries:
                        truncated = True
                        break
                    entries.append(entry)
        except OSError:
            relative = directory.relative_to(pages_root).as_posix()
            issues.append(
                KnowledgePageLintIssue(
                    "ERROR",
                    "DIRECTORY_UNREADABLE",
                    relative if relative != "." else "",
                )
            )
            continue
        directories: list[Path] = []
        for entry in sorted(entries, key=lambda item: item.name.casefold()):
            path = Path(entry.path)
            relative = path.relative_to(pages_root).as_posix()
            try:
                if entry.is_symlink():
                    if entry.name.casefold().endswith(".md"):
                        issues.append(
                            KnowledgePageLintIssue(
                                "ERROR", "SYMLINK_PAGE_REJECTED", relative
                            )
                        )
                    elif entry.is_dir(follow_symlinks=True):
                        issues.append(
                            KnowledgePageLintIssue(
                                "ERROR", "SYMLINK_DIRECTORY_REJECTED", relative
                            )
                        )
                    continue
                if entry.is_dir(follow_symlinks=False):
                    directories.append(path)
                elif entry.is_file(follow_symlinks=False) and entry.name.casefold().endswith(
                    ".md"
                ):
                    if len(paths) >= max_pages:
                        truncated = True
                        break
                    paths.append(path)
            except OSError:
                issues.append(
                    KnowledgePageLintIssue("ERROR", "ENTRY_UNREADABLE", relative)
                )
        stack.extend(reversed(directories))
    return paths, issues, truncated


def _read_page(path: Path, *, max_page_bytes: int) -> tuple[str, int]:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode):
            raise ValueError("PAGE_NOT_REGULAR")
        if observed.st_size > max_page_bytes:
            raise ValueError("PAGE_BYTE_LIMIT_EXCEEDED")
        chunks: list[bytes] = []
        remaining = max_page_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > max_page_bytes:
            raise ValueError("PAGE_BYTE_LIMIT_EXCEEDED")
        return payload.decode("utf-8"), len(payload)
    finally:
        os.close(descriptor)


class KnowledgePageLinter:
    def __init__(self, store: KnowledgeStore) -> None:
        self.store = store

    def lint(
        self,
        *,
        folder_id: str,
        as_of: date | None = None,
        stale_after_days: int = 90,
        max_pages: int = 1000,
        max_entries: int = 10_000,
        max_page_bytes: int = 256_000,
        max_total_bytes: int = 32_000_000,
    ) -> KnowledgePageLintReport:
        if stale_after_days < 1 or stale_after_days > 3650:
            raise ValueError("Knowledge page stale window must be between 1 and 3650 days")
        if max_pages < 1 or max_pages > 10_000:
            raise ValueError("Knowledge page lint limit must be between 1 and 10000")
        if max_entries < max_pages or max_entries > 100_000:
            raise ValueError(
                "Knowledge page entry limit must cover pages and be at most 100000"
            )
        if max_page_bytes < 1 or max_page_bytes > 4_000_000:
            raise ValueError(
                "Knowledge page byte limit must be between 1 and 4000000"
            )
        if max_total_bytes < 1 or max_total_bytes > 1_000_000_000:
            raise ValueError(
                "Knowledge page total byte limit must be between 1 and 1000000000"
            )
        folder = self.store.knowledge_folder(folder_id)
        if folder is None:
            raise ValueError("Knowledge page lint Folder was not found")
        root = KnowledgeFolderService.validate_root(folder.root_path)
        pages_root = root / "pages"
        if pages_root.is_symlink():
            raise ValueError("Knowledge pages root must not be a symbolic link")
        if not pages_root.exists():
            return KnowledgePageLintReport(folder_id, 0, 0, False, ())
        if not pages_root.is_dir():
            raise ValueError("Knowledge pages root must be a directory")

        candidates, issues, truncated = _discover_markdown_pages(
            pages_root,
            max_entries=max_entries,
            max_pages=max_pages,
        )
        documents: dict[str, tuple[str, dict[str, str], str]] = {}
        scanned_bytes = 0
        for path in candidates:
            if scanned_bytes >= max_total_bytes:
                truncated = True
                break
            relative_path = path.relative_to(pages_root).as_posix()
            try:
                text, size = _read_page(path, max_page_bytes=max_page_bytes)
            except ValueError as error:
                issues.append(
                    KnowledgePageLintIssue(
                        "ERROR", str(error), relative_path
                    )
                )
                continue
            except UnicodeError:
                issues.append(
                    KnowledgePageLintIssue(
                        "ERROR", "PAGE_UNREADABLE_UTF8", relative_path
                    )
                )
                continue
            except OSError:
                issues.append(
                    KnowledgePageLintIssue("ERROR", "PAGE_UNREADABLE", relative_path)
                )
                continue
            if scanned_bytes + size > max_total_bytes:
                issues.append(
                    KnowledgePageLintIssue(
                        "ERROR", "TOTAL_BYTE_LIMIT_EXCEEDED", relative_path
                    )
                )
                truncated = True
                break
            scanned_bytes += size
            documents[_page_key(relative_path)] = (
                relative_path,
                _frontmatter(text),
                text,
            )

        content_keys = {
            key
            for key in documents
            if key not in {"index", "_meta/topic-map"}
        }
        basename_map: dict[str, list[str]] = {}
        for key in documents:
            basename_map.setdefault(PurePosixPath(key).name, []).append(key)
        inbound: dict[str, set[str]] = {key: set() for key in documents}
        outbound: dict[str, set[str]] = {key: set() for key in documents}

        def resolve(reference: str) -> str | None:
            target = _link_target(reference)
            parts = PurePosixPath(target).parts
            if not target or any(part in {"", ".", ".."} for part in parts):
                return None
            if target in documents:
                return target
            matches = basename_map.get(PurePosixPath(target).name, ())
            return matches[0] if len(matches) == 1 else None

        current_date = as_of or datetime.now(UTC).date()
        stale_before = current_date - timedelta(days=stale_after_days)
        for key, (relative_path, metadata, text) in documents.items():
            special = key in {"index", "_meta/topic-map"}
            if not special:
                for required in sorted(_REQUIRED_FRONTMATTER):
                    if required not in metadata:
                        issues.append(
                            KnowledgePageLintIssue(
                                "ERROR",
                                "FRONTMATTER_FIELD_MISSING",
                                relative_path,
                                required,
                            )
                        )
                    elif not metadata[required].strip().strip("\"'"):
                        issues.append(
                            KnowledgePageLintIssue(
                                "ERROR",
                                "FRONTMATTER_FIELD_EMPTY",
                                relative_path,
                                required,
                            )
                        )
                parsed_dates: dict[str, date] = {}
                for date_field in ("created", "updated"):
                    raw_date = metadata.get(date_field, "").strip().strip("\"'")
                    if not raw_date:
                        continue
                    try:
                        parsed_dates[date_field] = date.fromisoformat(raw_date)
                    except ValueError:
                        issues.append(
                            KnowledgePageLintIssue(
                                "ERROR",
                                f"{date_field.upper()}_DATE_INVALID",
                                relative_path,
                                raw_date,
                            )
                        )
                updated_date = parsed_dates.get("updated")
                if updated_date is not None and updated_date < stale_before:
                    issues.append(
                        KnowledgePageLintIssue(
                            "WARNING",
                            "STALE_PAGE",
                            relative_path,
                            updated_date.isoformat(),
                        )
                    )
                if metadata.get("contested", "").casefold() == "true":
                    issues.append(
                        KnowledgePageLintIssue(
                            "WARNING", "CONTESTED_PAGE", relative_path
                        )
                    )
                if metadata.get("confidence", "").strip("\"'").casefold() == "low":
                    issues.append(
                        KnowledgePageLintIssue(
                            "WARNING", "LOW_CONFIDENCE_PAGE", relative_path
                        )
                    )
                if len(text.splitlines()) > 200:
                    issues.append(
                        KnowledgePageLintIssue(
                            "WARNING", "PAGE_SPLIT_RECOMMENDED", relative_path
                        )
                    )

            references = [match.group(1) for match in _WIKILINK.finditer(text)]
            references.extend(_list_value(metadata.get("contradictions", "")))
            for reference in references:
                resolved = resolve(reference)
                if resolved is None:
                    issues.append(
                        KnowledgePageLintIssue(
                            "ERROR", "BROKEN_WIKILINK", relative_path, reference
                        )
                    )
                    continue
                outbound[key].add(resolved)
                if resolved != key:
                    inbound[resolved].add(key)
            for reference in _list_value(metadata.get("contradictions", "")):
                issues.append(
                    KnowledgePageLintIssue(
                        "WARNING", "DECLARED_CONTRADICTION", relative_path, reference
                    )
                )

        for key in sorted(content_keys):
            if not inbound[key]:
                issues.append(
                    KnowledgePageLintIssue(
                        "WARNING", "ORPHAN_PAGE", documents[key][0]
                    )
                )
        if content_keys and "index" not in documents:
            issues.append(KnowledgePageLintIssue("ERROR", "INDEX_MISSING", "index.md"))
        elif "index" in documents:
            for key in sorted(content_keys - outbound["index"]):
                issues.append(
                    KnowledgePageLintIssue(
                        "WARNING", "INDEX_MISSING_PAGE", documents[key][0]
                    )
                )
        if len(content_keys) > 200 and "_meta/topic-map" not in documents:
            issues.append(
                KnowledgePageLintIssue(
                    "WARNING", "TOPIC_MAP_REQUIRED", "_meta/topic-map.md"
                )
            )
        if truncated:
            issues.append(
                KnowledgePageLintIssue(
                    "ERROR", "SCAN_TRUNCATED", "", "bounded lint is incomplete"
                )
            )

        issues.sort(
            key=lambda item: (
                0 if item.severity == "ERROR" else 1,
                item.code,
                item.relative_path,
                item.reference,
            )
        )
        return KnowledgePageLintReport(
            folder_id=folder_id,
            scanned_pages=len(documents),
            scanned_bytes=scanned_bytes,
            truncated=truncated,
            issues=tuple(issues),
        )
