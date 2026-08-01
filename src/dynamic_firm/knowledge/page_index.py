"""Explicit, deterministic Knowledge navigation-index publication.

The user-owned Folder remains the current page authority.  This service only
previews a bounded index and, after exact digest confirmation, creates
``pages/index.md`` exclusively.  It never rewrites the index, records a
second database authority, or classifies content with a model.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path, PurePosixPath

from .folder_service import KnowledgeFolderService
from .page_contracts import page_payload_identity
from .page_lint import _discover_markdown_pages, _frontmatter, _list_value, _read_page
from .page_models import KnowledgePageIndexPreview, KnowledgePageIndexPublication
from .pages import _existing_state, _exclusive_write
from .store import KnowledgeStore


_INDEX_PATH = "pages/index.md"
_SAFE_TOPIC = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_SAFE_LINK_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,120}\Z")


def _bounded(value: int, *, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"Knowledge page index {label} must be between {minimum} and {maximum}")
    return value


def _topic(value: str, *, fallback: str) -> str:
    normalized = value.strip().casefold()
    return normalized if _SAFE_TOPIC.fullmatch(normalized) else fallback


def _safe_link_key(relative_path: str) -> str:
    key = PurePosixPath(relative_path).with_suffix("")
    if not key.parts or any(_SAFE_LINK_PART.fullmatch(part) is None for part in key.parts):
        raise ValueError("Knowledge page index cannot safely link a page path")
    return key.as_posix()


class KnowledgePageIndexService:
    """Build a navigation map from bounded local page metadata only."""

    def __init__(self, store: KnowledgeStore) -> None:
        self.store = store

    def preview(
        self,
        *,
        folder_id: str,
        max_pages: int = 1000,
        max_entries: int = 10_000,
        max_page_bytes: int = 256_000,
        max_total_bytes: int = 32_000_000,
    ) -> KnowledgePageIndexPreview:
        max_pages = _bounded(max_pages, label="page limit", minimum=1, maximum=10_000)
        max_entries = _bounded(max_entries, label="entry limit", minimum=max_pages, maximum=100_000)
        max_page_bytes = _bounded(max_page_bytes, label="byte limit", minimum=1, maximum=4_000_000)
        max_total_bytes = _bounded(max_total_bytes, label="total byte limit", minimum=1, maximum=1_000_000_000)
        folder = self.store.knowledge_folder(folder_id)
        if folder is None:
            raise ValueError("Knowledge page index Folder was not found")
        root = KnowledgeFolderService.validate_root(folder.root_path)
        pages_root = root / "pages"
        if pages_root.is_symlink():
            raise ValueError("Knowledge pages root must not be a symbolic link")
        if not pages_root.exists():
            paths = []
            issues = []
            truncated = False
        elif not pages_root.is_dir():
            raise ValueError("Knowledge pages root must be a directory")
        else:
            paths, issues, truncated = _discover_markdown_pages(
                pages_root, max_entries=max_entries, max_pages=max_pages
            )
        if issues or truncated:
            raise ValueError("Knowledge page index requires a complete safe page scan")

        by_type: dict[str, list[str]] = defaultdict(list)
        by_topic: dict[str, list[str]] = defaultdict(list)
        scanned_bytes = 0
        for path in paths:
            relative = path.relative_to(pages_root).as_posix()
            if relative in {"index.md", "_meta/topic-map.md"}:
                continue
            try:
                text, byte_size = _read_page(path, max_page_bytes=max_page_bytes)
            except (OSError, UnicodeError, ValueError) as error:
                raise ValueError("Knowledge page index requires readable regular UTF-8 pages") from error
            if scanned_bytes + byte_size > max_total_bytes:
                raise ValueError("Knowledge page index exceeds its total byte limit")
            scanned_bytes += byte_size
            key = _safe_link_key(relative)
            metadata = _frontmatter(text)
            page_type = _topic(metadata.get("type", ""), fallback="unclassified")
            topics = {_topic(item, fallback="untagged") for item in _list_value(metadata.get("tags", ""))}
            by_type[page_type].append(key)
            for topic in topics or {"untagged"}:
                by_topic[topic].append(key)

        markdown = _render_index(by_type=by_type, by_topic=by_topic)
        payload, digest, byte_size = page_payload_identity(markdown)
        target = root / "pages" / "index.md"
        target_state = _existing_state(target, payload)
        return KnowledgePageIndexPreview(
            folder_id=folder.folder_id,
            relative_path=_INDEX_PATH,
            markdown=markdown,
            content_sha256=digest,
            byte_size=byte_size,
            indexed_page_count=sum(len(items) for items in by_type.values()),
            topic_count=len(by_topic),
            target_state=target_state,
            publishable=target_state in {"NEW", "EXACT_MATCH_RECOVERABLE"},
        )

    def publish(
        self,
        *,
        folder_id: str,
        expected_content_sha256: str,
        confirm: bool,
        **limits: int,
    ) -> KnowledgePageIndexPublication:
        if not confirm:
            raise ValueError("Knowledge page index publication requires explicit confirmation")
        preview = self.preview(folder_id=folder_id, **limits)
        if preview.content_sha256 != expected_content_sha256:
            raise ValueError("Knowledge page index preview digest changed before publication")
        if not preview.publishable:
            raise ValueError(f"Knowledge page index target is not publishable: {preview.target_state}")
        if preview.target_state == "NEW":
            folder = self.store.knowledge_folder(folder_id)
            assert folder is not None
            root = KnowledgeFolderService.validate_root(folder.root_path)
            payload = preview.markdown.encode("utf-8")
            try:
                _exclusive_write(root, PurePosixPath(_INDEX_PATH), payload)
            except FileExistsError:
                if _existing_state(root / "pages" / "index.md", payload) != "EXACT_MATCH_RECOVERABLE":
                    raise ValueError("Knowledge page index target changed during publication") from None
        return KnowledgePageIndexPublication(
            folder_id=preview.folder_id,
            relative_path=preview.relative_path,
            content_sha256=preview.content_sha256,
            byte_size=preview.byte_size,
            indexed_page_count=preview.indexed_page_count,
            topic_count=preview.topic_count,
            target_state=preview.target_state,
        )


def _render_index(*, by_type: dict[str, list[str]], by_topic: dict[str, list[str]]) -> str:
    lines = [
        "<!-- noruct.knowledge-page-index.v1 -->",
        "# Knowledge Index",
        "",
        "This navigation page was explicitly published from bounded local metadata.",
        "It is not automatically updated; edit it freely or create a new page deliberately.",
        "",
        "## By type",
        "",
    ]
    if not by_type:
        lines.append("No publishable Markdown pages were found.")
    for page_type in sorted(by_type):
        lines.extend((f"### {page_type}", ""))
        lines.extend(f"- [[{key}]]" for key in sorted(by_type[page_type], key=str.casefold))
        lines.append("")
    lines.extend(("## By topic", ""))
    if not by_topic:
        lines.append("No page topics were declared.")
    for topic in sorted(by_topic):
        lines.extend((f"### {topic}", ""))
        lines.extend(f"- [[{key}]]" for key in sorted(by_topic[topic], key=str.casefold))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
